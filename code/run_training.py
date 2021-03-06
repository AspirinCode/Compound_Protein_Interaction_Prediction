from collections import defaultdict
import pickle
import sys
import timeit

import numpy as np

import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim

from sklearn.metrics import roc_auc_score, precision_score, recall_score


class CompoundProteinInteractionPrediction(nn.Module):
    def __init__(self):
        super(CompoundProteinInteractionPrediction, self).__init__()
        self.embed_fingerprint = nn.Embedding(n_fingerprint, dim)
        self.embed_word = nn.Embedding(n_word, dim)
        self.W_gnn = nn.Linear(dim, dim)
        self.W_cnn = nn.Conv2d(in_channels=1, out_channels=1,
                               kernel_size=2*window+1, stride=1,
                               padding=window)
        self.W_attention = nn.Linear(dim, dim)
        self.W_out = nn.Linear(2*dim, 2)

    def gnn(self, xs, adjacency, layer_gnn):
        for _ in range(layer_gnn):
            hs = F.relu(self.W_gnn(xs))
            xs = hs + torch.matmul(adjacency, hs)
        return torch.unsqueeze(torch.sum(xs, 0), 0)

    def cnn(self, xs):
        xs = torch.unsqueeze(torch.unsqueeze(xs, 0), 0)
        return F.relu(self.W_cnn(xs))

    def attention_cnn(self, x, xs, layer_cnn):
        for _ in range(layer_cnn):
            hs = self.cnn(xs)
            hs = torch.squeeze(torch.squeeze(hs, 0), 0)
            weights = torch.tanh(F.linear(x, hs))
            xs = torch.t(weights) * hs
        return torch.unsqueeze(torch.sum(xs, 0), 0)

    def forward(self, inputs):

        fingerprints, adjacency, words = inputs

        """Compound vector with GNN."""
        x_fingerprints = self.embed_fingerprint(fingerprints)
        x_compound = self.gnn(x_fingerprints, adjacency, layer_gnn)

        """Protein vector with attention-CNN."""
        x_words = self.embed_word(words)
        x_protein = self.attention_cnn(x_compound, x_words, layer_cnn)

        y = torch.cat((x_compound, x_protein), 1)
        z = self.W_out(y)

        return z

    def __call__(self, data, train=True):

        inputs, interaction = data[:-1], data[-1]
        z = self.forward(inputs)

        if train:
            loss = F.cross_entropy(z, interaction)
            return loss
        else:
            z = F.softmax(z, 1).to('cpu').data[0].numpy()
            t = interaction.to('cpu').data[0].numpy()
            return z, t


class Trainer(object):
    def __init__(self, model):
        self.model = model
        self.optimizer = optim.Adam(self.model.parameters(), lr=lr)

    def train(self, dataset_train):
        np.random.shuffle(dataset_train)
        loss_total = 0
        for data in dataset_train:
            loss = self.model(data)
            self.optimizer.zero_grad()
            loss.backward()
            self.optimizer.step()
            loss_total += loss.to('cpu').data.numpy()
        return loss_total


class Tester(object):
    def __init__(self, model):
        self.model = model

    def test(self, dataset_test):

        z_list, t_list = [], []
        for data in dataset_test:
            z, t = self.model(data, train=False)
            z_list.append(z)
            t_list.append(t)

        score_list, label_list = [], []
        for z in z_list:
            score_list.append(z[1])
            label_list.append(np.argmax(z))
        auc = roc_auc_score(t_list, score_list)
        precision = precision_score(t_list, label_list)
        recall = recall_score(t_list, label_list)

        return auc, precision, recall

    def result(self, epoch, time, loss_total, auc_dev,
               auc_test, precision, recall, file_result):
        with open(file_result, 'a') as f:
            result = map(str, [epoch, time, loss_total, auc_dev,
                               auc_test, precision, recall])
            f.write('\t'.join(result) + '\n')

    def save(self, model, file_name):
        torch.save(model.state_dict(), file_name)


def load_dataset(data, dtype):
    return [dtype(d).to(device) for d in np.load(dir_input + data + '.npy')]


def load_pickle(data):
    with open(dir_input + data, 'rb') as f:
        return pickle.load(f)


def shuffle_dataset(dataset, seed):
    np.random.seed(seed)
    np.random.shuffle(dataset)
    return dataset


def split_dataset(dataset, ratio):
    n = int(ratio * len(dataset))
    dataset_1, dataset_2 = dataset[:n], dataset[n:]
    return dataset_1, dataset_2


if __name__ == "__main__":

    (DATASET, radius, ngram, dim, layer_gnn, window, layer_cnn, lr, lr_decay,
     decay_interval, iteration, setting) = sys.argv[1:]
    (dim, layer_gnn, window, layer_cnn,
     decay_interval, iteration) = map(int, [dim, layer_gnn, window, layer_cnn,
                                            decay_interval, iteration])
    lr, lr_decay = map(float, [lr, lr_decay])

    if torch.cuda.is_available():
        device = torch.device('cuda')
        print('The code uses GPU...')
    else:
        device = torch.device('cpu')
        print('The code uses CPU!!!')

    dir_input = ('../dataset/' + DATASET + '/input/radius' +
                 radius + '_ngram' + ngram + '/')
    compounds = load_dataset('compounds', torch.LongTensor)
    adjacencies = load_dataset('adjacencies', torch.FloatTensor)
    proteins = load_dataset('proteins', torch.LongTensor)
    interactions = load_dataset('interactions', torch.LongTensor)
    fingerprint_dict = load_pickle('fingerprint_dict.pickle')
    word_dict = load_pickle('word_dict.pickle')

    dataset = list(zip(compounds, adjacencies, proteins, interactions))
    dataset = shuffle_dataset(dataset, 1234)
    dataset_train, dataset_ = split_dataset(dataset, 0.8)
    dataset_dev, dataset_test = split_dataset(dataset_, 0.5)

    unknown = 100
    n_fingerprint = len(fingerprint_dict) + unknown
    n_word = len(word_dict) + unknown
    torch.manual_seed(1234)
    model = CompoundProteinInteractionPrediction().to(device)
    trainer = Trainer(model)
    tester = Tester(model)

    file_result = '../output/result/' + setting + '.txt'
    with open(file_result, 'w') as f:
        f.write('Epoch\tTime(sec)\tLoss\tAUC_dev\t'
                'AUC_test\tPrecision\tRecall\n')

    file_model = '../output/model/' + setting

    print('Epoch Time(sec) Loss AUC_dev AUC_test Precision Recall')

    start = timeit.default_timer()

    for epoch in range(iteration):

        if (epoch+1) % decay_interval == 0:
            trainer.optimizer.param_groups[0]['lr'] *= lr_decay

        loss_total = trainer.train(dataset_train)
        auc_dev = tester.test(dataset_dev)[0]
        auc_test, precision, recall = tester.test(dataset_test)

        end = timeit.default_timer()
        time = end - start

        tester.result(epoch, time, loss_total, auc_dev,
                      auc_test, precision, recall, file_result)
        tester.save(model, file_model)

        print(epoch, time, loss_total, auc_dev, auc_test, precision, recall)
