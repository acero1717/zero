# coding: utf-8

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import tensorflow as tf

from utils import util, dtype
from collections import namedtuple
from tensorflow.python.util import nest


class BeamSearchState(namedtuple("BeamSearchState",
                                 ("inputs", "state", "finish"))):
    pass


def beam_search(features, encoding_fn, decoding_fn, params):
    decode_length = params.decode_length
    beam_size = params.beam_size
    alpha = params.decode_alpha
    eos_id = params.tgt_vocab.eos()
    pad_id = params.tgt_vocab.pad()
    num_paral_tokens = max(params.ibdecoder_factor * 2, 1)

    batch_size = tf.shape(features["source"])[0]
    if params.search_mode == "cache":
        model_state = encoding_fn(features["source"])
    else:
        model_state = features["source"]

    src_mask = dtype.tf_to_float(tf.cast(features["source"], tf.bool))
    source_length = tf.reduce_sum(src_mask, -1)
    max_target_length = source_length + decode_length

    model_state = nest.map_structure(
        lambda x: util.expand_tile_dims(x, beam_size, axis=1),
        model_state
    )

    # in our mixed precision mode, we finally convert logits into tf.float32
    # tfdtype = tf.as_dtype(dtype.floatx())
    tfdtype = tf.float32

    # [batch, beam]
    init_log_probs = tf.constant([[0.] + [tfdtype.min] * (beam_size - 1)], dtype=tfdtype)
    init_log_probs = tf.tile(init_log_probs, [batch_size, 1])
    init_scores = tf.zeros_like(init_log_probs)
    # begin-of-sequence
    # IBDecoder: [batch, beam, 1] -> [batch, beam, num_paral_tokens]
    init_seq = tf.fill([batch_size, beam_size, num_paral_tokens], params.tgt_vocab.pad())
    init_finish_seq = tf.zeros_like(init_seq)
    # [batch, beam]
    init_finish_scores = tf.fill([batch_size, beam_size], tfdtype.min)
    init_finish_flags = tf.zeros([batch_size, beam_size], tf.bool)

    def cache_init(prev_seq, state):
        # used to initialize some caches
        # this is because pre-compute these caches is to hard,
        # so let's use one dummy run to obtain them.
        flat_prev_seqs = util.merge_neighbor_dims(prev_seq, axis=0)
        flat_prev_state = nest.map_structure(
            lambda x: util.merge_neighbor_dims(x, axis=0),
            state
        )
        # IBDecoder: 1 -> num_paral_tokens
        _, step_state = decoding_fn(
            flat_prev_seqs[:, -num_paral_tokens:], flat_prev_state, 0)

        new_state = nest.map_structure(
            lambda x: util.unmerge_neighbor_dims(x, batch_size, axis=0),
            step_state
        )
        new_state = util.dict_update(new_state, state)

        return new_state

    if params.search_mode == "cache":
        model_state = cache_init(init_seq, model_state)

    bsstate = BeamSearchState(
        inputs=(init_seq, init_log_probs, init_scores),
        state=model_state,
        finish=(init_finish_seq, init_finish_scores, init_finish_flags)
    )

    def _not_finished(time, bsstate):
        # if the maximum time step is reached, or
        # all samples in one batch satisfy that the worst finished sequence
        # score is not better than the best alive sequence score
        alive_log_probs = bsstate.inputs[1]
        finish_scores = bsstate.finish[1]
        finish_flags = bsstate.finish[2]

        # upper bound of length penality
        max_length_penality = tf.pow(
            (5. + tf.cast(max_target_length, tfdtype)) / 6., alpha)
        best_alive_score = alive_log_probs[:, 0] / max_length_penality

        # minimum score among finished sequences alone
        worst_finish_score = tf.reduce_min(
            finish_scores * tf.cast(finish_flags, tfdtype), 1)
        # deal with unfinished instances, which is set to `tf.float32.min`
        unfinish_mask = 1. - tf.cast(tf.reduce_any(finish_flags, 1), tfdtype)
        worst_finish_score += unfinish_mask * tfdtype.min

        # boundary
        bound_is_met = tf.reduce_all(tf.greater(worst_finish_score,
                                                best_alive_score))

        # length constraint
        length_is_met = tf.reduce_any(
            tf.less(time, tf.cast(max_target_length, tf.int32)))

        return tf.logical_and(tf.logical_not(bound_is_met), length_is_met)

    def _step_fn(time, bsstate):
        """one expansion step of beam search process"""

        # 1. feed previous predictions, and get the next probabilities
        # generating beam * vocab_size predictions
        prev_seq, prev_log_probs, prev_scores = bsstate.inputs

        flat_prev_seqs = util.merge_neighbor_dims(prev_seq, axis=0)
        flat_prev_state = nest.map_structure(
            lambda x: util.merge_neighbor_dims(x, axis=0),
            bsstate.state
        )

        # curr_logits: [batch * beam, vocab_size]
        if params.search_mode == "cache":
            # IBDecoder: 1 -> num_paral_tokens
            decode_target = flat_prev_seqs[:, -num_paral_tokens:]
        else:
            # introducing `dev` mode into search function
            # this mainly is for model developing, because when developing new models
            #  perhaps your new model is very complex, with complex internal dependencies
            #  at this time, maintaining the cache state is rather boring and usually make
            #  mistakes. To this end, I add the dev mode, that the model only uses
            #  source sentence and partial target sentence at the cost of slower decoding.
            # Definitely disabled if you want higher decoding efficiency.
            # IBDecoder: 1 -> num_paral_tokens
            decode_target = tf.pad(
                flat_prev_seqs[:, num_paral_tokens:], [[0, 0], [0, num_paral_tokens]], constant_values=1)
        # Step_Logits: [batch * beam, num_paral_tokens, vocab_size]
        step_logits, step_state = decoding_fn(
            decode_target, flat_prev_state, time)
        # add gumbel noise into the logits, simulate gumbel top-k sampling without replacement
        if params.enable_noise_beam_search:
            step_logits += util.gumbel_noise(util.shape_list(step_logits))
        # apply temperature decoding
        step_logits /= params.beam_search_temperature
        step_log_probs = util.log_prob_from_logits(step_logits)
        vocab_size = util.shape_list(step_log_probs)[-1]

        # force decoding
        eos_mask = tf.cast(tf.equal(tf.range(vocab_size), eos_id), tfdtype)
        step_log_probs = tf.cond(dtype.tf_to_float(time) < dtype.tf_to_float(1.),
                                 lambda: step_log_probs + tf.expand_dims(eos_mask, 0) * - dtype.inf(),
                                 lambda: step_log_probs)

        # expand to [batch, beam, vocab_size]
        step_log_probs = util.unmerge_neighbor_dims(step_log_probs,
                                                    batch_size, axis=0)
        step_state = nest.map_structure(
            lambda x: util.unmerge_neighbor_dims(x, batch_size, axis=0),
            step_state
        )

        # We adopt an approximated decoding algorithm, only considering top-sqrt(V) predictions in each step
        # Our primary testing suggests that setting sqrt_V to beam_size is big enough
        if beam_size > 1:
            sqrt_V = beam_size

            ##### IBDecoder,
            # step 1: get top sqrt_V (beam_size) predictions
            # [batch, beam, num_paral_tokens, sqrt_V]
            total_step_log_probs, total_step_log_indices = tf.nn.top_k(step_log_probs, sqrt_V)

            # step 2: recurrently perform outer vector addition, avoiding memory issues
            # collect top-K predictions with their id indices
            step_log_indices = []
            sub_step_log_prob = tf.expand_dims(total_step_log_probs[:, :, 0], -1) + \
                                tf.expand_dims(total_step_log_probs[:, :, 1], -2)
            sub_step_log_prob = tf.reshape(sub_step_log_prob, [batch_size, beam_size, sqrt_V * sqrt_V])

            for npt_idx in range(2, num_paral_tokens):
                step_log_prob, step_log_index = tf.nn.top_k(sub_step_log_prob, sqrt_V)
                step_log_prob = tf.expand_dims(step_log_prob, -1) + tf.expand_dims(total_step_log_probs[:, :, npt_idx], -2)
                sub_step_log_prob = tf.reshape(step_log_prob, [batch_size, beam_size, sqrt_V * sqrt_V])

                step_log_indices.append(step_log_index)

            step_log_probs = sub_step_log_prob
            ##### IBDecoder

            # 2. compute top-k scored next predictions
            # reducing beam * vocab_size to 2 * beam
            # [batch, beam, 1] + [batch, beam, sqrt_V * sqrt_V]
            curr_log_probs = tf.expand_dims(prev_log_probs, 2) + step_log_probs
            # IBDecoder: 1 -> num_paral_tokens
            length_penality = tf.pow((5.0 + tf.cast(time + num_paral_tokens, tfdtype)) / 6., alpha)
            curr_scores = curr_log_probs / length_penality

            # [batch, beam * sqrt_V * sqrt_V]
            curr_flat_scores = util.merge_neighbor_dims(curr_scores, axis=1)
            # [batch, 2 * beam]
            topk_scores, topk_indices = tf.nn.top_k(
                curr_flat_scores, 2 * beam_size)

            # index manipulation, [batch, 2 * beam]
            curr_beam_indices = topk_indices // (sqrt_V * sqrt_V)
            curr_symbol_indices = topk_indices % (sqrt_V * sqrt_V)
            beam2_pos = util.batch_coordinates(batch_size, 2 * beam_size)
            curr_coordinates = tf.stack([beam2_pos, curr_beam_indices], axis=2)

            # IBDecoder: based on the coordination of selected beams, extract corresponding word indices
            # [batch, beam, sqrt_V] => [batch, 2 * beam, sqrt_V]
            word_indices = [tf.gather_nd(v, curr_coordinates) for v in step_log_indices]
            word_indices_ths = tf.gather_nd(total_step_log_indices, curr_coordinates)

            beam2_range = util.expand_tile_dims(tf.range(2 * beam_size, dtype=tf.int32), batch_size, axis=0)

            # IBDecoder: backward tracing, collect predicted words and indices
            # [batch, 2 * beam]
            step_symbol_indices = curr_symbol_indices
            step_predict_words = []
            for npt_idx in range(1, num_paral_tokens-1):
                step_symbol_indices_A = step_symbol_indices // sqrt_V
                step_symbol_indices_B = step_symbol_indices % sqrt_V

                bbw_corrdinates = tf.stack([beam2_pos, beam2_range, step_symbol_indices_A], axis=2)
                step_symbol_indices = tf.gather_nd(word_indices[-npt_idx], bbw_corrdinates)
                bbw_corrdinates_th = tf.stack([beam2_pos, beam2_range, step_symbol_indices_B], axis=2)
                npt_idx_word = tf.gather_nd(word_indices_ths[:, :, -npt_idx], bbw_corrdinates_th)

                step_predict_words.append(npt_idx_word)

            # [batch, 2 * beam]
            step_symbol_indices_A = step_symbol_indices // sqrt_V
            step_symbol_indices_B = step_symbol_indices % sqrt_V
            # [batch, 2 * beam]
            bbw_corrdinates_1th = tf.stack([beam2_pos, beam2_range, step_symbol_indices_A], axis=2)
            word_1th = tf.gather_nd(word_indices_ths[:, :, 0], bbw_corrdinates_1th)
            bbw_corrdinates_2th = tf.stack([beam2_pos, beam2_range, step_symbol_indices_B], axis=2)
            word_2th = tf.gather_nd(word_indices_ths[:, :, 1], bbw_corrdinates_2th)

            # IBDecoder: the final top-beam size word prediction
            step_predict_words.extend([word_2th, word_1th])
            step_predict_words = tf.stack(step_predict_words[::-1], axis=2)

            # extract candidate sequences
            # [batch, 2 * beam, time + 1]
            curr_seq = tf.gather_nd(prev_seq, curr_coordinates)
            curr_seq = tf.concat([curr_seq, step_predict_words], 2)

            # 3. handling alive sequences
            # reducing 2 * beam to beam
            curr_fin_flags = tf.logical_or(
                # IBDecoder: among any predicted words, reach the end of this beam if any of them is eos
                tf.reduce_any(tf.equal(step_predict_words, eos_id), axis=-1),
                # if time step exceeds the maximum decoding length, should stop
                tf.expand_dims(
                    tf.greater_equal(time, tf.cast(max_target_length, tf.int32)), 1)
            )
            alive_scores = topk_scores + tf.cast(curr_fin_flags, tfdtype) * tfdtype.min
            # [batch, 2 * beam] -> [batch, beam]
            alive_scores, alive_indices = tf.nn.top_k(alive_scores, beam_size)
            beam_pos = util.batch_coordinates(batch_size, beam_size)
            alive_coordinates = tf.stack([beam_pos, alive_indices], axis=2)
            alive_seq = tf.gather_nd(curr_seq, alive_coordinates)
            alive_beam_indices = tf.gather_nd(curr_beam_indices, alive_coordinates)
            beam_coordinates = tf.stack([beam_pos, alive_beam_indices], axis=2)
            alive_state = nest.map_structure(
                lambda x: tf.gather_nd(x, beam_coordinates),
                step_state
            )
            alive_log_probs = alive_scores * length_penality

            # 4. handle finished sequences
            # reducing 3 * beam to beam
            prev_fin_seq, prev_fin_scores, prev_fin_flags = bsstate.finish
            # [batch, 2 * beam]
            curr_fin_scores = topk_scores + (1.0 - tf.cast(curr_fin_flags, tfdtype)) * tfdtype.min
            # [batch, 3 * beam]
            fin_flags = tf.concat([prev_fin_flags, curr_fin_flags], axis=1)
            fin_scores = tf.concat([prev_fin_scores, curr_fin_scores], axis=1)
            # [batch, beam]
            fin_scores, fin_indices = tf.nn.top_k(fin_scores, beam_size)
            fin_coordinates = tf.stack([beam_pos, fin_indices], axis=2)
            fin_flags = tf.gather_nd(fin_flags, fin_coordinates)
            pad_seq = tf.fill([batch_size, beam_size, num_paral_tokens],
                              tf.constant(pad_id, tf.int32))
            prev_fin_seq = tf.concat([prev_fin_seq, pad_seq], axis=2)
            fin_seq = tf.concat([prev_fin_seq, curr_seq], axis=1)
            fin_seq = tf.gather_nd(fin_seq, fin_coordinates)

        else:
            # IBDecoder: specific solution for greedy decoding, in this case, no worries about top-k handling
            # top-beam size is the final prediction, directly
            # [batch, beam, num_paral_tokens] ::: [batch, 1, num_paral_tokens]
            step_log_probs, step_log_indices = tf.nn.top_k(step_log_probs, 1)
            step_log_probs = tf.squeeze(step_log_probs, -1)
            step_log_indices = tf.squeeze(step_log_indices, -1)

            step_log_probs = tf.reduce_sum(step_log_probs, -1)

            curr_log_probs = prev_log_probs + step_log_probs
            length_penality = tf.pow((5.0 + tf.cast(time + num_paral_tokens, tfdtype)) / 6., alpha)
            curr_scores = curr_log_probs / length_penality

            curr_seq = tf.concat([prev_seq, step_log_indices], 2)
            words = step_log_indices

            # 3. handling alive sequences
            # reducing 2 * beam to beam
            curr_fin_flags = tf.logical_or(
                tf.reduce_any(tf.equal(words, eos_id), axis=-1),
                # if time step exceeds the maximum decoding length, should stop
                tf.expand_dims(
                    tf.greater_equal(time, tf.cast(max_target_length, tf.int32)), 1)
            )

            alive_scores = curr_scores + tf.cast(curr_fin_flags, tfdtype) * tfdtype.min
            alive_log_probs = alive_scores * length_penality

            # 4. handle finished sequences
            # reducing 3 * beam to beam
            prev_fin_seq, prev_fin_scores, prev_fin_flags = bsstate.finish
            # [batch, 2 * beam]
            curr_fin_scores = curr_scores + (1.0 - tf.cast(curr_fin_flags, tfdtype)) * tfdtype.min
            # [batch, 3 * beam]
            fin_flags = tf.logical_or(prev_fin_flags, curr_fin_flags)
            fin_scores = tf.maximum(prev_fin_scores, curr_fin_scores)
            fin_seq = curr_seq
            alive_seq = curr_seq
            alive_state = step_state

        next_state = BeamSearchState(
            inputs=(alive_seq, alive_log_probs, alive_scores),
            state=alive_state,
            finish=(fin_seq, fin_scores, fin_flags)
        )

        # IBDecoder: 1->num_paral_tokens
        # each decoding step produces `num_paral_tokens` tokens, note time denotes the number of tokens produced so far
        return time + num_paral_tokens, next_state

    time = tf.constant(0, tf.int32, name="time")
    shape_invariants = BeamSearchState(
        inputs=(tf.TensorShape([None, None, None]),
                tf.TensorShape([None, None]),
                tf.TensorShape([None, None])),
        state=nest.map_structure(
            lambda x: util.get_shape_invariants(x),
            bsstate.state
        ),
        finish=(tf.TensorShape([None, None, None]),
                tf.TensorShape([None, None]),
                tf.TensorShape([None, None]))
    )
    outputs = tf.while_loop(_not_finished, _step_fn, [time, bsstate],
                            shape_invariants=[tf.TensorShape([]),
                                              shape_invariants],
                            parallel_iterations=32,
                            back_prop=False)
    final_state = outputs[1]

    alive_seqs = final_state.inputs[0]
    init_scores = final_state.inputs[2]
    final_seqs = final_state.finish[0]
    final_scores = final_state.finish[1]
    final_flags = final_state.finish[2]

    alive_seqs.set_shape([None, beam_size, None])
    final_seqs.set_shape([None, beam_size, None])

    final_seqs = tf.where(tf.reduce_any(final_flags, 1), final_seqs,
                          alive_seqs)
    final_scores = tf.where(tf.reduce_any(final_flags, 1), final_scores,
                            init_scores)

    # IBDecoder: for the final prediction, skip the first-num_paral_tokens paddings
    return {
        'seq': final_seqs[:, :, num_paral_tokens:],
        'score': final_scores
    }
