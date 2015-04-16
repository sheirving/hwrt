#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
Segmentation is the task of splitting a data sequence into chuncks which can
then be classified chunk by chunk.

In the case of handwritten formula recognition it might be ok to segment by
strokes. However, one should note that cursive handwriting might make it
necessary to split on the point level.

Segmenting in the order in which strokes were written is also problematic as
delayed strokes (e.g. for extending a fraction stroke which was too short)
might occur.

This module contains algorithms for segmentation.
"""

import logging
import json

import itertools
import numpy
import os

import pickle
import pkg_resources

import math
import scipy.sparse.csgraph

# hwrt modules
# from . import HandwrittenData
from hwrt import utils
from hwrt.HandwrittenData import HandwrittenData
from hwrt import features
from hwrt import geometry
from hwrt import partitions

stroke_segmented_classifier = None


def _get_symbol_index(stroke_id_needle, segmentation):
    """
    :returns: The symbol index in which stroke_id_needle occurs

    >>> _get_symbol_index(3, [[0, 1, 2], [3, 4, 5], [6, 7]])
    1
    >>> _get_symbol_index(6, [[0, 1, 2], [3, 4, 5], [6, 7]])
    2
    >>> _get_symbol_index(7, [[0, 1, 2], [3, 4, 5], [6, 7]])
    2
    """
    for symbol_index, symbol in enumerate(segmentation):
        if stroke_id_needle in symbol:
            return symbol_index
    return None


def get_segmented_raw_data():
    import pymysql.cursors
    cfg = utils.get_database_configuration()
    mysql = cfg['mysql_dev']
    connection = pymysql.connect(host=mysql['host'],
                                 user=mysql['user'],
                                 passwd=mysql['passwd'],
                                 db=mysql['db'],
                                 cursorclass=pymysql.cursors.DictCursor)
    cursor = connection.cursor()
    sql = ("SELECT `id`, `data`, `segmentation` "
           "FROM `wm_raw_draw_data` WHERE `segmentation` "
           "IS NOT NULL AND `wild_point_count` = 0 "
           "ORDER BY `id` LIMIT 0, 4000")
    cursor.execute(sql)
    datasets = cursor.fetchall()
    return datasets


def get_dataset():
    """Create a dataset for machine learning.

    :returns: (X, y) where X is a list of tuples. Each tuple is a feature. y
              is a list of labels
              (0 for 'not in one symbol' and 1 for 'in symbol')
    """
    seg_data = "segmentation-X.npy"
    seg_labels = "segmentation-y.npy"
    if os.path.isfile(seg_data) and os.path.isfile(seg_labels):
        X = numpy.load(seg_data)
        y = numpy.load(seg_labels)
        with open('datasets.pickle', 'rb') as f:
            datasets = pickle.load(f)
        return (X, y, datasets)
    datasets = get_segmented_raw_data()
    X, y = [], []
    for i, data in enumerate(datasets):
        if i % 10 == 0:
            logging.info("i=%i", i)
        # logging.info("Start looking at dataset %i", i)
        segmentation = json.loads(data['segmentation'])
        # logging.info(segmentation)
        recording = json.loads(data['data'])
        X_symbol = [get_median_stroke_distance(recording)]
        if len([p for s in recording for p in s if p['time'] is None]) > 0:
            continue
        for strokeid1, strokeid2 in itertools.combinations(list(range(len(recording))), 2):
            stroke1 = recording[strokeid1]
            stroke2 = recording[strokeid2]
            if len(stroke1) == 0 or len(stroke2) == 0:
                logging.debug("stroke len 0. Skip.")
                continue
            X.append(get_stroke_features(recording, strokeid1, strokeid2)+X_symbol)
            same_symbol = (_get_symbol_index(strokeid1, segmentation) ==
                           _get_symbol_index(strokeid2, segmentation))
            y.append(int(same_symbol))
    X = numpy.array(X, dtype=numpy.float32)
    y = numpy.array(y, dtype=numpy.int32)
    numpy.save(seg_data, X)
    numpy.save(seg_labels, y)
    with open('datasets.pickle', 'wb') as f:
        pickle.dump(datasets, f, protocol=pickle.HIGHEST_PROTOCOL)
    return (X, y, datasets)


def get_nn_classifier(X, y):
    import lasagne
    import theano
    import theano.tensor as T
    N_CLASSES = 2

    # First, construct an input layer.
    # The shape parameter defines the expected input shape, which is just the
    # shape of our data matrix X.
    l_in = lasagne.layers.InputLayer(shape=X.shape)
    # A dense layer implements a linear mix (xW + b) followed by a nonlinearity.
    hiddens = [64, 64, 64]  # sollte besser als 0.12 sein (mit [32])
    layers = [l_in]

    for n_units in hiddens:
        l_hidden_1 = lasagne.layers.DenseLayer(
            layers[-1],  # The first argument is the input to this layer
            num_units=n_units,  # This defines the layer's output dimensionality
            nonlinearity=lasagne.nonlinearities.tanh)  # Various nonlinearities are available such as relu
        layers.append(l_hidden_1)
    # For our output layer, we'll use a dense layer with a softmax nonlinearity.
    l_output = lasagne.layers.DenseLayer(layers[-1], num_units=N_CLASSES,
                                         nonlinearity=lasagne.nonlinearities.softmax)
    # Now, we can generate the symbolic expression of the network's output
    # given an input variable.
    net_input = T.matrix('net_input')
    net_output = l_output.get_output(net_input)
    # As a loss function, we'll use Theano's categorical_crossentropy function.
    # This allows for the network output to be class probabilities,
    # but the target output to be class labels.
    true_output = T.ivector('true_output')
    loss = T.mean(T.nnet.categorical_crossentropy(net_output, true_output))

    reg = lasagne.regularization.l2(l_output)
    loss = loss + 0.001*reg
    #NLL_LOSS = -T.sum(T.log(p_y_given_x)[T.arange(y.shape[0]), y]) Retrieving
    # all parameters of the network is done using get_all_params, which
    # recursively collects the parameters of all layers connected to the
    # provided layer.
    all_params = lasagne.layers.get_all_params(l_output)

    # Now, we'll generate updates using Lasagne's SGD function
    updates = lasagne.updates.momentum(loss, all_params, learning_rate=0.1)

    # Finally, we can compile Theano functions for training and computing the
    # output.
    train = theano.function([net_input, true_output], loss, updates=updates)
    get_output = theano.function([net_input], net_output)

    logging.debug("|X|=%i", len(X))
    logging.debug("|y|=%i", len(y))
    logging.debug("|X[0]|=%i", len(X[0]))

    # Train
    epochs = 20
    for n in range(epochs):
        train(X, y)
    return get_output


def get_stroke_features(recording, strokeid1, strokeid2):
    """Get the features used to decide if two strokes belong to the same symbol
    or not.

    * Distance of bounding boxes
    """
    stroke1 = recording[strokeid1]
    stroke2 = recording[strokeid2]
    assert isinstance(stroke1, list), "stroke1 is a %s" % type(stroke1)
    X_i = []
    for s in [stroke1, stroke2]:
        hw = HandwrittenData(json.dumps([s]))
        feat1 = features.ConstantPointCoordinates(strokes=1,
                                                  points_per_stroke=20,
                                                  fill_empty_with=0)
        feat2 = features.ReCurvature(strokes=1)
        feat3 = features.Ink()
        X_i += hw.feature_extraction([feat1, feat2, feat3])
    X_i += [get_strokes_distance(stroke1, stroke2)]  # Distance of strokes
    X_i += [get_time_distance(stroke1, stroke2)]  # Time in between
    X_i += [abs(strokeid2-strokeid1)]  # Strokes in between
    return X_i


def get_median_stroke_distance(recording):
    dists = []
    for s1_id in range(len(recording)-1):
        for s2_id in range(s1_id+1, len(recording)):
            dists.append(get_strokes_distance(recording[s1_id],
                                              recording[s2_id]))
    return numpy.median(dists)


def get_time_distance(s1, s2):
    min_dist = abs(s1[0]['time'] - s2[0]['time'])
    for p1, p2 in zip(s1, s2):
        dist = abs(p1['time'] - p2['time'])
        min_dist = min(min_dist, dist)
    return min_dist


def get_strokes_distance(s1, s2):
    if len(s1) == 1:
        s1 += s1
    if len(s2) == 1:
        s2 += s2
    stroke1 = geometry.PolygonalChain(s1)
    stroke2 = geometry.PolygonalChain(s2)

    min_dist = geometry.segments_distance(stroke1[0], stroke2[0])
    for seg1, seg2 in itertools.product(stroke1, stroke2):
        min_dist = min(min_dist, geometry.segments_distance(seg1, seg2))
    return min_dist


def merge_segmentations(segs1, segs2):
    """
    Parameters
    ----------
    segs1 : a list of tuples
        Each tuple is a segmentation with its score
    segs2 : a list of tuples
        Each tuple is a segmentation with its score

    Returns
    -------
    list of tuples :
        Segmentations with their score, combined from segs1 and segs2
    """
    topf = partitions.TopFinder(500)
    for s1, s2 in itertools.product(segs1, segs2):
        topf.push(s1[0]+s2[0], s1[1]*s2[1])
    return list(topf)


def update_segmentation_data(segmentation, add):
    return [[el + add for el in symbol] for symbol in segmentation]


def get_segmentation(recording, single_clf):
    """

    Parameters
    ----------
    recording : A list of lists
        Each sublist represents a stroke

    Returns
    -------
    list of tuples :
        Segmentations together with their probabilities. Each probability
        has to be positive and the sum may not be bigger than 1.0.

    Examples
    --------
    >>> stroke1 = [{'x': 0, 'y': 0, 'time': 0}, {'x': 12, 'y': 12, 'time': 1}]
    >>> stroke2 = [{'x': 0, 'y': 10, 'time': 2}, {'x': 12, 'y': 0, 'time': 3}]
    >>> stroke3 = [{'x': 14, 'y': 0, 'time': 5}, {'x': 14, 'y': 12, 'time': 6}]
    >>> #get_segmentation([stroke1, stroke2, stroke3], single_clf)
    [
      ([[0, 1], [2]], 0.8),
      ([[0], [1,2]], 0.1),
      ([[0,2], [1]], 0.05)
    ]
    """
    global stroke_segmented_classifier
    X_symbol = [get_median_stroke_distance(recording)]
    if stroke_segmented_classifier is None:
        logging.info("Start creation of training set")
        X, y, datasets = get_dataset()
        logging.info("Start training")
        nn = get_nn_classifier(X, y)
        stroke_segmented_classifier = lambda X: nn(X)[0][1]
        #import pprint
        #pp = pprint.PrettyPrinter(indent=4)
        y_predicted = numpy.argmax(nn(X), axis=1)
        classification = [yi == yip for yi, yip in zip(y, y_predicted)]
        err = float(sum([not i for i in classification]))/len(classification)
        logging.info("Error: %0.2f (for %i training examples)", err, len(y))

    # Pre-segment to 8 strokes
    # TODO: Take first 4 strokes and add strokes within their bounding box
    # TODO: What if that is more then 8 strokes?
    # -> Geometry
    #    Build tree structure. A stroke `c` is the child of another stroke `p`,
    #    if the bounding box of `c` is within the bounding box of `p`.
    #       Problem: B <-> 13
    top_segmentations_global = [([], 1.0)]
    for chunk_part in range(int(math.ceil(float(len(recording))/8))):
        chunk = recording[8*chunk_part:8*(chunk_part+1)]

        # Segment after pre-segmentation
        prob = [[1.0 for _ in chunk] for _ in chunk]
        for strokeid1 in range(len(chunk)):
            for strokeid2 in range(len(chunk)):
                if strokeid1 == strokeid2:
                    continue
                X = get_stroke_features(chunk, strokeid1, strokeid2)
                X += X_symbol
                X = numpy.array([X], dtype=numpy.float32)
                prob[strokeid1][strokeid2] = stroke_segmented_classifier(X)

        top_segmentations = list(partitions.get_top_segmentations(prob, 500))
        for i, segmentation in enumerate(top_segmentations):
            symbols = apply_segmentation(chunk, segmentation)
            min_top2 = partitions.TopFinder(1, find_min=True)
            for i, symbol in enumerate(symbols):
                predictions = single_clf.predict(symbol)
                min_top2.push("value-%i" % i,
                              predictions[0]['probability'] + predictions[1]['probability'])
            top_segmentations[i][1] *= list(min_top2)[0][1]
        for i, segmentation in enumerate(top_segmentations):
            top_segmentations[i][0] = update_segmentation_data(top_segmentations[i][0], 8*chunk_part)
        top_segmentations_global = merge_segmentations(top_segmentations_global, top_segmentations)
    return top_segmentations_global


def _is_out_of_order(segmentation):
    """
    Check if a given segmentation is out of order.

    Examples
    --------
    >>> _is_out_of_order([[0, 1, 2, 3]])
    False
    >>> _is_out_of_order([[0, 1], [2, 3]])
    False
    >>> _is_out_of_order([[0, 1, 3], [2]])
    True
    """
    last_stroke = -1
    for symbol in segmentation:
        for stroke in symbol:
            if last_stroke > stroke:
                return True
            last_stroke = stroke
    return False


def _less_than(l, n):
    return float(len([1 for el in l if el < n]))


class single_classificer(object):
    def __init__(self):
        logging.info("Start reading model...")
        model_path = pkg_resources.resource_filename('hwrt', 'misc/')
        model_file = os.path.join(model_path, "model.tar")
        logging.info("Model: %s", model_file)
        (preprocessing_queue, feature_list, model,
         output_semantics) = utils.load_model(model_file)
        self.preprocessing_queue = preprocessing_queue
        self.feature_list = feature_list
        self.model = model
        self.output_semantics = output_semantics

    def predict(self, parsed_json):
        evaluate = utils.evaluate_model_single_recording_preloaded
        results = evaluate(self.preprocessing_queue,
                           self.feature_list,
                           self.model,
                           self.output_semantics,
                           json.dumps(parsed_json['data']),
                           parsed_json['id'])
        return results


def apply_segmentation(recording, segmentation):
    symbols = []
    seg, prob = segmentation
    for symbol_indices in seg:
        symbol = []
        for index in symbol_indices:
            symbol.append(recording[index])
        symbols.append({'data': symbol, 'id': 'symbol-%i' % index})
    return symbols


class Graph(object):
    def __init__(self):
        self.nodes = []

    def add_node(self, payload):
        """
        Returns
        -------
        int
            Identifier for the inserted node.
        """
        self.nodes.append(Node(len(self.nodes), payload))
        return len(self.nodes) - 1

    def add_edge(self, node_i, node_j):
        self.nodes[node_i].neighbors.append(self.nodes[node_j])
        self.nodes[node_j].neighbors.append(self.nodes[node_i])

    def get_connected_nodes(self):
        remaining_graph_nodes = list(range(len(self.nodes)))
        segments = []
        while len(remaining_graph_nodes) > 0:
            node_nr = remaining_graph_nodes.pop()
            segment = []
            queued = [node_nr]
            while len(queued) > 0:
                current = queued.pop()
                segment.append(current)
                remaining_graph_nodes.remove(current)
                queued = [n.identifier for n in self.nodes[current].neighbors
                          if n.identifier in remaining_graph_nodes]
            segments.append(segment)
        return segments

    def generate_euclidean_edges(self):
        n = len(self.nodes)
        self.w = numpy.zeros(shape=(n, n))
        for i in range(n):
            for j in range(n):
                self.w[i][j] = self.nodes[i].get().dist_to(self.nodes[j].get())


class Node(object):
    def __init__(self, identifier, payload):
        self.neighbors = []
        self.payload = payload
        self.identifier = identifier

    def add_neighbor(self, neighbor_node):
        self.neighbors.append(neighbor_node)

    def get(self):
        return self.payload


def get_segmentation_from_mst(mst, number):
    """Get a segmentation from a MST

    If the MST has 5 strokes and a spanning tree like
    1-\
       3-4-5
    2-/
    the number 3 (0011) would mean that the 0th edge
    and the 1st edge get cut. Lets say that the edge 0 is next to node 1 and
    edge 1 is next to node 2. Then the resulting segmentation would be
    [[1], [2], [3, 4, 5]]

    Parameters
    ----------
    mst :
        Minimum spanning tree
    number : int (0..edges in MST)
        The number of the segmentation.
    """
    pass


def get_mst(points):
    """
    Parameters
    ----------
    points : list of points (geometry.Point)
        The first element of the list is the center of the bounding box of the
        first stroke, the second one belongs to the seconds stroke, ...

    Returns
    -------
    mst : square matrix
        0 nodes the edges are not connected, > 0 means they are connected
        Please note that the returned matrix is not symmetrical!
    """
    graph = Graph()
    for point in points:
        graph.add_node(point)
    graph.generate_euclidean_edges()
    print(graph.w)
    matrix = scipy.sparse.csgraph.minimum_spanning_tree(graph.w)
    return matrix.toarray().astype(int)


if __name__ == '__main__':
    import sys
    logging.basicConfig(format='%(asctime)s %(levelname)s %(message)s',
                        level=logging.DEBUG,
                        stream=sys.stdout)

    logging.info("Start doctest")
    import doctest
    doctest.testmod()

    logging.info("Get single classifier")
    single_clf = single_classificer()

    logging.info("Get segmented raw data")
    recordings = get_segmented_raw_data()
    logging.info("Start testing")
    score_place = []
    out_of_order_count = 0
    ## Filter recordings
    new_recordings = []
    for recording in recordings:
        recording['data'] = json.loads(recording['data'])
        recording['segmentation'] = json.loads(recording['segmentation'])
        had_none = False
        for stroke in recording['data']:
            for point in stroke:
                if point['time'] is None:
                    logging.debug("Had None-time: %i", recording['id'])
                    had_none = True
                    break
            if had_none:
                break
        if not had_none:
            new_recordings.append(recording)

    recordings = new_recordings
    logging.info("Done filtering")

    for nr, recording in enumerate(recordings):
        if nr % 100 == 0:
            print(("## %i " % nr) + "#"*80)
        seg_predict = get_segmentation(recording['data'], single_clf)
        real_seg = recording['segmentation']
        pred_str = ""
        for i, pred in enumerate(seg_predict):
            seg, score = pred
            if i == 0:
                pred_str = "  Predict segmentation:\t%s (%0.8f)" % (seg, score)
            #print("#{0:>3} {1:.8f}: {2}".format(i, score, seg))
            if seg == real_seg:
                score_place.append(i)
                break
        else:
            i = -1
        print("## %i" % recording['id'])
        print("  Real segmentation:\t%s (got at place %i)" % (real_seg, i))
        print(pred_str)
        out_of_order_count += _is_out_of_order(real_seg)
    print(score_place)
    logging.info("mean: %0.2f", numpy.mean(score_place))
    logging.info("median: %0.2f", numpy.median(score_place))
    logging.info("TOP-1: %0.2f", _less_than(score_place, 1)/len(recordings))
    logging.info("TOP-3: %0.2f", _less_than(score_place, 3)/len(recordings))
    logging.info("TOP-10: %0.2f", _less_than(score_place, 10)/len(recordings))
    logging.info("TOP-20: %0.2f", _less_than(score_place, 20)/len(recordings))
    logging.info("TOP-50: %0.2f", _less_than(score_place, 50)/len(recordings))
    logging.info("Out of order: %i", out_of_order_count)
    logging.info("Total: %i", len(recordings))