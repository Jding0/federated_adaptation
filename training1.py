import argparse
import json
from datetime import datetime
import os
import logging
import torch
print(torch.__version__)

import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
from torch.autograd import Variable
import math
from tqdm import tqdm
from torchvision import transforms
from torch.utils.tensorboard import SummaryWriter

from utils.image_helper import ImageHelper
from utils.text_helper import TextHelper

from utils.utils import dict_html

logger = logging.getLogger("logger")
# logger.setLevel("ERROR")
import yaml
import time
import numpy as np

import random
from utils.utils import *
from utils.text_load import *
from copy import deepcopy


criterion = torch.nn.CrossEntropyLoss()


########################################################################################################################
def fisher_matrix_diag(train_data_sets, target_model, criterion):
    model = target_model
    # Init
    fisher={}
    for n,p in model.named_parameters():
        fisher[n]=0*p.data
    # Compute
    model.train()
    start_time = time.time()
    for model_id in range(len(train_data_sets)):
        current_data_model, train_data_all = train_data_sets[model_id]
        ntokens = len(helper.corpus.dictionary)
        hidden = model.init_hidden(helper.batch_size)
        trunk = len(train_data_all)//100*(100-helper.local_test_perc)
        train_data = train_data_all[:trunk]
        data_iterator = range(0, train_data.size(0) - 1, helper.bptt)
        for batch_id, batch in enumerate(data_iterator):
            data, targets = helper.get_batch(train_data, batch,
                                              evaluation=False)
            # Forward and backward
            model.zero_grad()
            hidden = tuple([each.data for each in hidden])
            output, hidden = model(data, hidden)
            loss = criterion(output.view(-1, ntokens), targets)
            loss.backward()
            # Get gradients
            for n,p in model.named_parameters():
                if p.grad is not None:
                    fisher[n]+=len(data)*p.grad.data.pow(2)
    # Mean
    for n,_ in model.named_parameters():
        fisher[n]=fisher[n]/train_data.size(0)
        fisher[n]=torch.autograd.Variable(fisher[n],requires_grad=False)
    print('time spent on fisher:',time.time()-start_time)
    return fisher

def criterion1(global_model, model, fisher, output, targets, criterion, lamb=5000):
    model_old = deepcopy(global_model)
    model_old.eval()
    for param in model_old.parameters():# Freeze the weights
        param.requires_grad = False
    # Regularization for all previous tasks
    loss_reg=0
    for (name,param),(_,param_old) in zip(model.named_parameters(),model_old.named_parameters()):
        loss_reg+=torch.sum(fisher[name]*(param_old-param).pow(2))/2
    return criterion(output, targets)+lamb*loss_reg

    
########################################################################################################################


def train(fisher, helper, epoch, train_data_sets, local_model, target_model, last_weight_accumulator):    
    Test_Acc_Local = list()
    Test_Acc_Global = list()
    for model_id in tqdm(range(helper.no_models)):
        model = local_model
        ## Synchronize LR and models
        model.copy_params(target_model.state_dict())
        if helper.multi_gpu:
            model = torch.nn.DataParallel(model, dim=1).cuda()
        
        if helper.freeze_base:
            num = 0
            for parame in model.parameters():
                if num <=3:
                    parame.requires_grad = False
                num += 1
            optimizer = torch.optim.SGD(filter(lambda p: p.requires_grad, model.parameters()), lr=helper.lr,
                                        momentum=helper.momentum,
                                        weight_decay=helper.decay)
            
        optimizer = torch.optim.SGD(model.parameters(), lr=helper.lr,
                                    momentum=helper.momentum,
                                    weight_decay=helper.decay)
        model.train()
        start_time = time.time()
        if helper.data_type == 'text':
            current_data_model, train_data_all = train_data_sets[model_id]
            ntokens = len(helper.corpus.dictionary)
            if helper.multi_gpu:
                hidden = model.module.init_hidden(helper.batch_size)
            else:
                hidden = model.init_hidden(helper.batch_size)
            trunk = len(train_data_all)//100*(100-helper.local_test_perc)
            train_data = train_data_all[:trunk]
            test_data = train_data_all[trunk:]
        else:
            _, (current_data_model, train_data) = train_data_sets[model_id]
        
        for internal_epoch in range(1, helper.retrain_no_times + 1):
            model.train()
            start_time = time.time()
            total_loss = 0.
            
            if helper.data_type == 'text':
                data_iterator = range(0, train_data.size(0) - 1, helper.bptt)
            else:
                data_iterator = train_data
            batch_num = 0
            for batch_id, batch in enumerate(data_iterator):
                batch_num += 1
                optimizer.zero_grad()
                data, targets = helper.get_batch(train_data, batch,
                                                  evaluation=False)
                if helper.data_type == 'text':
#                     hidden = helper.repackage_hidden(hidden)
                    # Creating new variables for the hidden state, otherwise we'd backprop
                    # through the entire training history.
                    hidden = tuple([each.data for each in hidden])
                    output, hidden = model(data, hidden)
                    if helper.ewc:
                        loss = criterion1(target_model, model, fisher, output.view(-1, ntokens), targets, criterion, lamb=helper.lamb)
                    else:
                        loss = criterion(output.view(-1, ntokens), targets)
                else:
                    output = model(data)
                    loss = nn.functional.cross_entropy(output, targets)
                loss.backward()

                if helper.diff_privacy:
                    optimizer.step()
                    model_norm = helper.model_dist_norm(model, target_params_variables)
                    if model_norm > helper.params['s_norm']:
                        norm_scale = helper.params['s_norm'] / (model_norm)
                        for name, layer in model.named_parameters():
                            #### don't scale tied weights:
                            if helper.params.get('tied', False) and name == 'decoder.weight' or '__'in name:
                                continue
                            clipped_difference = norm_scale * (
                            layer.data - target_model.state_dict()[name])
                            layer.data.copy_(
                                target_model.state_dict()[name] + clipped_difference)
                elif helper.data_type == 'text':
                    # `clip_grad_norm` helps prevent the exploding gradient
                    # problem in RNNs / LSTMs.
                    torch.nn.utils.clip_grad_norm_(model.parameters(), helper.params['clip'])
                    optimizer.step()
                else:
                    optimizer.step()

                total_loss += loss.item()
        
        t = time.time()
        logger.info(f'testing model on local testset at model_id: {model_id}')
        local_loss, local_correct, local_total_test_wors, local_acc = eval_(helper, test_data, model)
        Test_Acc_Local.append(local_acc)
        logger.info(f'testing model on global testset at model_id: {model_id}')
        epoch_loss, epoch_acc = test(helper=helper, data_source=helper.test_data,
                                     model=model, is_poison=False, visualize=True)
        Test_Acc_Global.append(epoch_acc)
        logger.info(f'time spent on testing: {time.time() - t}')
    logger.info(f'Test_Acc_Local: {Test_Acc_Local}')
    logger.info(f'Test_Acc_Global: {Test_Acc_Global}')
    savedir1 = '/home/ty367/federated/data/'
    savedir2 = str(helper.lr)+'_freeze_base_'+str(helper.freeze_base)+'_ewc_'+str(helper.ewc)+str(helper.params['current_time'])
    logger.info(f'stats: {savedir2}')
    np.save(savedir1+'Test_Acc_Local_'+savedir2+'.npy',Test_Acc_Local)
    np.save(savedir1+'Test_Acc_Global_'+savedir2+'.npy',Test_Acc_Global)
        

def eval_(helper, data_source, model, is_poison=False):
    model.eval()
    total_loss = 0.0
    correct = 0.0
    total_test_words = 0.0
    data_iterator = range(0, data_source.size(0)-1, helper.params['bptt'])
    dataset_size = len(data_source)
    
    with torch.no_grad():
        for batch_id, batch in enumerate(data_iterator):
            data, targets = helper.get_batch(data_source, batch, evaluation=True)
            if helper.multi_gpu:
                hidden = model.module.init_hidden(data.size(-1))
            else:
                hidden = model.init_hidden(data.size(-1))
            output, hidden = model(data, hidden)
            output_flat = output.view(-1, helper.n_tokens)
            total_loss += len(data) * criterion(output_flat, targets).data
            hidden = helper.repackage_hidden(hidden)
            pred = output_flat.data.max(1)[1]
            correct += pred.eq(targets.data).sum().to(dtype=torch.float)
            total_test_words += targets.data.shape[0]
        acc = 100.0 * (correct / total_test_words)
        total_l = total_loss.item() / (dataset_size-1)
    return total_l, correct.item(), total_test_words, acc.item()

def test_local(helper, train_data_sets, target_model):
    Test_local_Loss = list()
    Test_local_Correct = list()
    Test_local_Total_test_words = list()
    Test_local_Acc = list()
    for model_id in range(len(train_data_sets)):
        model = target_model
        model.eval()
        if helper.data_type == 'text':
            current_data_model, train_data_all = train_data_sets[model_id]
            ntokens = len(helper.corpus.dictionary)
            if helper.multi_gpu:
                hidden = model.module.init_hidden(helper.batch_size)
            else:
                hidden = model.init_hidden(helper.batch_size)
            
            trunk = len(train_data_all)//100*(100-helper.local_test_perc)
            train_data = train_data_all[:trunk]
            test_data = train_data_all[trunk:]
        else:
            _, (current_data_model, train_data) = train_data_sets[model_id]
        
        local_loss, local_correct, local_total_test_wors, local_acc = eval_(helper, test_data, model)
        Test_local_Loss.append(local_loss)
        Test_local_Correct.append(local_correct)
        Test_local_Total_test_words.append(local_total_test_wors)
        Test_local_Acc.append(local_acc)
    np.save('/home/ty367/federated/data/Test_local_Loss.npy',Test_local_Loss) 
    np.save('/home/ty367/federated/data/Test_local_Correct.npy',Test_local_Correct) 
    np.save('/home/ty367/federated/data/Test_local_Total_test_words.npy',Test_local_Total_test_words) 
    np.save('/home/ty367/federated/data/Test_local_Acc.npy',Test_local_Acc) 
        
def test(helper, data_source,
         model, is_poison=False, visualize=True):
    model.eval()
    total_loss = 0.0
    correct = 0.0
    total_test_words = 0.0
    if helper.data_type == 'text':
        if helper.multi_gpu:
            hidden = model.module.init_hidden(helper.params['test_batch_size'])
        else:
            hidden = model.init_hidden(helper.params['test_batch_size'])
        random_print_output_batch = \
        random.sample(range(0, (data_source.size(0) // helper.params['bptt']) - 1), 1)[0]
        data_iterator = range(0, data_source.size(0)-1, helper.params['bptt'])
        if helper.partial_test:
            data_iterator = random.sample(data_iterator, len(data_iterator)//helper.partial_test)
        dataset_size = len(data_source)
    else:
        dataset_size = len(data_source.dataset)
        data_iterator = data_source

    with torch.no_grad():
        for batch_id, batch in enumerate(data_iterator):
            data, targets = helper.get_batch(data_source, batch, evaluation=True)
            if helper.data_type == 'text':
                output, hidden = model(data, hidden)
                output_flat = output.view(-1, helper.n_tokens)
                total_loss += len(data) * criterion(output_flat, targets).data
                hidden = tuple([each.data for each in hidden])
#                 hidden = helper.repackage_hidden(hidden)
                pred = output_flat.data.max(1)[1]
                correct += pred.eq(targets.data).sum().to(dtype=torch.float)
                total_test_words += targets.data.shape[0]
            else:
                output = model(data)
                total_loss += nn.functional.cross_entropy(output, targets,
                                                  reduction='sum').item() # sum up batch loss
                pred = output.data.max(1)[1]  # get the index of the max log-probability
                correct += pred.eq(targets.data.view_as(pred)).cpu().sum().item()

        if helper.data_type == 'text':
            acc = 100.0 * (correct / total_test_words)
            total_l = total_loss.item() / (dataset_size-1)
            if helper.multi_gpu:
                modelname = model.module.name
            else:
                modelname = model.name
            logger.info('___Global_Test {} poisoned: {}, Average loss: {:.4f}, '
                        'Accuracy: {}/{} ({:.4f}%) | per_perplexity {:8.2f}'
                        .format(modelname, is_poison, total_l, correct, total_test_words, acc, math.exp(total_l) if total_l < 30 else -1.))
            acc = acc.item()
#             total_l = total_l.item()
        else:
            acc = 100.0 * (float(correct) / float(dataset_size))
            total_l = total_loss / dataset_size

            logger.info(f'___Test {model.name} , Average loss: {total_l},  '
                        f'Accuracy: {correct}/{dataset_size} ({acc}%)')

    return (total_l, acc)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='PPDL')
    parser.add_argument('--params', dest='params', default='utils/params.yaml')
    parser.add_argument('--name', dest='name', required=True)

    args = parser.parse_args()
    d = datetime.now().strftime('%b.%d_%H.%M.%S')

    with open(args.params) as f:
        params_loaded = yaml.load(f)

    current_time = datetime.now().strftime('%b.%d_%H.%M.%S')
    if params_loaded['data_type'] == "image":
        helper = ImageHelper(current_time=current_time, params=params_loaded,
                             name=params_loaded.get('name', 'image'))
    else:
        helper = TextHelper(current_time=current_time, params=params_loaded,
                            name=params_loaded.get('name', 'text'))

    helper.load_data()
    helper.create_model()

    best_loss = float('inf')

    # configure logging
    if helper.log:
        logger = create_logger()
        fh = logging.FileHandler(filename=f'{helper.folder_path}/log.txt')
        formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        fh.setFormatter(formatter)
        logger.addHandler(fh)

        logger.warning(f'Logging things. current path: {helper.folder_path}')

        helper.params['tb_name'] = args.name
        with open(f'{helper.folder_path}/params.yaml.txt', 'w') as f:
            yaml.dump(helper.params, f)
    else:
        logger = create_logger()

    if helper.tb:
        wr = SummaryWriter(log_dir=f'/home/ty367/federated/runs/{args.name}')
        helper.writer = wr
        table = create_table(helper.params)        
        helper.writer.add_text('Model Params', table)
        print(table)

    if not helper.random:
        helper.fix_random()


    participant_ids = range(len(helper.train_data))
    mean_acc = list()

    weight_accumulator = None
    # save parameters:
    with open(f'{helper.folder_path}/params.yaml', 'w') as f:
        yaml.dump(helper.params, f)
    if not helper.only_eval:
        dist_list = list()
        Test_Loss = list()
        Test_Acc = list()
        if helper.ewc:
            if not os.path.exists(helper.resumed_fisher):
                fisher = fisher_matrix_diag([(pos, helper.train_data[pos]) for pos in participant_ids[1:]], helper.target_model, criterion)
                torch.save(fisher, helper.resumed_fisher)
            else:
                fisher = torch.load(helper.resumed_fisher)
        else:
            fisher = None
        for epoch in range(helper.start_epoch, helper.params['epochs'] + 1):
            start_time = time.time()

            subset_data_chunks = random.sample(participant_ids[1:], helper.no_models)
            logger.info(f'Selected models: {subset_data_chunks}')
            t = time.time()
            
            train(fisher=fisher, helper=helper, epoch=epoch, train_data_sets=[(pos, helper.train_data[pos]) for pos in subset_data_chunks],
                                       local_model=helper.local_model, target_model=helper.target_model,
                                        last_weight_accumulator=weight_accumulator)
            logger.info(f'time spent on training: {time.time() - t}')
    logger.info(f"This run has a label: {helper.params['current_time']}. ")
#     logger.info(f"Visdom environment: {helper.params['environment_name']}")
