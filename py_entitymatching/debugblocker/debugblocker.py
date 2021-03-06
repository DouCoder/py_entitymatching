import cloudpickle
import heapq as hq
import logging
import numpy
import multiprocessing
import os
import pickle
import pandas as pd
import py_entitymatching as mg
import py_entitymatching.catalog.catalog_manager as cm
import py_entitymatching as em
import sys
import time

from array import array
from collections import namedtuple
from joblib import Parallel, delayed
from operator import attrgetter
from py_entitymatching.debugblocker.debugblocker_cython import \
        debugblocker_cython, debugblocker_config_cython, debugblocker_topk_cython, debugblocker_merge_topk_cython
from py_entitymatching.utils.validation_helper import validate_object_type

logger = logging.getLogger(__name__)

SELECTED_FIELDS_UPPER_BOUND = 8


def debug_blocker(candidate_set, ltable, rtable, output_size=200,
        attr_corres=None, verbose=True, n_jobs=1, n_configs=1):
    """
    This function debugs the blocker output and reports a list of potential
    matches that are discarded by a blocker (or a blocker sequence).
    Specifically,  this function takes in the two input tables for
    matching and the candidate set returned by a blocker (or a blocker
    sequence), and produces a list of tuple pairs which are rejected by the
    blocker but with high potential of being true matches.
    
    Args:
        candidate_set (DataFrame): The candidate set generated by
            applying the blocker on the ltable and rtable.
        ltable,rtable (DataFrame): The input DataFrames that are used to
            generate the blocker output.
        output_size (int): The number of tuple pairs that will be
            returned (defaults to 200).
        attr_corres (list): A list of attribute correspondence tuples.
            When ltable and rtable have different schemas, or the same
            schema but different words describing the attributes, the
            user needs to manually specify the attribute correspondence.
            Each element in this list should be a tuple of strings
            which are the corresponding attributes in ltable and rtable.
            The default value is None, and if the user doesn't specify
            this list, a built-in function for finding the
            attribute correspondence list will be called. But we highly
            recommend the users manually specify the attribute
            correspondences, unless the schemas of ltable and rtable are
            identical (defaults to None).
        verbose (boolean):  A flag to indicate whether the debug information
         should be logged (defaults to False).
        n_jobs (int): The number of parallel jobs to be used for computation
            (defaults to 1). If -1 all CPUs are used. If 0 or 1,
            no parallel computation is used at all, which is useful for
            debugging. For n_jobs below -1, (n_cpus + 1 + n_jobs) are
            used (where n_cpus are the total number of CPUs in the
            machine).Thus, for n_jobs = -2, all CPUs but one are used.
            If (n_cpus + 1 + n_jobs) is less than 1, then no parallel
            computation is used (i.e., equivalent to the default).
        n_configs (int): The maximum number of configs to be used for 
            calculating the topk list(defaults to 1). If -1, the config
            number is set as the number of cpu. If -2, all configs are used. 
            if n_configs is less than the maximum number of generated configs, 
            then n_configs will be used. Otherwise, all the generated configs
            will be used.
    Returns:
        A pandas DataFrame with 'output_size' number of rows. Each row in the
        DataFrame is a tuple pair which has potential of being a true
        match, but is rejected by the blocker (meaning that the tuple
        pair is in the Cartesian product of ltable and rtable subtracted
        by the candidate set). The fields in the returned DataFrame are
        from ltable and rtable, which are useful for determining similar
        tuple pairs.
    Raises:
        AssertionError: If `ltable`, `rtable` or `candset` is not of type
            pandas DataFrame.
        AssertionError: If `ltable` or `rtable` is empty (size of 0).
        AssertionError: If the output `size` parameter is less than or equal
            to 0.
        AssertionError: If the attribute correspondence (`attr_corres`) list is
            not in the correct format (a list of tuples).
        AssertionError: If the attribute correspondence (`attr_corres`)
            cannot be built correctly.
    Examples:
        >>> import py_entitymatching as em
        >>> ob = em.OverlapBlocker()
        >>> C = ob.block_tables(A, B, l_overlap_attr='title', r_overlap_attr='title', overlap_size=3)
        >>> corres = [('ID','ssn'), ('name', 'ename'), ('address', 'location'),('zipcode', 'zipcode')]
        >>> D = em.debug_blocker(C, A, B, attr_corres=corres)
        >>> import py_entitymatching as em
        >>> ob = em.OverlapBlocker()
        >>> C = ob.block_tables(A, B, l_overlap_attr='name', r_overlap_attr='name', overlap_size=3)
        >>> D = em.debug_blocker(C, A, B, output_size=150)
    """
    # Check input types.
    _validate_types(ltable, rtable, candidate_set, output_size,
                    attr_corres, verbose)

    # Basic checks.
    # Check table size.
    if len(ltable) == 0:
        raise AssertionError('Error: ltable is empty!')
    if len(rtable) == 0:
        raise AssertionError('Error: rtable is empty!')

    # Check the value of output size.
    if output_size <= 0:
        raise AssertionError('The input parameter: \'pred_list_size\''
                            ' is less than or equal to 0. Nothing needs'
                            ' to be done!')

    # get metadata
    l_key, r_key = cm.get_keys_for_ltable_rtable(ltable, rtable, logger, verbose)

    # validate metadata
    cm._validate_metadata_for_table(ltable, l_key, 'ltable', logger, verbose)
    cm._validate_metadata_for_table(rtable, r_key, 'rtable', logger, verbose)

    # Check the user input field correst list (if exists) and get the raw
    # version of our internal correst list.
    _check_input_field_correspondence_list(ltable, rtable, attr_corres)
    corres_list = _get_field_correspondence_list(ltable, rtable,
                                                l_key, r_key, attr_corres)

    # Build the (col_name: col_index) dict to speed up locating a field in
    # the schema.
    ltable_col_dict = _build_col_name_index_dict(ltable)
    rtable_col_dict = _build_col_name_index_dict(rtable)

    # Filter correspondence list to remove numeric types. We only consider
    # string types for document concatenation.
    _filter_corres_list(ltable, rtable, l_key, r_key,
                       ltable_col_dict, rtable_col_dict, corres_list)

    # Get field filtered new table.
    ltable_filtered, rtable_filtered = _get_filtered_table(
        ltable, rtable, corres_list)

    # Select a subset of fields with high scores
    feature_list = _select_features(ltable_filtered, rtable_filtered, l_key, r_key)

    if len(feature_list) == 0:
        raise AssertionError('\nError: the selected field list is empty,'
                            ' nothing could be done! Please check if all'
                            ' table fields are numeric types.')

    # Map the record key value to its index in the table
    lrecord_id_to_index_map = _build_id_to_index_map(ltable_filtered, l_key)
    rrecord_id_to_index_map = _build_id_to_index_map(rtable_filtered, r_key)

    # Build the tokenized record list delimited by a white space on the
    # selected fields.
    lrecord_list = _get_tokenized_table(ltable_filtered, l_key, feature_list)
    rrecord_list = _get_tokenized_table(rtable_filtered, r_key, feature_list)

    # Build the token order according to token's frequency. To run a
    # prefix filtering based similarity join algorithm, we first need
    # the global token order. 
    order_dict, token_index_dict = _build_global_token_order(
        lrecord_list, rrecord_list)


    # Sort the token in each record by the global order.
    _replace_token_with_numeric_index(lrecord_list, order_dict)
    _replace_token_with_numeric_index(rrecord_list, order_dict)

    _sort_record_tokens_by_global_order(lrecord_list)
    _sort_record_tokens_by_global_order(rrecord_list)

    lrecord_token_list, lrecord_index_list =\
                            _split_record_token_and_index(lrecord_list)
    rrecord_token_list, rrecord_index_list =\
                            _split_record_token_and_index(rrecord_list)

    del lrecord_list
    del rrecord_list

    # Reformat the candidate set from a dataframe to a list of record index
    # tuple pair.
    new_formatted_candidate_set = _index_candidate_set(
        candidate_set, lrecord_id_to_index_map, rrecord_id_to_index_map, verbose)

    
    ltable_field_length_list = _calc_table_field_length(lrecord_index_list, len(feature_list))
    rtable_field_length_list = _calc_table_field_length(rrecord_index_list, len(feature_list))

    ltable_field_token_sum = _calc_table_field_token_sum(ltable_field_length_list, len(feature_list))
    rtable_field_token_sum = _calc_table_field_token_sum(rtable_field_length_list, len(feature_list))


    rec_list = debugblocker_cython_parallel(lrecord_token_list, rrecord_token_list,
                        lrecord_index_list, rrecord_index_list,
                        ltable_field_token_sum, rtable_field_token_sum,
                        new_formatted_candidate_set, len(feature_list),
                        output_size, n_jobs, n_configs)

    ret_dataframe = _assemble_topk_table(rec_list[0:output_size], ltable_filtered, rtable_filtered, l_key, r_key)
    return ret_dataframe

def debugblocker_topk_cython_wrapper(config, lrecord_token_list,
        rrecord_token_list, lrecord_index_list, rrecord_index_list, py_cand_set,
        py_output_size):
    # deserialize data    
    lrecord_token_list = pickle.loads(lrecord_token_list)
    rrecord_token_list = pickle.loads(rrecord_token_list)
    lrecord_index_list = pickle.loads(lrecord_index_list)
    rrecord_index_list = pickle.loads(rrecord_index_list)

    return debugblocker_topk_cython(config, lrecord_token_list, rrecord_token_list,
    lrecord_index_list, rrecord_index_list, py_cand_set,
    py_output_size)

def debugblocker_cython_parallel(lrecord_token_list, rrecord_token_list,
                        lrecord_index_list, rrecord_index_list,
                        ltable_field_token_sum, rtable_field_token_sum, py_cand_set,
                        py_num_fields, py_output_size, n_jobs, n_configs):

    # pickle list of list to accelate in multi-process
    lrecord_token_list = pickle.dumps(lrecord_token_list)
    rrecord_token_list = pickle.dumps(rrecord_token_list)
    lrecord_index_list = pickle.dumps(lrecord_index_list)
    rrecord_index_list = pickle.dumps(rrecord_index_list)

    # generate config lists

    py_config_lists = debugblocker_config_cython(ltable_field_token_sum, rtable_field_token_sum, 
                            py_num_fields, len(lrecord_token_list), len(rrecord_token_list))

    n_configs = _get_config_num(n_jobs, n_configs, len(py_config_lists))
    # parallel computer topk based on config lists
    rec_lists = Parallel(n_jobs=n_jobs)(delayed(debugblocker_topk_cython_wrapper)
        (py_config_lists[i], lrecord_token_list, rrecord_token_list,
        lrecord_index_list, rrecord_index_list, py_cand_set,
        py_output_size) for i in range(n_configs))

    py_rec_list = debugblocker_merge_topk_cython(rec_lists)
    
    return py_rec_list

# get the number of configs according the input value of n_configs
def _get_config_num(n_jobs, n_configs, n_total_configs):
    if n_jobs == 0 or n_configs == 0 or n_configs < -2 :
      raise ValueError('n_jobs != 0 && n_configs != 0 && n_configs >= -2')

    n_cpus = multiprocessing.cpu_count()
    if n_configs == -2 :
        n_configs = n_total_configs
    elif n_configs == -1 :
        # set n_configs as the number of the cpu cores
        if n_jobs < 0 :
          n_configs = n_cpus + 1 + n_jobs
        else:
          n_configs = n_jobs

    if n_configs < n_total_configs:
        return n_configs
    else:
        return n_total_configs

# Validate the types of input parameters.
def _validate_types(ltable, rtable, candidate_set, output_size,
                    attr_corres, verbose):
    validate_object_type(ltable, pd.DataFrame, 'Input left table')

    validate_object_type(rtable, pd.DataFrame, 'Input right table')

    validate_object_type(candidate_set, pd.DataFrame, 'Input candidate set')

    validate_object_type(output_size, int, 'Output size')

    if attr_corres is not None:
        if not isinstance(attr_corres, list):
            logging.error('Input attribute correspondence is not of'
                          ' type list')
            raise AssertionError('Input attribute correspondence is'
                                 ' not of type list')

        for pair in attr_corres:
            if not isinstance(pair, tuple):
                logging.error('Pair in attribute correspondence list is not'
                              ' of type tuple')
                raise AssertionError('Pair in attribute correspondence list'
                                     ' is not of type tuple')

    if not isinstance(verbose, bool):
        logger.error('Parameter verbose is not of type bool')
        raise AssertionError('Parameter verbose is not of type bool')


def _calc_table_field_length(record_index_list, num_field):
    table_field_length_list = []
    for i in range(len(record_index_list)):
        field_array = []
        for j in range(num_field):
            field_array.append(0)
        field_array = array('I', field_array)
        for j in range(len(record_index_list[i])):
            if (record_index_list[i][j] >= num_field):
                raise AssertionError('index should less than num_field')
            field_array[record_index_list[i][j]] += 1
        table_field_length_list.append(field_array)

    return table_field_length_list


def _calc_table_field_token_sum(table_field_length_list, num_field):
    table_field_token_sum = []
    for i in range(num_field):
        table_field_token_sum.append(0)
    for i in range(len(table_field_length_list)):
        for j in range(len(table_field_length_list[i])):
            table_field_token_sum[j] += table_field_length_list[i][j]

    return table_field_token_sum


def _check_input_field_correspondence_list(ltable, rtable, field_corres_list):
    if field_corres_list is None or len(field_corres_list) == 0:
        return
    true_ltable_fields = list(ltable.columns)
    true_rtable_fields = list(rtable.columns)
    for pair in field_corres_list:
        if type(pair) != tuple or len(pair) != 2:
            raise AssertionError('Error in checking user input field'
                                 ' correspondence: pair \'%s\' in not in the'
                                 'tuple format!' % (pair))

    given_ltable_fields = [field[0] for field in field_corres_list]
    given_rtable_fields = [field[1] for field in field_corres_list]
    for given_field in given_ltable_fields:
        if given_field not in true_ltable_fields:
            raise AssertionError('Error in checking user input field'
                                 ' correspondence: the field \'%s\' is'
                                 ' not in the ltable!' % (given_field))
    for given_field in given_rtable_fields:
        if given_field not in true_rtable_fields:
            raise AssertionError('Error in checking user input field'
                                 ' correspondence:'
                                 ' the field \'%s\' is not in the'
                                 ' rtable!' % (given_field))
    return


def _get_field_correspondence_list(ltable, rtable, lkey, rkey, attr_corres):
    corres_list = []
    if attr_corres is None or len(attr_corres) == 0:
        corres_list = mg.get_attr_corres(ltable, rtable)['corres']
        if len(corres_list) == 0:
            raise AssertionError('Error: the field correspondence list'
                                 ' is empty. Please specify the field'
                                 ' correspondence!')
    else:
        for tu in attr_corres:
            corres_list.append(tu)

    key_pair = (lkey, rkey)
    if key_pair not in corres_list:
        corres_list.append(key_pair)

    return corres_list

# Filter the correspondence list. Remove the fields in numeric types.
def _filter_corres_list(ltable, rtable, ltable_key, rtable_key,
                       ltable_col_dict, rtable_col_dict, corres_list):
    ltable_dtypes = list(ltable.dtypes)
    rtable_dtypes = list(rtable.dtypes)
    for i in reversed(range(len(corres_list))):
        lcol_name = corres_list[i][0]
        rcol_name = corres_list[i][1]
        # Filter the pair where both fields are numeric types.
        if ltable_dtypes[ltable_col_dict[lcol_name]] != numpy.dtype('O') \
                and rtable_dtypes[rtable_col_dict[rcol_name]] != numpy.dtype('O'):
            if lcol_name != ltable_key and rcol_name != rtable_key:
                corres_list.pop(i)

    if len(corres_list) == 1 and corres_list[0][0] == ltable_key\
                             and corres_list[0][1] == rtable_key:
        raise AssertionError('The field correspondence list is empty after'
                            ' filtering: please verify your correspondence'
                            ' list, or check if each field is of numeric'
                            ' type!')


# Filter the original input tables according to the correspondence list.
# The filtered tables will only contain the fields in the correspondence list.
def _get_filtered_table(ltable, rtable, corres_list):
    ltable_cols = [col_pair[0] for col_pair in corres_list]
    rtable_cols = [col_pair[1] for col_pair in corres_list]
    lfiltered_table = ltable[ltable_cols]
    rfiltered_table = rtable[rtable_cols]
    return lfiltered_table, rfiltered_table


def _build_col_name_index_dict(table):
    col_dict = {}
    col_names = list(table.columns)
    for i in range(len(col_names)):
        col_dict[col_names[i]] = i
    return col_dict


# Select the most important fields for similarity join. The importance
# of a fields is measured by the combination of field value uniqueness
# and non-emptyness.
def _select_features(ltable, rtable, lkey, rkey):
    lcolumns = list(ltable.columns)
    rcolumns = list(rtable.columns)
    lkey_index = -1
    rkey_index = -1
    if len(lcolumns) != len(rcolumns):
        raise AssertionError('Error: FILTERED ltable and FILTERED rtable'
                            ' have different number of fields!')
    for i in range(len(lcolumns)):
        if lkey == lcolumns[i]:
            lkey_index = i
    if lkey_index < 0:
        raise AssertionError('Error: cannot find key in the FILTERED'
                            ' ltable schema!')
    for i in range(len(rcolumns)):
        if rkey == rcolumns[i]:
            rkey_index = i
    if rkey_index < 0:
        raise AssertionError('Error: cannot find key in the FILTERED'
                            ' rtable schema!')

    lweight = _get_feature_weight(ltable)
    rweight = _get_feature_weight(rtable)
    if len(lweight) != len(rweight):
        raise AssertionError('Error: ltable and rtable don\'t have the'
                            ' same schema')

    Rank = namedtuple('Rank', ['index', 'weight'])
    rank_list = []
    for i in range(len(lweight)):
        rank_list.append(Rank(i, lweight[i] * rweight[i]))
    if lkey_index == rkey_index:
        rank_list.pop(lkey_index)
    else:
        # Make sure we remove the index with larger value first!!!
        if lkey_index > rkey_index:
            rank_list.pop(lkey_index)
            rank_list.pop(rkey_index)
        else:
            rank_list.pop(rkey_index)
            rank_list.pop(lkey_index)

    rank_list = sorted(rank_list, key=attrgetter('weight'), reverse=True)
    rank_index_list = []
    num_selected_fields = 0

    if len(rank_list) < SELECTED_FIELDS_UPPER_BOUND:
        num_selected_fields = len(rank_list)
    else:
        num_selected_fields = SELECTED_FIELDS_UPPER_BOUND

    for i in range(num_selected_fields):
        rank_index_list.append(rank_list[i].index)

    return rank_index_list


# Calculate the importance (weight) for each field in a table.
def _get_feature_weight(table):
    num_records = len(table)
    if num_records == 0:
        raise AssertionError('Error: empty table!')
    weight = []
    for col in table.columns:
        value_set = set()
        non_empty_count = 0
        col_values = table[col]
        for value in col_values:
            if not pd.isnull(value) and value != '':
                value_set.add(value)
                non_empty_count += 1
        selectivity = 0.0
        if non_empty_count != 0:
            selectivity = len(value_set) * 1.0 / non_empty_count
        non_empty_ratio = non_empty_count * 1.0 / num_records


        # The field weight is the combination of non-emptyness
        # and uniqueness.
        weight.append(non_empty_ratio + selectivity)
    return weight


# Build the mapping of record key value and its index in the table.
def _build_id_to_index_map(table, table_key):
    record_id_to_index = {}
    id_col = list(table[table_key])
    for i in range(len(id_col)):
        # id_col[i] = str(id_col[i])
        if id_col[i] in record_id_to_index:
            raise AssertionError('record_id is already in record_id_to_index')
        record_id_to_index[id_col[i]] = i
    return record_id_to_index



# Tokenize a table. First tokenize each table column by a white space,
# then concatenate the column of each record. The reason for tokenizing
# columns first is that it's more efficient than iterate each dataframe
# tuple.
def _get_tokenized_table(table, table_key, feature_list):
    record_list = []
    columns = table.columns[feature_list]
    tmp_table = []
    for col in columns:
        column_token_list = _get_tokenized_column(table[col])
        tmp_table.append(column_token_list)

    num_records = len(table[table_key])
    for i in range(num_records):
        token_list = []
        index_map = {}

        for j in range(len(columns)):
            tmp_col_tokens = tmp_table[j][i]
            for token in tmp_col_tokens:
                if token != '':
                    if token in index_map:
                        token_list.append((token + '_' + str(index_map[token]), j))
                        index_map[token] += 1
                    else:
                        token_list.append((token, j))
                        index_map[token] = 1
        record_list.append(token_list)

    return record_list


# Tokenize each table column by white spaces.
def _get_tokenized_column(column):
    column_token_list = []
    for value in list(column):
        tmp_value = _replace_nan_to_empty(value)
        if tmp_value != '':
            tmp_list = list(tmp_value.lower().split(' '))
            column_token_list.append(tmp_list)
        else:
            column_token_list.append([''])
    return column_token_list


# Check the value of each field. Replace nan with empty string
# Cast floats into integers.
def _replace_nan_to_empty(field):
    if pd.isnull(field):
        return ''
    elif type(field) in [float, numpy.float64, int, numpy.int64]:
        return str('{0:.0f}'.format(field))
    else:
        return str(field)


# Reformat the input candidate set. Since the input format is DataFrame,
# it's difficult for us to know if a tuple pair is in the candidate
# set or not. We will use the reformatted candidate set in the topk
# similarity join.
def _index_candidate_set(candidate_set, lrecord_id_to_index_map, rrecord_id_to_index_map, verbose):
    if len(candidate_set) == 0:
        return {}
    new_formatted_candidate_set = {}
    
    # Get metadata
    key, fk_ltable, fk_rtable, ltable, rtable, l_key, r_key = \
        cm.get_metadata_for_candset(candidate_set, logger, verbose)

    # Validate metadata
    cm._validate_metadata_for_candset(candidate_set, key, fk_ltable, fk_rtable, ltable, rtable, l_key, r_key,
                                      logger, verbose)

    ltable_key_data = list(candidate_set[fk_ltable])
    rtable_key_data = list(candidate_set[fk_rtable])

    for i in range(len(ltable_key_data)):
        if ltable_key_data[i] in lrecord_id_to_index_map and \
                        rtable_key_data[i] in rrecord_id_to_index_map:
            l_key_data = lrecord_id_to_index_map[ltable_key_data[i]]
            r_key_data = rrecord_id_to_index_map[rtable_key_data[i]]
            if l_key_data in new_formatted_candidate_set:
                new_formatted_candidate_set[l_key_data].add(r_key_data)
            else:
                new_formatted_candidate_set[l_key_data] = {r_key_data}

    return new_formatted_candidate_set


# Build the global order of tokens in the table by frequency.
def _build_global_token_order(lrecord_list, rrecord_list):
    freq_order_dict = {}
    _build_global_token_order_impl(lrecord_list, freq_order_dict)
    _build_global_token_order_impl(rrecord_list, freq_order_dict)
    token_list = []
    for token in freq_order_dict:
        token_list.append(token)
    token_list = sorted(token_list, key=lambda x: (freq_order_dict[x], x))

    order_dict = {}
    token_index_dict = {}
    for i in range(len(token_list)):
        order_dict[token_list[i]] = i
        token_index_dict[i] = token_list[i]

    return order_dict, token_index_dict


# Implementation of building the global order of tokens in the table by frenqucy. 
def _build_global_token_order_impl(record_list, order_dict):
    for record in record_list:
        for tup in record:
            token = tup[0]
            if token in order_dict:
                order_dict[token] += 1
            else:
                order_dict[token] = 1


def _replace_token_with_numeric_index(record_list, order_dict):
    for i in range(len(record_list)):
        tmp_record = []
        for tup in record_list[i]:
            token = tup[0]
            index = tup[1]
            if token in order_dict:
                tmp_record.append((order_dict[token], index))
        record_list[i] = tmp_record

# Sort each tokenized record by the global token order.
def _sort_record_tokens_by_global_order(record_list):
    for i in range(len(record_list)):
        record_list[i] = sorted(record_list[i], key=lambda x: x[0])


def _split_record_token_and_index(record_list):
    record_token_list = []
    record_index_list = []
    for i in range(len(record_list)):
        token_list = []
        index_list = []
        for j in range(len(record_list[i])):
            token_list.append(record_list[i][j][0])
            index_list.append(record_list[i][j][1])
        record_token_list.append(array('I', token_list))
        record_index_list.append(array('I', index_list))

    return record_token_list, record_index_list


# Assemble the topk heap to a dataframe.
def _assemble_topk_table(rec_list, ltable, rtable, lkey, rkey, ret_key='_id',
                         l_output_prefix='ltable_', r_output_prefix='rtable_'):
    ret_data_col_name_list = ['_id']
    ltable_col_names = list(ltable.columns)
    rtable_col_names = list(rtable.columns)
    lkey_index = 0
    rkey_index = 0
    for i in range(len(ltable_col_names)):
        if ltable_col_names[i] == lkey:
            lkey_index = i

    for i in range(len(rtable_col_names)):
        if rtable_col_names[i] == rkey:
            rkey_index = i

    ret_data_col_name_list.append(l_output_prefix + lkey)
    ret_data_col_name_list.append(r_output_prefix + rkey)
    ltable_col_names.remove(lkey)
    rtable_col_names.remove(rkey)

    for i in range(len(ltable_col_names)):
        ret_data_col_name_list.append(l_output_prefix + ltable_col_names[i])
    for i in range(len(rtable_col_names)):
        ret_data_col_name_list.append(r_output_prefix + rtable_col_names[i])

    ret_tuple_list = []
    for i in range(len(rec_list)):
        tup = rec_list[i]
        lrecord = list(ltable.loc[tup[1]])
        rrecord = list(rtable.loc[tup[2]])
        ret_tuple = [i]
        ret_tuple.append(lrecord[lkey_index])
        ret_tuple.append(rrecord[rkey_index])
        for j in range(len(lrecord)):
            if j != lkey_index:
                ret_tuple.append(lrecord[j])
        for j in range(len(rrecord)):
            if j != rkey_index:
                ret_tuple.append(rrecord[j])
        ret_tuple_list.append(ret_tuple)

    data_frame = pd.DataFrame(ret_tuple_list)
    # When the ret data frame is empty, we cannot assign column names.
    if len(data_frame) == 0:
        return data_frame

    data_frame.columns = ret_data_col_name_list
    cm.set_candset_properties(data_frame, ret_key, l_output_prefix + lkey,
                              r_output_prefix + rkey, ltable, rtable)

    return data_frame


if __name__ == "__main__":
     output_path = '../results/'
     lkey = 'id'
     rkey = 'id'
     #ltable = mg.read_csv_metadata('../datasets/Walmart-Amazon/tableA.csv', key=lkey)
     #rtable = mg.read_csv_metadata('../datasets/Walmart-Amazon/tableB.csv', key=rkey)
     #cand_set = mg.read_csv_metadata('../datasets/Walmart-Amazon/title_overlap7.csv',
     #                               ltable=ltable, rtable=rtable, fk_ltable='ltable_' + lkey,
     #                                   fk_rtable='rtable_' + rkey, key='_id')
     ltable = mg.read_csv_metadata('../datasets/debugblocker/Amazon-Google/tableA.csv', key=lkey)
     rtable = mg.read_csv_metadata('../datasets/debugblocker/Amazon-Google/tableB.csv', key=rkey)
     cand_set = mg.read_csv_metadata('../datasets/debugblocker/candidate_sets/Amazon-Google/overlap/title_overlap3.csv',
                                    ltable=ltable, rtable=rtable, fk_ltable='ltable_' + lkey,
                                        fk_rtable='rtable_' + rkey, key='_id')
     output_size = 200
     debug_blocker(cand_set, ltable, rtable, output_size)

