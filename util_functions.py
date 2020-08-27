from __future__ import print_function
import numpy as np
import random
from tqdm import tqdm
import os, sys, pdb, math, time
from copy import deepcopy
import multiprocessing as mp
import networkx as nx
import argparse
import scipy.io as sio
import scipy.sparse as ssp
import torch
from torch_geometric.data import Data, Dataset, InMemoryDataset
import warnings
warnings.simplefilter('ignore', ssp.SparseEfficiencyWarning)
cur_dir = os.path.dirname(os.path.realpath(__file__))
import torch.multiprocessing
torch.multiprocessing.set_sharing_strategy('file_system')


class MyDataset(InMemoryDataset):
    def __init__(self, data_list, root, transform=None, pre_transform=None):
        self.data_list = data_list
        super(MyDataset, self).__init__(root, transform, pre_transform)
        self.data, self.slices = torch.load(self.processed_paths[0])

    @property
    def raw_file_names(self):
        return []

    @property
    def processed_file_names(self):
        return ['data.pt']

    def download(self):
        # Download to `self.raw_dir`.
        pass

    def process(self):
        # Read data into huge `Data` list.
        data_list = self.data_list

        if self.pre_filter is not None:
            data_list = [data for data in data_list if self.pre_filter(data)]

        if self.pre_transform is not None:
            data_list = [self.pre_transform(data) for data in data_list]

        data, slices = self.collate(data_list)
        torch.save((data, slices), self.processed_paths[0])
        del self.data_list


class MyDynamicDataset(Dataset):
    def __init__(self, root, A, links, labels, h, sample_ratio, max_nodes_per_hop, 
                 u_features, v_features, class_values):
        super(MyDynamicDataset, self).__init__(root)
        self.A = A
        self.links = links
        self.labels = labels
        self.h = h
        self.sample_ratio = sample_ratio
        self.max_nodes_per_hop = max_nodes_per_hop
        self.u_features = u_features
        self.v_features = v_features
        self.class_values = class_values

    @property
    def raw_file_names(self):
        return []

    @property
    def processed_file_names(self):
        return []

    def _download(self):
        pass

    def _process(self):
        pass

    def __len__(self):
        return len(self.links[0])

    def get(self, idx):
        i, j = self.links[0][idx], self.links[1][idx]
        g_label = self.labels[idx]
        tmp = subgraph_extraction_labeling(
            (i, j), self.A, self.h, self.sample_ratio, self.max_nodes_per_hop, 
            self.u_features, self.v_features, self.class_values, g_label
        )
        return construct_pyg_graph(*tmp)


def links2subgraphs(A,
                    train_indices, 
                    val_indices, 
                    test_indices, 
                    train_labels, 
                    val_labels, 
                    test_labels, 
                    h=1, 
                    sample_ratio=1.0, 
                    max_nodes_per_hop=None, 
                    u_features=None, 
                    v_features=None, 
                    class_values=None, 
                    testing=False, 
                    parallel=True):
    # extract enclosing subgraphs
    def helper(A, links, g_labels):
        g_list = []
        if not parallel:
            with tqdm(total=len(links[0])) as pbar:
                for i, j, g_label in zip(links[0], links[1], g_labels):
                    tmp = subgraph_extraction_labeling(
                        (i, j), A, h, sample_ratio, max_nodes_per_hop, u_features, 
                        v_features, class_values, g_label
                    )
                    data = construct_pyg_graph(*tmp)
                    g_list.append(data)
                    pbar.update(1)
        else:
            start = time.time()
            pool = mp.Pool(mp.cpu_count())
            results = pool.starmap_async(
                subgraph_extraction_labeling, 
                [
                    ((i, j), A, h, sample_ratio, max_nodes_per_hop, u_features, 
                    v_features, class_values, g_label) 
                    for i, j, g_label in zip(links[0], links[1], g_labels)
                ]
            )
            remaining = results._number_left
            pbar = tqdm(total=remaining)
            while True:
                pbar.update(remaining - results._number_left)
                if results.ready(): break
                remaining = results._number_left
                time.sleep(1)
            results = results.get()
            pool.close()
            pbar.close()
            end = time.time()
            print("Time eplased for subgraph extraction: {}s".format(end-start))
            print("Transforming to pytorch_geometric graphs...")
            g_list = []
            pbar = tqdm(total=len(results))
            while results:
                tmp = results.pop()
                g_list.append(construct_pyg_graph(*tmp))
                pbar.update(1)
            pbar.close()
            end2 = time.time()
            print("Time eplased for transforming to pytorch_geometric graphs: {}s".format(end2-end))
        return g_list

    print('Enclosing subgraph extraction begins...')
    train_graphs = helper(A, train_indices, train_labels)
    if not testing:
        val_graphs = helper(A, val_indices, val_labels)
    else:
        val_graphs = []
    test_graphs = helper(A, test_indices, test_labels)

    return train_graphs, val_graphs, test_graphs


def subgraph_extraction_labeling(ind, A, h=1, sample_ratio=1.0, max_nodes_per_hop=None, 
                                 u_features=None, v_features=None, class_values=None, 
                                 y=1):
    # extract the h-hop enclosing subgraph around link 'ind'
    dist = 0
    u_nodes, v_nodes = [ind[0]], [ind[1]]
    u_dist, v_dist = [0], [0]
    u_visited, v_visited = set([ind[0]]), set([ind[1]])
    u_fringe, v_fringe = set([ind[0]]), set([ind[1]])
    for dist in range(1, h+1):
        v_fringe, u_fringe = neighbors(u_fringe, A, True), neighbors(v_fringe, A, False)
        u_fringe = u_fringe - u_visited
        v_fringe = v_fringe - v_visited
        u_visited = u_visited.union(u_fringe)
        v_visited = v_visited.union(v_fringe)
        if sample_ratio < 1.0:
            u_fringe = random.sample(u_fringe, int(sample_ratio*len(u_fringe)))
            v_fringe = random.sample(v_fringe, int(sample_ratio*len(v_fringe)))
        if max_nodes_per_hop is not None:
            if max_nodes_per_hop < len(u_fringe):
                u_fringe = random.sample(u_fringe, max_nodes_per_hop)
            if max_nodes_per_hop < len(v_fringe):
                v_fringe = random.sample(v_fringe, max_nodes_per_hop)
        if len(u_fringe) == 0 and len(v_fringe) == 0:
            break
        u_nodes = u_nodes + list(u_fringe)
        v_nodes = v_nodes + list(v_fringe)
        u_dist = u_dist + [dist] * len(u_fringe)
        v_dist = v_dist + [dist] * len(v_fringe)
    subgraph = A[u_nodes, :][:, v_nodes]
    # remove link between target nodes
    subgraph[0, 0] = 0

    # prepare pyg graph constructor input
    u, v, r = ssp.find(subgraph)  # r is 1, 2... (rating labels + 1)
    v += len(u_nodes)
    r = r - 1  # transform r back to rating label
    num_nodes = len(u_nodes) + len(v_nodes)
    node_labels = [x*2 for x in u_dist] + [x*2+1 for x in v_dist]
    max_node_label = 2*h + 1
    y = class_values[y]

    # get node features
    if u_features is not None:
        u_features = u_features[u_nodes]
    if v_features is not None:
        v_features = v_features[v_nodes]
    node_features = None
    if False: 
        # directly use padded node features
        if u_features is not None and v_features is not None:
            u_extended = np.concatenate(
                [u_features, np.zeros([u_features.shape[0], v_features.shape[1]])], 1
            )
            v_extended = np.concatenate(
                [np.zeros([v_features.shape[0], u_features.shape[1]]), v_features], 1
            )
            node_features = np.concatenate([u_extended, v_extended], 0)
    if False:
        # use identity features (one-hot encodings of node idxes)
        u_ids = one_hot(u_nodes, A.shape[0] + A.shape[1])
        v_ids = one_hot([x+A.shape[0] for x in v_nodes], A.shape[0] + A.shape[1])
        node_ids = np.concatenate([u_ids, v_ids], 0)
        #node_features = np.concatenate([node_features, node_ids], 1)
        node_features = node_ids
    if True:
        # only output node features for the target user and item
        if u_features is not None and v_features is not None:
            node_features = [u_features[0], v_features[0]]
    
    return u, v, r, node_labels, max_node_label, y, node_features


def construct_pyg_graph(u, v, r, node_labels, max_node_label, y, node_features):
    u, v = torch.LongTensor(u), torch.LongTensor(v)
    r = torch.LongTensor(r)  
    edge_index = torch.stack([torch.cat([u, v]), torch.cat([v, u])], 0)
    edge_type = torch.cat([r, r])
    x = torch.FloatTensor(one_hot(node_labels, max_node_label+1))
    y = torch.FloatTensor([y])
    data = Data(x, edge_index, edge_type=edge_type, y=y)

    if node_features is not None:
        if type(node_features) == list:  # a list of u_feature and v_feature
            u_feature, v_feature = node_features
            data.u_feature = torch.FloatTensor(u_feature).unsqueeze(0)
            data.v_feature = torch.FloatTensor(v_feature).unsqueeze(0)
        else:
            x2 = torch.FloatTensor(node_features)
            data.x = torch.cat([data.x, x2], 1)
    return data

   
def neighbors(fringe, A, row=True):
    # find all 1-hop neighbors of nodes in fringe from A
    res = set()
    for node in fringe:
        if row:
            _, nei, _ = ssp.find(A[node, :])
        else:
            nei, _, _ = ssp.find(A[:, node])
        nei = set(nei)
        res = res.union(nei)
    return res


def one_hot(idx, length):
    idx = np.array(idx)
    x = np.zeros([len(idx), length])
    x[np.arange(len(idx)), idx] = 1.0
    return x


def PyGGraph_to_nx(data):
    edges = list(zip(data.edge_index[0, :].tolist(), data.edge_index[1, :].tolist()))
    g = nx.from_edgelist(edges)
    g.add_nodes_from(range(len(data.x)))  # in case some nodes are isolated
    # transform r back to rating label
    edge_types = {(u, v): data.edge_type[i].item() for i, (u, v) in enumerate(edges)}
    nx.set_edge_attributes(g, name='type', values=edge_types)
    node_types = dict(zip(range(data.num_nodes), torch.argmax(data.x, 1).tolist()))
    nx.set_node_attributes(g, name='type', values=node_types)
    g.graph['rating'] = data.y.item()
    return g

