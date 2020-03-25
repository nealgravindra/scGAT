'''
Load data pre-processed in scanpy workflow

----
ACM CHIL 2020 paper

Neal G. Ravindra, 200228
'''

import os,sys,pickle,time,random,glob
import numpy as np
import pandas as pd

from typing import List
import copy
import os.path as osp
import torch
import torch.utils.data
from torch_sparse import SparseTensor, cat
from torch_geometric.data import Data
import torch.nn.functional as F
from torch_geometric.nn import GCNConv, GATConv
from sklearn.model_selection import train_test_split


## utils
def scipysparse2torchsparse(x) :
    '''
    Input: scipy csr_matrix
    Returns: torch tensor in experimental sparse format

    REF: Code adatped from [PyTorch discussion forum](https://discuss.pytorch.org/t/better-way-to-forward-sparse-matrix/21915>)
    '''
    samples=x.shape[0]
    features=x.shape[1]
    values=x.data
    coo_data=x.tocoo()
    indices=torch.LongTensor([coo_data.row,coo_data.col]) # OR transpose list of index tuples
    t=torch.sparse.FloatTensor(indices,torch.from_numpy(values).float(),[samples,features])
    return indices,t

def accuracy(output, labels):
    preds = output.max(1)[1].type_as(labels)
    correct = preds.eq(labels).double()
    correct = correct.sum()
    return correct / len(labels)


def masked_nll_loss(logits, labels, mask):
    loss = F.nll_loss(logits, labels, reduction='none')
    mask = mask.type(torch.float)
    mask = mask / (torch.sum(mask) / mask.size()[0])
    loss *= mask
    return torch.sum(loss) / loss.size()[0]

def masked_accuracy(output, labels, mask):
    """Accuracy with masking."""
    preds = output.max(1)[1].type_as(labels)
    correct = preds.eq(labels).double()
    correct *= mask
    correct = correct.sum()
    return correct / mask.sum()


## load data
def get_data(pdfp = '/home/ngr4/project/scgraph/data/processed/',
             data_pkl = 'transduction_50pData.pkl',
             replicate=sys.argv[1], # for pkl file saved
             NumParts = 4000, # num sub-graphs
             Device = 'cuda', # if no gpu, `Device='cpu'`
             fastmode = True) : # if `fastmode=False`, report validation

    class ClusterData(torch.utils.data.Dataset):
        r"""Clusters/partitions a graph data object into multiple subgraphs, as
        motivated by the `"Cluster-GCN: An Efficient Algorithm for Training Deep
        and Large Graph Convolutional Networks"
        <https://arxiv.org/abs/1905.07953>`_ paper.

        Args:
            data (torch_geometric.data.Data): The graph data object.
            num_parts (int): The number of partitions.
            recursive (bool, optional): If set to :obj:`True`, will use multilevel
                recursive bisection instead of multilevel k-way partitioning.
                (default: :obj:`False`)
            save_dir (string, optional): If set, will save the partitioned data to
                the :obj:`save_dir` directory for faster re-use.
        """
        def __init__(self, data, num_parts, recursive=False, save_dir=None):
            assert (data.edge_index is not None)

            self.num_parts = num_parts
            self.recursive = recursive
            self.save_dir = save_dir

            self.process(data)

        def process(self, data):
            recursive = '_recursive' if self.recursive else ''
            filename = f'part_data_{self.num_parts}{recursive}.pt'

            path = osp.join(self.save_dir or '', filename)
            if self.save_dir is not None and osp.exists(path):
                data, partptr, perm = torch.load(path)
            else:
                data = copy.copy(data)
                num_nodes = data.num_nodes

                (row, col), edge_attr = data.edge_index, data.edge_attr
                adj = SparseTensor(row=row, col=col, value=edge_attr)
                adj, partptr, perm = adj.partition(self.num_parts, self.recursive)

                for key, item in data:
                    if item.size(0) == num_nodes:
                        data[key] = item[perm]

                data.edge_index = None
                data.edge_attr = None
                data.adj = adj

                if self.save_dir is not None:
                    torch.save((data, partptr, perm), path)

            self.data = data
            self.perm = perm
            self.partptr = partptr


        def __len__(self):
            return self.partptr.numel() - 1


        def __getitem__(self, idx):
            start = int(self.partptr[idx])
            length = int(self.partptr[idx + 1]) - start

            data = copy.copy(self.data)
            num_nodes = data.num_nodes

            for key, item in data:
                if item.size(0) == num_nodes:
                    data[key] = item.narrow(0, start, length)

            data.adj = data.adj.narrow(1, start, length)

            row, col, value = data.adj.coo()
            data.adj = None
            data.edge_index = torch.stack([row, col], dim=0)
            data.edge_attr = value

            return data


        def __repr__(self):
            return (f'{self.__class__.__name__}({self.data}, '
                    f'num_parts={self.num_parts})')



    class ClusterLoader(torch.utils.data.DataLoader):
        r"""The data loader scheme from the `"Cluster-GCN: An Efficient Algorithm
        for Training Deep and Large Graph Convolutional Networks"
        <https://arxiv.org/abs/1905.07953>`_ paper which merges partioned subgraphs
        and their between-cluster links from a large-scale graph data object to
        form a mini-batch.

        Args:
            cluster_data (torch_geometric.data.ClusterData): The already
                partioned data object.
            batch_size (int, optional): How many samples per batch to load.
                (default: :obj:`1`)
            shuffle (bool, optional): If set to :obj:`True`, the data will be
                reshuffled at every epoch. (default: :obj:`False`)
        """
        def __init__(self, cluster_data, batch_size=1, shuffle=False, **kwargs):

            class HelperDataset(torch.utils.data.Dataset):
                def __len__(self):
                    return len(cluster_data)

                def __getitem__(self, idx):
                    start = int(cluster_data.partptr[idx])
                    length = int(cluster_data.partptr[idx + 1]) - start

                    data = copy.copy(cluster_data.data)
                    num_nodes = data.num_nodes
                    for key, item in data:
                        if item.size(0) == num_nodes:
                            data[key] = item.narrow(0, start, length)

                    return data, idx

            def collate(batch):
                data_list = [data[0] for data in batch]
                parts: List[int] = [data[1] for data in batch]
                partptr = cluster_data.partptr

                adj = cat([data.adj for data in data_list], dim=0)

                adj = adj.t()
                adjs = []
                for part in parts:
                    start = partptr[part]
                    length = partptr[part + 1] - start
                    adjs.append(adj.narrow(0, start, length))
                adj = cat(adjs, dim=0).t()
                row, col, value = adj.coo()

                data = cluster_data.data.__class__()
                data.num_nodes = adj.size(0)
                data.edge_index = torch.stack([row, col], dim=0)
                data.edge_attr = value

                ref = data_list[0]
                keys = ref.keys
                keys.remove('adj')

                for key in keys:
                    if ref[key].size(0) != ref.adj.size(0):
                        data[key] = ref[key]
                    else:
                        data[key] = torch.cat([d[key] for d in data_list],
                                              dim=ref.__cat_dim__(key, ref[key]))

                return data

            super(ClusterLoader,
                  self).__init__(HelperDataset(), batch_size, shuffle,
                                 collate_fn=collate, **kwargs)



    ## data
    with open(os.path.join(pdfp,data_pkl),'rb') as f :
        datapkl = pickle.load(f)
        f.close()

    node_features = torch.from_numpy(datapkl['features']).float() # already dense, node_features = torch.from_numpy(datapkl['features'].todense()).float()
    # _,node_features = scipysparse2torchsparse(features)
    labels = torch.LongTensor(datapkl['labels'])
    edge_index,_ = scipysparse2torchsparse(datapkl['adj'])

    idx_train,idx_test = train_test_split(range(node_features.shape[0]), test_size=0.2, random_state=42, stratify=labels)
    idx_test,idx_val = train_test_split(idx_test, test_size=0.5, random_state=42, stratify=labels[idx_test])
    train_mask = [1 if i in idx_train else 0 for i in range(node_features.shape[0])]
    val_mask = [1 if i in idx_val else 0 for i in range(node_features.shape[0])]
    test_mask = [1 if i in idx_test else 0 for i in range(node_features.shape[0])]

    train_mask = torch.tensor(train_mask,dtype=torch.bool)
    val_mask = torch.tensor(val_mask,dtype=torch.bool)
    test_mask = torch.tensor(test_mask,dtype=torch.bool)

    del datapkl

    d = Data(x=node_features, edge_index=edge_index, y=labels,
             train_mask=train_mask, val_mask=val_mask, test_mask=test_mask)
    # del node_features,edge_index,labels
    # del train_mask,val_mask,test_mask

    cd = ClusterData(d,num_parts=NumParts)
    cl = ClusterLoader(cd,batch_size=BatchSize,shuffle=True)

    return cl


## model
class GAT(torch.nn.Module):
    def __init__(self):
        super(GAT, self).__init__()
        self.gat1 = GATConv(d.num_node_features, out_channels=nHiddenUnits,
                            heads=nHeads, concat=True, negative_slope=alpha,
                            dropout=dropout, bias=True)
        self.gat2 = GATConv(nHiddenUnits*nHeads, d.y.unique().size()[0],
                            heads=nHeads, concat=False, negative_slope=alpha,
                            dropout=dropout, bias=True)

    def forward(self, data):
        x, edge_index = data.x, data.edge_index
        x = self.gat1(x, edge_index)
        x = F.elu(x)
        x = self.gat2(x, edge_index)
        return F.log_softmax(x, dim=1)


## train
if False :
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu') # don't let user make decisions
else :
    device = torch.device(Device)

random.seed(rs)
np.random.seed(rs)
torch.manual_seed(rs)
if Device == 'cuda' :
    torch.cuda.manual_seed(rs)

model = GAT().to(device)
optimizer = torch.optim.Adagrad(model.parameters(),
                                lr=LR,
                                weight_decay=WeightDecay)

# features, adj, labels = Variable(features), Variable(adj), Variable(labels)

def train(epoch):
    t = time.time()
    epoch_loss = []
    epoch_acc = []
    epoch_acc_val = []
    epoch_loss_val = []

    model.train()
    for batch in cl :
        batch = batch.to(device)
        optimizer.zero_grad()
        output = model(batch)
        # y_true = batch.y.to(device)
        loss = masked_nll_loss(output, batch.y, batch.train_mask)
        loss.backward()
        if clip is not None :
            torch.nn.utils.clip_grad_norm_(model.parameters(),clip)
        optimizer.step()
        epoch_loss.append(loss.item())
        epoch_acc.append(masked_accuracy(output, batch.y, batch.train_mask).item())
    if not fastmode :
        d_val = Data(x=node_features, edge_index=edge_index, y=labels,
             train_mask=train_mask, val_mask=val_mask, test_mask=test_mask)
        d_val = d_val.to(device)
        model.eval()
        output = model(d_val)
        loss_val = masked_nll_loss(output, d_val.y, d_val.val_mask)
        acc_val = masked_accuracy(output,d_val.y, d_val.val_mask).item()
        print('Epoch {}\t<loss>={:.4f}\t<acc>={:.4f}\tloss_val={:.4f}\tacc_val={:.4f}\tin {:.2f}-s'.format(epoch,np.mean(epoch_loss),np.mean(epoch_acc),loss_val.item(),acc_val,time.time() - t))
        return loss_val.item()
    else :
        print('Epoch {}\t<loss>={:.4f}\t<acc>={:.4f}\tin {:.2f}-s'.format(epoch,np.mean(epoch_loss),np.mean(epoch_acc),time.time()-t))
        return np.mean(epoch_loss)


def compute_test():
    d_test = Data(x=node_features, edge_index=edge_index, y=labels,
             train_mask=train_mask, val_mask=val_mask, test_mask=test_mask)

    model.eval()
    d_test=d_test.to(device)
    output = model(d_test)
    loss_test = masked_nll_loss(output, d_test.y, d_test.test_mask)
#     loss_test = nn.BCEWithLogitsLoss(output[idx_test], labels[idx_test])
    acc_test = masked_accuracy(output, d_test.y, d_test.test_mask).item()
    print("Test set results:",
          "\n    loss={:.4f}".format(loss_test.item()),
          "\n    accuracy={:.4f}".format(acc_test))

## call trainer
t_total = time.time()
loss_values = []
bad_counter = 0
best = nEpochs + 1
best_epoch = 0
for epoch in range(nEpochs):
    loss_values.append(train(epoch))

    torch.save(model.state_dict(), '{}-'.format(epoch)+replicate+'.pkl')
    if loss_values[-1] < best:
        best = loss_values[-1]
        best_epoch = epoch
        bad_counter = 0
    else:
        bad_counter += 1

    if bad_counter == patience:
        break

    files = glob.glob('*-'+replicate+'.pkl')
    for file in files:
        epoch_nb = int(file.split('-'+replicate+'.pkl')[0])
        if epoch_nb < best_epoch:
            os.remove(file)

files = glob.glob('*-'+replicate+'.pkl')
for file in files:
    epoch_nb = int(file.split('-'+replicate+'.pkl')[0])
    if epoch_nb > best_epoch:
        os.remove(file)

print('Optimization Finished!')
print('Total time elapsed: {:.2f}-min'.format((time.time() - t_total)/60))

# Restore best model
print('Loading epoch #{}'.format(best_epoch))
model.load_state_dict(torch.load('{}-'.format(best_epoch)+replicate+'.pkl'))

# Testing
compute_test()
