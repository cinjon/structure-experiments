import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.autograd import Variable
import torch.optim as optim
from torch.utils.data import DataLoader

import numpy as np
import scipy.misc
import random
import math
import os
from PIL import Image
import progressbar
import logging
import shutil
import time

from util import *

from modules import *
from models import *
from env import *
from params import *
from generations import *
from atari_dataset import AtariData
from video_dataset import *
from covariance import construct_covariance

print("Tensor type: ", dtype)

# ------- load the model first, as this affects the paths used --------
if opt.load is not None:
    checkpoint = torch.load('results/' + opt.load + '/model.t7')
    model = checkpoint['model'].type(dtype)
    i = checkpoint['i']
    cp_opt = checkpoint['opt']

    # we're strictly trying to pick up where we left off
    # load everything just as it was (but rename)
    if opt.resume:
        setattrs(opt, cp_opt, exceptions=['load', 'resume', 'use_loaded_opt'])
        opt.name = cp_opt['name'] + '_'

    # if we want to use the options from the checkpoint, load them in
    # (skip the ones that don't make sense to load)
    if opt.use_loaded_opt:
        setattrs(opt, cp_opt, exceptions=[
            'name', 'load', 'sanity', 'resume', 'use_loaded_opt'
        ])
else:
    i = 0
    model = IndependentModel(opt.latents,
                             opt.latent_dim,
                             opt.image_width,
                             transition=Transition,
                            #  first_inference=TinyDCGANFirstInference,
                             first_inference=TinyDCGANFirstInference,
                            #  inference=TinyDCGANInference,
                             inference=TinyDCGANInference,
                            #  generator=TinyDCGANGenerator).type(dtype)
                             generator=TinyDCGANGenerator).type(dtype)

opt.save = 'results/' + opt.name

# -------- take care of logging, cleaning out the folder, etc -------
make_result_folder(opt.save)
write_options(opt.save)


# copy over the old results if we're resuming
if opt.resume:
    shutil.copyfile(cp_opt['save'] + '/results.csv',
                    opt.save + '/results.csv')

logging.basicConfig(filename = opt.save + "/results.csv",
                    level = logging.DEBUG,
                    format = "%(message)s")
logging.debug(("step,loss,nll,divergence,prior divergence,"
               "trans divergence,grad norm,ms/seq,lr"))

# --------- load a dataset ---------
train_data = BounceData(5, opt.balls, opt.colors, opt.image_width)
test_data = BounceData(5, opt.balls, opt.colors, opt.image_width)
# train_data = HorizontalBounceData(5, opt.balls, opt.colors, opt.image_width)
# test_data = HorizontalBounceData(5, opt.balls, opt.colors, opt.image_width)
train_loader = DataLoader(train_data,
                          num_workers=0,
                          batch_size=opt.batch_size,
                          shuffle=True)
test_loader = DataLoader(test_data,
                         num_workers=0,
                         batch_size=opt.batch_size,
                         shuffle=True)
                        #   drop_last=True)
print("Number of training sequences: " + str(len(train_data)))
print("Number of testing sequences: " + str(len(test_data)))

# ------------------------------------


KL = GaussianKLD().type(dtype)
LL = GaussianLL().type(dtype)
mse = nn.MSELoss().type(dtype)
l1 = nn.L1Loss().type(dtype)

def make_real_seq(length):
    sequence = [torch.zeros(opt.batch_size, opt.image_width**2 * 3)
                for _ in range(length)]
    for batch in range(batch_size):
        sequence[0][batch] = gen.start().render().view(opt.image_width**2 * 3)
        for i in range(1, length):
            sequence[i][batch] = gen.step().render().view(opt.image_width**2 * 3)
    return [Variable(x.type(dtype)) for x in sequence]

def make_seq(length, dim):
    sequence = [torch.zeros(opt.batch_size, dim).normal_(0.5, 0.1)]
    for i in range(1, length):
        noise = torch.zeros(opt.batch_size, dim) # torch.normal([0.0, 0.0], [0.1, 0.1])
        sequence.append(sequence[i-1] + noise)
    sequence = [Variable(x.type(dtype)) for x in sequence]
    return sequence

def sequence_input(seq):
    return [Variable(x.type(dtype)) for x in seq]

optimizer = optim.Adam(
    model.parameters(),
    lr=opt.lr)

print(model)

mean_loss = 0
mean_divergence = 0
mean_nll = 0
mean_prior_div = 0
mean_trans_div = 0
mean_grad_norm = 0
z_var_means = []
z_var_min = 1e6
z_var_max = -1

sequence = None
if opt.sanity:
    n_steps = 100 * opt.batch_size
    k = 100 * opt.batch_size
else:
    n_steps = int(5e8)
    k = 100000

cov_start = time.time()
construct_covariance(opt.save, model, train_loader, 5000,
                     label="train_" + str(i))
construct_covariance(opt.save, model, test_loader, 5000,
                     label="test_" + str(i))
cov_end = time.time()
print("Covariance analysis done. Duration: {:.2f}".format(cov_end - cov_start))

# make k a multiple of batch_size
k = (k // opt.batch_size) * opt.batch_size
progress = progressbar.ProgressBar(max_value=k)
while i < n_steps:
    for sequence in train_loader:
        # deal with the last, missized batch until drop_last gets shipped
        if sequence.size(0) != opt.batch_size:
            continue
        i += opt.batch_size

        sequence.transpose_(0, 1)
        sequence = sequence_input(list(sequence))

        kl_scale = 1
        # if not opt.no_kl_annealing:
        #     kl_scale = max(0, min(i / opt.kl_anneal_end, 1))

        generations, nll, divergences = model(sequence, kl_scale=kl_scale)
        (seq_divergence, seq_prior_div, seq_trans_div) = divergences
        mean_divergence += seq_divergence.data[0]
        mean_prior_div += seq_prior_div.data[0]
        mean_trans_div += seq_trans_div.data[0]
        mean_nll += nll.data[0]

        # var_min, var_mean, var_max = batch_z_vars
        # z_var_means.append(var_mean)
        # z_var_min = min(var_min, z_var_min)
        # z_var_max = max(var_max, z_var_max)

        kl_penalty = seq_divergence * opt.kl_weight
        if not opt.no_kl_annealing:
            kl_weight = max(0, min(i / opt.kl_anneal_end, 1))
            kl_penalty = kl_weight * kl_penalty

        loss = nll + kl_penalty
        mean_loss += loss.data[0]

        model.zero_grad()
        loss.backward()

        mean_grad_norm += grad_norm(model)

        if not opt.no_sgld:
            for p in model.parameters():
                if p.grad is not None:
                    p.grad.data += p.grad.data.clone().normal_(0, opt.lr)
        optimizer.step()

        progress.update(i%k)
        is_update_time = (i >= n_steps or (i % k == 0 and i > 0))
        if is_update_time:
            progress.finish()
            clear_progressbar()

            elapsed_time = progress.end_time - progress.start_time
            elapsed_seconds = elapsed_time.total_seconds()

            batches = k / opt.batch_size
            log_values = (i,
                          mean_loss / batches,
                          mean_nll / batches,
                          mean_divergence / batches,
                          mean_prior_div / batches,
                          mean_trans_div / batches,
                          mean_grad_norm / batches,
                          elapsed_seconds / k * 1000,
                          opt.lr)
            print(("Step: {:8d}, Loss: {:10.3f}, NLL: {:10.3f}, "
                   "Divergence: {:10.3f}, "
                   "Prior divergence: {:10.3f}, "
                   "Trans divergence: {:10.3f}, "
                   "Grad norm: {:10.3f}, "
                   "ms/seq: {:6.2f}").format(*log_values[:-1]))

            # make list of n copies of format string, then format
            format_string = ",".join(["{:.8e}"]*len(log_values))
            logging.debug(format_string.format(*log_values))

            # save_dict = {
            #         'model': model,
            #         'opt': vars(opt),
            #         'i': i,
            #         # 'train_data': train_data,
            #         # 'test_data': test_data,
            #     }
            # torch.save(save_dict, opt.save + '/model.t7')
            mean_loss = 0
            mean_divergence = 0
            mean_prior_div = 0
            mean_trans_div = 0
            mean_grad_norm = 0
            mean_nll = 0
            save_all_generations(model, sequence, generations)

            # progress = progressbar.ProgressBar(max_value=k)

        save_every = int(1e6)
        if i >= n_steps or (i % save_every == 0 and i > 0):
            save_dict = {
                    'model': model,
                    'opt': vars(opt),
                    'i': i,
                    # 'train_data': train_data,
                    # 'test_data': test_data,
                }
            atomic_save(save_dict, opt.save + '/model.t7')

        # do this at the beginning, and periodically after
        if i == n_steps or (i % 1000000 == 0 and i > 0):
            construct_covariance(opt.save, model, train_loader, 5000,
                                 label="train_" + str(i))
            construct_covariance(opt.save, model, test_loader, 5000,
                                 label="test_" + str(i))

        # learning rate decay
        # 0.985 every 320K -> ~0.2 at 1,000,000 steps
        if not opt.no_lr_decay and i % 320000 == 0 and i > 0:
            opt.lr = opt.lr * 0.985
            print("Decaying learning rate to: ", opt.lr)
            set_lr(optimizer, opt.lr)

        if is_update_time:
            progress = progressbar.ProgressBar(max_value=k)


        if i >= n_steps:
            break
