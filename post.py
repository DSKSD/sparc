import collections
import json
import logging
import os
import shutil
import torch
import math
import pandas as pd
import numpy as np
import six
from scipy.sparse import csr_matrix, save_npz, hstack, vstack
from termcolor import colored, cprint
from torch.utils.data import TensorDataset, DataLoader, RandomSampler, SequentialSampler
from eval_utils import normalize_answer, f1_score, exact_match_score

from multiprocessing import Queue
from multiprocessing.pool import ThreadPool
from threading import Thread

from tqdm import tqdm as tqdm_
from decimal import *

import tokenization

QuestionResult = collections.namedtuple("QuestionResult",
                                        ['qas_id', 'start', 'end', 'sparse', 'input_ids'])
_NbestPrediction = collections.namedtuple(  # pylint: disable=invalid-name
           "NbestPrediction", ["text", "logit", "no_answer_logit"])

logging.basicConfig(format='%(asctime)s - %(levelname)s - %(name)s -   %(message)s',
                    datefmt='%m/%d/%Y %H:%M:%S',
                    level=logging.INFO)
logger = logging.getLogger(__name__)

# For debugging
quant_stat = {}
b_quant_stat = {}
ranker = None


def tqdm(*args, mininterval=5.0, **kwargs):
    return tqdm_(*args, mininterval=mininterval, **kwargs)


def _improve_answer_span(doc_tokens, input_start, input_end, tokenizer,
                         orig_answer_text):
    """Returns tokenized answer spans that better match the annotated answer."""

    # The SQuAD annotations are character based. We first project them to
    # whitespace-tokenized words. But then after WordPiece tokenization, we can
    # often find a "better match". For example:
    #
    #   Question: What year was John Smith born?
    #   Context: The leader was John Smith (1895-1943).
    #   Answer: 1895
    #
    # The original whitespace-tokenized answer will be "(1895-1943).". However
    # after tokenization, our tokens will be "( 1895 - 1943 ) .". So we can match
    # the exact answer, 1895.
    #
    # However, this is not always possible. Consider the following:
    #
    #   Question: What country is the top exporter of electornics?
    #   Context: The Japanese electronics industry is the lagest in the world.
    #   Answer: Japan
    #
    # In this case, the annotator chose "Japan" as a character sub-span of
    # the word "Japanese". Since our WordPiece tokenizer does not split
    # "Japanese", we just use "Japanese" as the annotation. This is fairly rare
    # in SQuAD, but does happen.
    tok_answer_text = " ".join(tokenizer.tokenize(orig_answer_text))

    for new_start in range(input_start, input_end + 1):
        for new_end in range(input_end, new_start - 1, -1):
            text_span = " ".join(doc_tokens[new_start:(new_end + 1)])
            if text_span == tok_answer_text:
                return (new_start, new_end)

    return (input_start, input_end)


def _check_is_max_context(doc_spans, cur_span_index, position):
    """Check if this is the 'max context' doc span for the token."""

    # Because of the sliding window approach taken to scoring documents, a single
    # token can appear in multiple documents. E.g.
    #  Doc: the man went to the store and bought a gallon of milk
    #  Span A: the man went to the
    #  Span B: to the store and bought
    #  Span C: and bought a gallon of
    #  ...
    #
    # Now the word 'bought' will have two scores from spans B and C. We only
    # want to consider the score with "maximum context", which we define as
    # the *minimum* of its left and right context (the *sum* of left and
    # right context will always be the same, of course).
    #
    # In the example the maximum context for 'bought' would be span C since
    # it has 1 left context and 3 right context, while span B has 4 left context
    # and 0 right context.
    best_score = None
    best_span_index = None
    for (span_index, doc_span) in enumerate(doc_spans):
        end = doc_span.start + doc_span.length - 1
        if position < doc_span.start:
            continue
        if position > end:
            continue
        num_left_context = position - doc_span.start
        num_right_context = end - position
        score = min(num_left_context, num_right_context) + 0.01 * doc_span.length
        if best_score is None or score > best_score:
            best_score = score
            best_span_index = span_index

    return cur_span_index == best_span_index


def get_metadata(id2example, features, results, max_answer_length, do_lower_case, verbose_logging):
    start = np.concatenate([result.start[1:len(feature.tokens) - 1] for feature, result in zip(features, results)],
                           axis=0)
    end = np.concatenate([result.end[1:len(feature.tokens) - 1] for feature, result in zip(features, results)], axis=0)

    input_ids = None
    sparse_map = None
    sparse_bi_map = None
    sparse_tri_map = None
    len_per_para = []
    if results[0].start_sp is not None:
        input_ids = np.concatenate([f.input_ids[1:len(f.tokens) - 1] for f in features], axis=0)
        sparse_features = None # uni
        sparse_bi_features = None
        sparse_tri_features = None
        if '1' in results[0].start_sp:
            sparse_features = [result.start_sp['1'][1:len(feature.tokens)-1, 1:len(feature.tokens)-1]
                               for feature, result in zip(features, results)]
        if '2' in results[0].start_sp:
            sparse_bi_features = [result.start_sp['2'][1:len(feature.tokens)-1, 1:len(feature.tokens)-1]
                               for feature, result in zip(features, results)]
        if '3' in results[0].start_sp:
            sparse_tri_features = [result.start_sp['3'][1:len(feature.tokens)-1, 1:len(feature.tokens)-1]
                               for feature, result in zip(features, results)]

        # TODO: Fix here as there could be no '1'
        map_size = max([k.shape[0] for k in sparse_features])
        sparse_map = np.zeros((input_ids.shape[0], map_size), dtype=np.float32)
        if sparse_bi_features is not None:
            sparse_bi_map = np.zeros((input_ids.shape[0], map_size), dtype=np.float32)
        if sparse_tri_features is not None:
            sparse_tri_map = np.zeros((input_ids.shape[0], map_size), dtype=np.float32)

        curr_size = 0
        for sidx, sparse_feature in enumerate(sparse_features):
            sparse_map[curr_size:curr_size + sparse_feature.shape[0],:sparse_feature.shape[1]] += sparse_feature
            if sparse_bi_features is not None:
                assert sparse_bi_features[sidx].shape == sparse_feature.shape
                sparse_bi_map[curr_size:curr_size + sparse_bi_features[sidx].shape[0],:sparse_bi_features[sidx].shape[1]] += \
                    sparse_bi_features[sidx]
            if sparse_tri_features is not None:
                assert sparse_tri_features[sidx].shape == sparse_feature.shape
                sparse_tri_map[curr_size:curr_size + sparse_tri_features[sidx].shape[0],:sparse_tri_features[sidx].shape[1]] += \
                    sparse_tri_features[sidx]
            curr_size += sparse_feature.shape[0]
            len_per_para.append(sparse_feature.shape[0])

        assert input_ids.shape[0] == start.shape[0] and curr_size == sparse_map.shape[0]

    fs = np.concatenate([result.filter_start_logits[1:len(feature.tokens) - 1]
                         for feature, result in zip(features, results)],
                         axis=0)
    fe = np.concatenate([result.filter_end_logits[1:len(feature.tokens) - 1]
                         for feature, result in zip(features, results)],
                        axis=0)

    span_logits = np.zeros([np.shape(start)[0], max_answer_length], dtype=start.dtype)
    start2end = -1 * np.ones([np.shape(start)[0], max_answer_length], dtype=np.int32)
    idx = 0
    for feature, result in zip(features, results):
        for i in range(1, len(feature.tokens) - 1):
            for j in range(i, min(i + max_answer_length, len(feature.tokens) - 1)):
                span_logits[idx, j - i] = result.span_logits[i, j]
                start2end[idx, j - i] = idx + j - i
            idx += 1

    word2char_start = np.zeros([start.shape[0]], dtype=np.int32)
    word2char_end = np.zeros([start.shape[0]], dtype=np.int32)

    sep = ' [PAR] '
    full_text = ""
    prev_example = None

    word_pos = 0
    for feature in features:
        example = id2example[feature.unique_id]
        if prev_example is not None and feature.doc_span_index == 0:
            full_text = full_text + ' '.join(prev_example.doc_words) + sep

        for i in range(1, len(feature.tokens) - 1):
            _, start_pos, _ = get_final_text_(example, feature, i, min(len(feature.tokens) - 2, i + 1), do_lower_case,
                                              verbose_logging)
            _, _, end_pos = get_final_text_(example, feature, max(1, i - 1), i, do_lower_case,
                                            verbose_logging)
            start_pos += len(full_text)
            end_pos += len(full_text)
            word2char_start[word_pos] = start_pos
            word2char_end[word_pos] = end_pos
            word_pos += 1
        prev_example = example
    full_text = full_text + ' '.join(prev_example.doc_words)

    metadata = {'did': prev_example.doc_idx, 'context': full_text, 'title': prev_example.title,
                'start': start, 'end': end, 'span_logits': span_logits,
                'start2end': start2end,
                'word2char_start': word2char_start, 'word2char_end': word2char_end,
                'filter_start': fs, 'filter_end': fe, 'input_ids': input_ids,
                'sparse': sparse_map, 'sparse_bi': sparse_bi_map, 'sparse_tri': sparse_tri_map,
                'len_per_para': len_per_para}

    '''
    # For analysis
    # if 'never' in features[0].tokens or 'example' in features[0].tokens or 'not' in features[0].tokens:
    # if 'message includes a budget message and an economic' in full_text:
    if False:
        start_index = [118]
        par_index = 0
        for vidx, (v1, v2) in enumerate(zip(features[par_index].tokens[1:-1],
            results[par_index].start_sp['1'][start_index[0]+1][1:len(features[par_index].tokens)-1])):
            # out_json.append((v1, v2))
            if vidx in start_index:
                cprint('{}({:.3f}, {})'.format(v1, v2, vidx), 'green', end=' ')
                continue
            if v2 > 1.0:
                cprint('{}({:.3f}, {})'.format(v1, v2, vidx), 'red', end=' ')
            else:
                print('{}({:.3f}, {})'.format(v1, v2, vidx), end=' ')
        print()

        for vidx, (v1, v2) in enumerate(zip(features[par_index].tokens[1:-1],
            results[par_index].start_sp['2'][start_index[0]+1][1:len(features[par_index].tokens)-1])):
            # out_json.append((v1, v2))
            if vidx in start_index:
                cprint('{}({:.3f}, {})'.format(v1, v2, vidx), 'green', end=' ')
                continue
            if v2 > 1.0:
                cprint('{}({:.3f}, {})'.format(v1, v2, vidx), 'red', end=' ')
            else:
                print('{}({:.3f}, {})'.format(v1, v2, vidx), end=' ')
        print()

        # DrQA analysis
        tfidf_result = ranker.text2spvec(full_text.split('[PAR]')[par_index])
        # tfidf_result = ranker.text2spvec(' '.join(features[par_index].tokens[1:-1]))
        print(tfidf_result)
        exit()

    for vidx, (v1, v2) in enumerate(zip(features[0].tokens[1:-1],
        results[0].start_sp['3'][1][1:len(features[0].tokens)-1])):
        # out_json.append((v1, v2))
        if vidx in start_index:
            cprint('{}({:.3f})'.format(v1, v2), 'green', end=' ')
            continue
        if v2 > 0.1:
            cprint('{}({:.3f})'.format(v1, v2), 'red', end=' ')
        else:
            print('{}({:.3f})'.format(v1, v2), end=' ')
    exit()
    '''

    return metadata

def filter_metadata(metadata, threshold):
    start_idxs, = np.where(metadata['filter_start'] > threshold)
    end_idxs, = np.where(metadata['filter_end'] > threshold)
    end_long2short = {long: short for short, long in enumerate(end_idxs)}

    # metadata['word2char_start'] = metadata['word2char_start'][start_idxs]
    # metadata['word2char_end'] = metadata['word2char_end'][end_idxs]
    metadata['start'] = metadata['start'][start_idxs]
    metadata['end'] = metadata['end'][end_idxs]
    metadata['sparse'] = metadata['sparse'][start_idxs]
    if metadata['sparse_bi'] is not None:
        metadata['sparse_bi'] = metadata['sparse_bi'][start_idxs]
    if metadata['sparse_tri'] is not None:
        metadata['sparse_tri'] = metadata['sparse_tri'][start_idxs]
    metadata['f2o_start'] = start_idxs
    metadata['f2o_end'] = end_idxs
    metadata['span_logits'] = metadata['span_logits'][start_idxs]
    metadata['start2end'] = metadata['start2end'][start_idxs]
    for i, each in enumerate(metadata['start2end']):
        for j, long in enumerate(each.tolist()):
            metadata['start2end'][i, j] = end_long2short[long] if long in end_long2short else -1

    return metadata


def compress_metadata(metadata, dense_offset, dense_scale, sparse_offset, sparse_scale):
    for key in ['start', 'end']:
        if key in metadata:
            '''
            if key == 'start':
                for meta in metadata[key]:
                    for number in meta:
                        num_str = "%.1f" % number
                        if float(num_str) not in b_quant_stat:
                            b_quant_stat[float(num_str)] = 0
                        b_quant_stat[float(num_str)] += 1
            '''
            metadata[key] = float_to_int8(metadata[key], dense_offset, dense_scale)
            '''
            if key == 'start':
                for meta in metadata[key]:
                    for number in meta:
                        num_str = "%d" % number
                        if int(num_str) not in quant_stat:
                            quant_stat[int(num_str)] = 0
                        quant_stat[int(num_str)] += 1
            '''
    for key in ['sparse', 'sparse_bi', 'sparse_tri']:
        if key in metadata and metadata[key] is not None:
            '''
            if key == 'sparse':
                for meta in metadata[key]:
                    for number in meta:
                        num_str = "%.1f" % number
                        if float(num_str) not in b_quant_stat:
                            b_quant_stat[float(num_str)] = 0
                        b_quant_stat[float(num_str)] += 1
            '''
            metadata[key] = float_to_int8(metadata[key], sparse_offset, sparse_scale)
            '''
            if key == 'sparse':
                for meta in metadata[key]:
                    for number in meta:
                        num_str = "%d" % number
                        if int(num_str) not in quant_stat:
                            quant_stat[int(num_str)] = 0
                        quant_stat[int(num_str)] += 1
            '''
    return metadata


def pool_func(item):
    metadata_ = get_metadata(*item[:-1])
    metadata_ = filter_metadata(metadata_, item[-1])
    return metadata_


def write_hdf5(all_examples, all_features, all_results,
               max_answer_length, do_lower_case, hdf5_path, filter_threshold, verbose_logging,
               dense_offset=None, dense_scale=None, sparse_offset=None, sparse_scale=None, use_sparse=False):
    assert len(all_examples) > 0

    import h5py
    from multiprocessing import Process
    from time import time

    id2feature = {feature.unique_id: feature for feature in all_features}
    id2example = {id_: all_examples[id2feature[id_].example_index] for id_ in id2feature}

    '''
    from drqa import retriever
    from drqa.retriever import utils

    global ranker
    RANKER_PATH = '/home/jinhyuk/github/drqa/data/wikipedia/docs-tfidf-ngram=2-hash=16777216-tokenizer=simple.npz'
    ranker = MyTfidfDocRanker(
        tfidf_path=RANKER_PATH,
        strict=False
    )
    '''

    # Deprecated?
    def add_(inqueue_, outqueue_):
        with ThreadPool(2) as pool:
            items = []
            for item in iter(inqueue_.get, None):
                args = list(item[:3]) + [max_answer_length, do_lower_case, verbose_logging] + [item[3],
                                                                                               filter_threshold]
                items.append(args)
                if len(items) < 16:
                    continue
                out = pool.map(pool_func, items)
                map(outqueue_.put, out)
                items = []

            out = pool.map(pool_func, items)
            map(outqueue_.put, out)

            outqueue_.put(None)

    def add(inqueue_, outqueue_):
        for item in iter(inqueue_.get, None):
            args = list(item[:3]) + [max_answer_length, do_lower_case, verbose_logging, filter_threshold]
            out = pool_func(args)
            outqueue_.put(out)

        outqueue_.put(None)

    # Deprecated
    def recursively_save_dict_contents_to_group(h5file, path, dic):
        for key, item in dic.items():
            if item is None:
                return
            if type(item) == int:
                item = np.int64(item)
            if type(item) == bool:
                item = 'true' if item else 'false'
            if isinstance(item, (np.ndarray, np.int64, np.float64, str, bytes)):
                h5file.create_dataset(path + key, data=item)
            elif isinstance(item, dict):
                recursively_save_dict_contents_to_group(h5file, path+key+'/', item)
            elif type(item) == list:
                recursively_save_dict_contents_to_group(h5file, path+key+'/', {str(kk): tt for kk, tt in enumerate(item)})
            elif pd.isnull(item):
                h5file.create_dataset(path + key, data='NaN')
            else:
                raise ValueError('Cannot save %s type %s'%(type(item), item))

    def write(outqueue_):
        with h5py.File(hdf5_path) as f:
            while True:
                metadata = outqueue_.get()
                if metadata:
                    did = str(metadata['did'])
                    if did in f:
                        logger.info('%s exists; replacing' % did)
                        del f[did]
                    dg = f.create_group(did)

                    dg.attrs['context'] = metadata['context']
                    dg.attrs['title'] = metadata['title']
                    if dense_offset is not None:
                        metadata = compress_metadata(metadata, dense_offset, dense_scale, sparse_offset, sparse_scale)
                        dg.attrs['offset'] = dense_offset
                        dg.attrs['scale'] = dense_scale
                        dg.attrs['sparse_offset'] = sparse_offset
                        dg.attrs['sparse_scale'] = sparse_scale
                    dg.create_dataset('start', data=metadata['start'])
                    dg.create_dataset('end', data=metadata['end'])
                    if metadata['sparse'] is not None:
                        dg.create_dataset('sparse', data=metadata['sparse'])
                        if metadata['sparse_bi'] is not None:
                            dg.create_dataset('sparse_bi', data=metadata['sparse_bi'])
                        if metadata['sparse_tri'] is not None:
                            dg.create_dataset('sparse_tri', data=metadata['sparse_tri'])
                        dg.create_dataset('input_ids', data=metadata['input_ids'])
                        dg.create_dataset('len_per_para', data=metadata['len_per_para'])
                    dg.create_dataset('span_logits', data=metadata['span_logits'])
                    dg.create_dataset('start2end', data=metadata['start2end'])
                    dg.create_dataset('word2char_start', data=metadata['word2char_start'])
                    dg.create_dataset('word2char_end', data=metadata['word2char_end'])
                    dg.create_dataset('f2o_start', data=metadata['f2o_start'])
                    dg.create_dataset('f2o_end', data=metadata['f2o_end'])
                    # recursively_save_dict_contents_to_group(dg, 'metadata/', metadata['metadata'])

                else:
                    break

    features = []
    results = []
    inqueue = Queue(maxsize=500)
    outqueue = Queue(maxsize=500)
    write_p = Thread(target=write, args=(outqueue,))
    p = Thread(target=add, args=(inqueue, outqueue))
    write_p.start()
    p.start()

    start_time = time()
    for count, result in enumerate(tqdm(all_results, total=len(all_features))):
        example = id2example[result.unique_id]
        feature = id2feature[result.unique_id]
        condition = len(features) > 0 and example.par_idx == 0 and feature.doc_span_index == 0

        if condition:
            in_ = (id2example, features, results)
            logger.info('inqueue size: %d, outqueue size: %d' % (inqueue.qsize(), outqueue.qsize()))
            inqueue.put(in_)
            # add(id2example, features, results)
            features = [feature]
            results = [result]
        else:
            features.append(feature)
            results.append(result)
        if count % 500 == 0:
            logger.info('%d/%d at %.1f' % (count + 1, len(all_features), time() - start_time))
    in_ = (id2example, features, results)
    inqueue.put(in_)
    inqueue.put(None)
    p.join()
    write_p.join()

    import collections
    b_stats = collections.OrderedDict(sorted(b_quant_stat.items()))
    stats = collections.OrderedDict(sorted(quant_stat.items()))
    for k, v in b_stats.items():
        print(k, v)
    for k, v in stats.items():
        print(k, v)


def write_predictions(all_examples, all_features, all_results,
                      max_answer_length, do_lower_case, output_prediction_file, 
                      output_score_file, verbose_logging, threshold):

    id2feature = {feature.unique_id: feature for feature in all_features}
    id2example = {id_: all_examples[id2feature[id_].example_index] for id_ in id2feature}

    token_count = 0
    vec_count = 0
    predictions = {}
    scores = {}
    loss = []

    for result in tqdm(all_results, total=len(all_features), desc='[Evaluation]'):
        loss += [result.loss]
        feature = id2feature[result.unique_id]
        example = id2example[result.unique_id]
        id_ = example.qas_id

        # Initial setting
        # predictions[id_] = ''
        # scores[id_] = -1e9
        token_count += len(feature.tokens)

        for start_index in range(len(feature.tokens)):
            for end_index in range(start_index, min(len(feature.tokens), start_index + max_answer_length - 1)):
                if start_index not in feature.token_to_word_map:
                    continue
                if end_index not in feature.token_to_word_map:
                    continue
                if not feature.token_is_max_context.get(start_index, False):
                    continue
                filter_start_logit = result.filter_start_logits[start_index]
                filter_end_logit = result.filter_end_logits[end_index]

                # Filter based on threshold (default: -2)
                if filter_start_logit < threshold or filter_end_logit < threshold:
                    # orig_text, start_pos, end_pos = get_final_text_(example, feature, start_index, end_index,
                    #                                                 do_lower_case, verbose_logging)
                    # print('Filter: %s (%.2f, %.2f)'% (orig_text[start_pos:end_pos], filter_start_logit, filter_end_logit))
                    continue
                else:
                    # orig_text, start_pos, end_pos = get_final_text_(example, feature, start_index, end_index,
                    #                                                 do_lower_case, verbose_logging)
                    # print('Saved: %s (%.2f, %.2f)'% (orig_text[start_pos:end_pos], filter_start_logit, filter_end_logit))
                    pass

                vec_count += 1
                score = result.all_logits[start_index, end_index]

                if id_ not in scores or score > scores[id_]:
                    orig_text, start_pos, end_pos = get_final_text_(example, feature, start_index, end_index,
                                                                    do_lower_case, verbose_logging)
                    # print('Saved: %s (%.2f, %.2f)'% (orig_text[start_pos:end_pos], filter_start_logit, filter_end_logit))
                    phrase = orig_text[start_pos:end_pos]
                    predictions[id_] = phrase
                    scores[id_] = score.item()

        if id_ not in predictions:
            assert id_ not in scores
            logger.info('for %s, no answer found'% id_)

    logger.info('num vecs=%d, num_words=%d, nvpw=%.4f' % (vec_count, token_count, vec_count / token_count))

    with open(output_prediction_file, 'w') as fp:
        json.dump(predictions, fp)

    with open(output_score_file, 'w') as fp:
        json.dump({k: -v for (k, v) in scores.items()}, fp)

    return sum(loss) / len(loss)


def get_question_results(question_examples, query_eval_features, question_dataloader, device, model):
    id2feature = {feature.unique_id: feature for feature in query_eval_features}
    id2example = {id_: question_examples[id2feature[id_].example_index] for id_ in id2feature}
    for (input_ids_, input_mask_, example_indices) in question_dataloader:
        input_ids_ = input_ids_.to(device)
        input_mask_ = input_mask_.to(device)
        with torch.no_grad():
            batch_start, batch_end, batch_sps, batch_eps = model(query_ids=input_ids_,
                                                                 query_mask=input_mask_)
        for i, example_index in enumerate(example_indices):
            start = batch_start[i].detach().cpu().numpy().astype(np.float16)
            end = batch_end[i].detach().cpu().numpy().astype(np.float16)
            sparse = None
            if len(batch_sps) > 0:
                # sparse = batch_sps[i].detach().cpu().numpy().astype(np.float16)
                sparse = {ng: bb_ssp[i].detach().cpu().numpy().astype(np.float16) for ng, bb_ssp in batch_sps.items()}
            # span_logit = batch_span_logits[i].detach().cpu().numpy().astype(np.float16)
            query_eval_feature = query_eval_features[example_index.item()]
            unique_id = int(query_eval_feature.unique_id)
            qas_id = id2example[unique_id].qas_id
            yield QuestionResult(qas_id=qas_id,
                                 start=start,
                                 end=end,
                                 sparse=sparse,
                                 input_ids=query_eval_feature.input_ids[1:len(query_eval_feature.tokens_)-1])


def write_question_results(question_results, question_features, path):
    import h5py
    '''
    global ranker
    RANKER_PATH = '/home/jinhyuk/github/drqa/data/wikipedia/docs-tfidf.npz'
    ranker = MyTfidfDocRanker(
        tfidf_path=RANKER_PATH,
        strict=False
    )
    '''
    with h5py.File(path, 'w') as f:
        for question_result, question_feature in tqdm(zip(question_results, question_features)):
            '''
            if '56d9c92bdc89441400fdb80e' in question_result.qas_id:
                tfidf_result = ranker.text2spvec(' '.join(question_feature.tokens_)[1:-1])
                print(tfidf_result)
                exit()
            else:
                print('pass', question_result.qas_id)
                continue
            '''
            sparse = None
            sparse_bi = None
            sparse_tri = None
            input_ids = None
            if question_result.sparse is not None:
                sparse = question_result.sparse['1'][1:len(question_feature.tokens_) - 1]
                if '2' in question_result.sparse:
                    sparse_bi = question_result.sparse['2'][1:len(question_feature.tokens_) - 1]
                if '3' in question_result.sparse:
                    sparse_tri = question_result.sparse['3'][1:len(question_feature.tokens_) - 1]

                input_ids = question_feature.input_ids[1:len(question_feature.tokens_) - 1]

            dummy_ones = np.ones((question_result.start.shape[0], 1))
            data = np.concatenate([question_result.start, question_result.end, dummy_ones], -1)
            f.create_dataset(question_result.qas_id, data=data)
            # print(question_result.qas_id, input_ids, data[0,:10])

            if False:
            # if sparse is not None:
                # print([(tok, val) for tok, val in zip(question_feature.tokens_[1:-1], sparse)])
                # print([(tok, val) for tok, val in zip(question_feature.tokens_[1:-1], sparse_bi)])
                # print([(tok, val) for tok, val in zip(question_feature.tokens_[1:-1], sparse_tri)])
                # print()
                '''
                start_index = []
                for vidx, (v1, v2) in enumerate(zip(question_feature.tokens_[1:-1], sparse)):
                    # out_json.append((v1, v2))
                    if vidx in start_index:
                        cprint('{}({:.3f})'.format(v1, v2), 'green', end=' ')
                        continue
                    if v2 > 1.0:
                        cprint('{}({:.3f})'.format(v1, v2), 'red', end=' ')
                    else:
                        print('{}({:.3f})'.format(v1, v2), end=' ')
                print()
                for vidx, (v1, v2) in enumerate(zip(question_feature.tokens_[1:-1], sparse_bi)):
                    # out_json.append((v1, v2))
                    if vidx in start_index:
                        cprint('{}({:.3f})'.format(v1, v2), 'green', end=' ')
                        continue
                    if v2 > 1.0:
                        cprint('{}({:.3f})'.format(v1, v2), 'red', end=' ')
                    else:
                        print('{}({:.3f})'.format(v1, v2), end=' ')
                print()
                print()
                for vidx, (v1, v2) in enumerate(zip(question_feature.tokens_[1:-1], sparse_tri)):
                    # out_json.append((v1, v2))
                    if vidx in start_index:
                        cprint('{}({:.3f})'.format(v1, v2), 'green', end=' ')
                        continue
                    if v2 > 0.1:
                        cprint('{}({:.3f})'.format(v1, v2), 'red', end=' ')
                    else:
                        print('{}({:.3f})'.format(v1, v2), end=' ')
                print()
                exit()
                '''
                f.create_dataset(question_result.qas_id + '_sparse', data=sparse)
                if sparse_bi is not None:
                    f.create_dataset(question_result.qas_id + '_sparse_bi', data=sparse_bi)
                if sparse_tri is not None:
                    f.create_dataset(question_result.qas_id + '_sparse_tri', data=sparse_tri)
                f.create_dataset(question_result.qas_id + '_input_ids', data=input_ids)


def convert_question_features_to_dataloader(query_eval_features, fp16, local_rank, predict_batch_size):
    all_input_ids_ = torch.tensor([f.input_ids for f in query_eval_features], dtype=torch.long)
    all_input_mask_ = torch.tensor([f.input_mask for f in query_eval_features], dtype=torch.long)
    all_example_index_ = torch.arange(all_input_ids_.size(0), dtype=torch.long)
    if fp16:
        all_input_ids_, all_input_mask_ = tuple(t.half() for t in (all_input_ids_, all_input_mask_))

    question_data = TensorDataset(all_input_ids_, all_input_mask_, all_example_index_)

    if local_rank == -1:
        question_sampler = SequentialSampler(question_data)
    else:
        question_sampler = DistributedSampler(question_data)
    question_dataloader = DataLoader(question_data, sampler=question_sampler, batch_size=predict_batch_size)
    return question_dataloader


def get_final_text_(example, feature, start_index, end_index, do_lower_case, verbose_logging):
    tok_tokens = feature.tokens[start_index:(end_index + 1)]
    orig_doc_start = feature.token_to_word_map[start_index]
    orig_doc_end = feature.token_to_word_map[end_index]
    orig_words = example.doc_words[orig_doc_start:(orig_doc_end + 1)]
    tok_text = " ".join(tok_tokens)

    # De-tokenize WordPieces that have been split off.
    tok_text = tok_text.replace(" ##", "")
    tok_text = tok_text.replace("##", "")

    # Clean whitespace
    tok_text = tok_text.strip()
    tok_text = " ".join(tok_text.split())
    orig_text = " ".join(orig_words)
    full_text = " ".join(example.doc_words)

    start_pos, end_pos = get_final_text(tok_text, orig_text, do_lower_case, verbose_logging) # TODO: need to check
    offset = sum(len(word) + 1 for word in example.doc_words[:orig_doc_start])

    return full_text, offset + start_pos, offset + end_pos


def get_final_text(pred_text, orig_text, do_lower_case, verbose_logging=False):
    """Project the tokenized prediction back to the original text."""

    # When we created the data, we kept track of the alignment between original
    # (whitespace tokenized) tokens and our WordPiece tokenized tokens. So
    # now `orig_text` contains the span of our original text corresponding to the
    # span that we predicted.
    #
    # However, `orig_text` may contain extra characters that we don't want in
    # our prediction.
    #
    # For example, let's say:
    #   pred_text = steve smith
    #   orig_text = Steve Smith's
    #
    # We don't want to return `orig_text` because it contains the extra "'s".
    #
    # We don't want to return `pred_text` because it's already been normalized
    # (the SQuAD eval script also does punctuation stripping/lower casing but
    # our tokenizer does additional normalization like stripping accent
    # characters).
    #
    # What we really want to return is "Steve Smith".
    #
    # Therefore, we have to apply a semi-complicated alignment heruistic between
    # `pred_text` and `orig_text` to get a character-to-charcter alignment. This
    # can fail in certain cases in which case we just return `orig_text`.
    default_out = 0, len(orig_text)

    def _strip_spaces(text):
        ns_chars = []
        ns_to_s_map = collections.OrderedDict()
        for (i, c) in enumerate(text):
            if c == " ":
                continue
            ns_to_s_map[len(ns_chars)] = i
            ns_chars.append(c)
        ns_text = "".join(ns_chars)
        return (ns_text, ns_to_s_map)

    # We first tokenize `orig_text`, strip whitespace from the result
    # and `pred_text`, and check if they are the same length. If they are
    # NOT the same length, the heuristic has failed. If they are the same
    # length, we assume the characters are one-to-one aligned.
    tokenizer = tokenization.BasicTokenizer(do_lower_case=do_lower_case)

    tok_text = " ".join(tokenizer.tokenize(orig_text))

    start_position = tok_text.find(pred_text)
    if start_position == -1:
        if verbose_logging:
            logger.info(
                "Unable to find text: '%s' in '%s'" % (pred_text, orig_text))
        return default_out
    end_position = start_position + len(pred_text) - 1

    (orig_ns_text, orig_ns_to_s_map) = _strip_spaces(orig_text)
    (tok_ns_text, tok_ns_to_s_map) = _strip_spaces(tok_text)

    if len(orig_ns_text) != len(tok_ns_text):
        if verbose_logging:
            logger.info("Length not equal after stripping spaces: '%s' vs '%s'",
                        orig_ns_text, tok_ns_text)
        return default_out

    # We then project the characters in `pred_text` back to `orig_text` using
    # the character-to-character alignment.
    tok_s_to_ns_map = {}
    for (i, tok_index) in six.iteritems(tok_ns_to_s_map):
        tok_s_to_ns_map[tok_index] = i

    orig_start_position = None
    if start_position in tok_s_to_ns_map:
        ns_start_position = tok_s_to_ns_map[start_position]
        if ns_start_position in orig_ns_to_s_map:
            orig_start_position = orig_ns_to_s_map[ns_start_position]

    if orig_start_position is None:
        if verbose_logging:
            logger.info("Couldn't map start position")
        return default_out

    orig_end_position = None
    if end_position in tok_s_to_ns_map:
        ns_end_position = tok_s_to_ns_map[end_position]
        if ns_end_position in orig_ns_to_s_map:
            orig_end_position = orig_ns_to_s_map[ns_end_position]

    if orig_end_position is None:
        if verbose_logging:
            logger.info("Couldn't map end position")
        return default_out

    # output_text = orig_text[orig_start_position:(orig_end_position + 1)]
    return orig_start_position, orig_end_position + 1


def float_to_int8(num, offset, factor, keep_zeros=False):
    out = (num - offset) * factor
    out = out.clip(-128, 127)
    if keep_zeros:
        out = out * (num != 0.0).astype(np.int8)
    out = np.round(out).astype(np.int8)
    return out


def int8_to_float(num, offset, factor, keep_zeros=False):
    if not keep_zeros:
        return num.astype(np.float32) / factor + offset
    else:
        return (num.astype(np.float32) / factor + offset) * (num != 0.0).astype(np.float32)


def write_predictions_nq(logger, all_examples, all_features, all_results, n_best_size,
                      do_lower_case, output_prediction_file,
                      output_nbest_file, verbose_logging,
                      write_prediction=True, n_paragraphs=None):

    """Write final predictions to the json file."""

    example_index_to_features = collections.defaultdict(list)
    for feature in all_features:
        example_index_to_features[feature.example_index].append(feature)

    unique_id_to_result = {}
    for result in all_results:
        unique_id_to_result[result.unique_id] = result

    _PrelimPrediction = collections.namedtuple(  # pylint: disable=invalid-name
       "PrelimPrediction",
       ["paragraph_index", "feature_index", "start_index", "end_index", "logit", "no_answer_logit"])

    all_predictions = collections.OrderedDict()
    all_nbest_json = collections.OrderedDict()

    if verbose_logging:
        all_examples = tqdm(enumerate(all_examples))
    else:
        all_examples = enumerate(all_examples)

    for (example_index, example) in all_examples:
        features = example_index_to_features[example_index]
        if len(features)==0 and n_paragraphs is None:
            pred = _NbestPrediction(
                        text="empty",
                        logit=-1000,
                        no_answer_logit=1000)
            all_predictions[example.qas_id] = ("empty", example.all_answers)
            all_nbest_json[example.qas_id] = [pred]
            continue

        prelim_predictions = []
        yn_predictions = []

        if n_paragraphs is None:
            results = sorted(enumerate(features),
                         key=lambda f: unique_id_to_result[f[1].unique_id].switch[3])[:1]
        else:
            results = enumerate(features)
        for (feature_index, feature) in results:
            result = unique_id_to_result[feature.unique_id]
            scores = []
            start_logits = result.start_logits[:len(feature.tokens)]
            end_logits = result.end_logits[:len(feature.tokens)]
            for (i, s) in enumerate(start_logits):
                for (j, e) in enumerate(end_logits[i:i+10]):
                    scores.append(((i, i+j), s+e))

            scores = sorted(scores, key=lambda x: x[1], reverse=True)

            cnt = 0
            for (start_index, end_index), score in scores:
                if start_index >= len(feature.tokens):
                    continue
                if end_index >= len(feature.tokens):
                    continue
                if start_index not in feature.token_to_word_map:
                    continue
                if end_index not in feature.token_to_word_map:
                    continue
                if not feature.token_is_max_context.get(start_index, False):
                    continue
                if end_index < start_index:
                    continue
                prelim_predictions.append(
                   _PrelimPrediction(
                       paragraph_index=feature.paragraph_index,
                       feature_index=feature_index,
                       start_index=start_index,
                       end_index=end_index,
                       logit=-result.switch[3], #score,
                       no_answer_logit=result.switch[3]))
                if n_paragraphs is None:
                    if write_predictions and len(prelim_predictions)>=n_best_size:
                        break
                    elif not write_predictions:
                        break
                cnt += 1

        prelim_predictions = sorted(
                prelim_predictions,
                key=lambda x: x.logit,
                reverse=True)
        no_answer_logit = result.switch[3]

        def get_nbest_json(prelim_predictions):

            seen_predictions = {}
            nbest = []
            for pred in prelim_predictions:
                if len(nbest) >= n_best_size:
                    break

                if pred.start_index == pred.end_index == -1:
                    final_text = "yes"
                elif pred.start_index == pred.end_index == -2:
                    final_text = "no"
                else:
                    feature = features[pred.feature_index]

                    tok_tokens = feature.tokens[pred.start_index:(pred.end_index + 1)]
                    orig_doc_start = feature.token_to_word_map[pred.start_index]
                    orig_doc_end = feature.token_to_word_map[pred.end_index]
                    orig_tokens = feature.doc_tokens[orig_doc_start:(orig_doc_end + 1)]
                    tok_text = " ".join(tok_tokens)

                    # De-tokenize WordPieces that have been split off.
                    tok_text = tok_text.replace(" ##", "")
                    tok_text = tok_text.replace("##", "")

                    # Clean whitespace
                    tok_text = tok_text.strip()
                    tok_text = " ".join(tok_text.split())
                    orig_text = " ".join(orig_tokens)

                    final_text = nq_get_final_text(tok_text, orig_text, do_lower_case, \
                                                   logger, verbose_logging)


                if final_text in seen_predictions:
                    continue

                nbest.append(
                    _NbestPrediction(
                        text=final_text,
                        logit=pred.logit,
                        no_answer_logit=no_answer_logit))

            # In very rare edge cases we could have no valid predictions. So we
            # just create a nonce prediction in this case to avoid failure.
            if not nbest:
                nbest.append(
                _NbestPrediction(text="empty", logit=0.0, no_answer_logit=no_answer_logit))

            assert len(nbest) >= 1

            total_scores = []
            for entry in nbest:
                total_scores.append(entry.logit)

            probs = _compute_softmax(total_scores)
            nbest_json = []
            for (i, entry) in enumerate(nbest):
                output = collections.OrderedDict()
                output['text'] = entry.text
                output['probability'] = probs[i]
                output['logit'] = entry.logit
                output['no_answer_logit'] = entry.no_answer_logit
                nbest_json.append(output)

            assert len(nbest_json) >= 1
            return nbest_json
        if n_paragraphs is None:
            nbest_json = get_nbest_json(prelim_predictions)
            all_predictions[example.qas_id] = (nbest_json[0]["text"], example.all_answers)
            all_nbest_json[example.qas_id] = nbest_json
        else:
            all_predictions[example.qas_id] = []
            all_nbest_json[example.qas_id] = []
            for n in n_paragraphs:
                nbest_json = get_nbest_json([pred for pred in prelim_predictions if \
                                             pred.paragraph_index<n])
                all_predictions[example.qas_id].append(nbest_json[0]["text"])

    if write_prediction:
        logger.info("Writing predictions to: %s" % (output_prediction_file))
        logger.info("Writing nbest to: %s" % (output_nbest_file))

        with open(output_prediction_file, "w") as writer:
            writer.write(json.dumps(all_predictions, indent=4) + "\n")

        with open(output_nbest_file, "w") as writer:
            writer.write(json.dumps(all_nbest_json, indent=4) + "\n")

    if n_paragraphs is None:
        f1s, ems = [], []
        for prediction, groundtruth in all_predictions.values():
            if len(groundtruth)==0:
                f1s.append(0)
                ems.append(0)
                continue
            f1s.append(max([f1_score(prediction, gt)[0] for gt in groundtruth]))
            ems.append(max([exact_match_score(prediction, gt) for gt in groundtruth]))
        final_f1, final_em = np.mean(f1s), np.mean(ems)
    else:
        f1s, ems = [[] for _ in n_paragraphs], [[] for _ in n_paragraphs]
        for predictions in all_predictions.values():
            groundtruth = predictions[-1]
            predictions = predictions[:-1]
            if len(groundtruth)==0:
                for i in range(len(n_paragraphs)):
                    f1s[i].append(0)
                    ems[i].append(0)
                continue
            for i, prediction in enumerate(predictions):
                f1s[i].append(max([f1_score(prediction, gt)[0] for gt in groundtruth]))
                ems[i].append(max([exact_match_score(prediction, gt) for gt in groundtruth]))
        for n, f1s_, ems_ in zip(n_paragraphs, f1s, ems):
            logger.info("n=%d\tF1 %.2f\tEM %.2f"%(n, np.mean(f1s_)*100, np.mean(ems_)*100))
        final_f1, final_em = np.mean(f1s[-1]), np.mean(ems[-1])
    return final_em, final_f1



def _compute_softmax(scores):
    """Compute softmax probability over raw logits."""
    if not scores:
        return []

    max_score = None
    for score in scores:
        if max_score is None or score > max_score:
            max_score = score

    exp_scores = []
    total_sum = 0.0
    for score in scores:
        x = math.exp(score - max_score)
        exp_scores.append(x)
        total_sum += x

    probs = []
    for score in exp_scores:
        probs.append(score / total_sum)
    return probs


def nq_get_final_text(pred_text, orig_text, do_lower_case, logger, verbose_logging):
    """Project the tokenized prediction back to the original text."""
    def _strip_spaces(text):
        ns_chars = []
        ns_to_s_map = collections.OrderedDict()
        for (i, c) in enumerate(text):
            if c == " ":
                continue
            ns_to_s_map[len(ns_chars)] = i
            ns_chars.append(c)
        ns_text = "".join(ns_chars)
        return (ns_text, ns_to_s_map)

    # We first tokenize `orig_text`, strip whitespace from the result
    # and `pred_text`, and check if they are the same length. If they are
    # NOT the same length, the heuristic has failed. If they are the same
    # length, we assume the characters are one-to-one aligned.
    tokenizer = tokenization.BasicTokenizer(do_lower_case=do_lower_case)

    tok_text = " ".join(tokenizer.tokenize(orig_text))

    start_position = tok_text.find(pred_text)
    if start_position == -1:
        if verbose_logging:
            logger.info(
                "Unable to find text: '%s' in '%s'" % (pred_text, orig_text))
        return orig_text
    end_position = start_position + len(pred_text) - 1

    (orig_ns_text, orig_ns_to_s_map) = _strip_spaces(orig_text)
    (tok_ns_text, tok_ns_to_s_map) = _strip_spaces(tok_text)

    if len(orig_ns_text) != len(tok_ns_text):
        if verbose_logging:
            logger.info("Length not equal after stripping spaces: '%s' vs '%s'",
                            orig_ns_text, tok_ns_text)
        return orig_text

    # We then project the characters in `pred_text` back to `orig_text` using
    # the character-to-character alignment.
    tok_s_to_ns_map = {}
    for (i, tok_index) in six.iteritems(tok_ns_to_s_map):
        tok_s_to_ns_map[tok_index] = i

    orig_start_position = None
    if start_position in tok_s_to_ns_map:
        ns_start_position = tok_s_to_ns_map[start_position]
        if ns_start_position in orig_ns_to_s_map:
            orig_start_position = orig_ns_to_s_map[ns_start_position]

    if orig_start_position is None:
        if verbose_logging:
            logger.info("Couldn't map start position")
        return orig_text

    orig_end_position = None
    if end_position in tok_s_to_ns_map:
        ns_end_position = tok_s_to_ns_map[end_position]
        if ns_end_position in orig_ns_to_s_map:
            orig_end_position = orig_ns_to_s_map[ns_end_position]

    if orig_end_position is None:
        if verbose_logging:
            logger.info("Couldn't map end position")
        return orig_text

    output_text = orig_text[orig_start_position:(orig_end_position + 1)]
    return output_text

