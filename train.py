#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
    Training
"""
import argparse
import os
import sys

import numpy as np
import torch
import torch.optim as optim
import torch.nn as nn
from torchsummary import summary
from torch.optim.lr_scheduler import ReduceLROnPlateau

from models.model import ChenModel, LeeModel
from input.utils import split_data
from input.data_loader import get_loader


def parse_args():
    """ Parsing arguments """
    parser = argparse.ArgumentParser(
        description='Training options for hyperspectral data',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument('-src_path',
                        required=False, type=str,
                        default='./data/hyperspectral_src_l2norm2.pt',
                        help='Path to training input file')
    parser.add_argument('-tgt_path',
                        required=False, type=str,
                        default='./data/hyperspectral_tgt_sm.pt',
                        help='Path to training labels')
    parser.add_argument('-gpu',
                        type=int, default=-1,
                        help="Gpu id to be used, default is -1 which means cpu")
    # Training options
    train = parser.add_argument_group('Training')
    train.add_argument('-epoch', type=int,
                       default=10,
                       help="Number of training epochs, default is 10")
    train.add_argument('-patch_size', type=int,
                       default=27,
                       help="Size of the spatial neighbourhood, default is 11")
    train.add_argument('-patch_step', type=int,
                       default=1,
                       help="Number of pixels to skip for each image patch while sliding over the training image")
    train.add_argument('-lr', type=float,
                       default=1e-3,
                       help="Learning rate, default is 1e-3")
    train.add_argument('-batch_size', type=int,
                       default=64,
                       help="Batch size, default is 64")
    train.add_argument('-train_from', type=str,
                       default='',
                       help="Path to checkpoint to start training from.")
    train.add_argument('-model', type=str,
                       default='ChenModel', choices=['ChenModel', 'LeeModel'],
                       help="Name of deep learning model to train with, options are [ChenModel | LeeModel]")
    train.add_argument('-save_dir', type=str,
                       default='',
                       help="Directory to save model. If not specified, use name of the model")
    train.add_argument('-report_frequency', type=int,
                       default=20,
                       help="Report training result every 'report_frequency' steps")
    opt = parser.parse_args()

    return opt


def get_input_data(metadata_path):
    """
    Get info such as number of classes for categorical classes
    :return:
    """
    metadata = torch.load(metadata_path)

    return metadata


def get_device(id):
    device = torch.device('cpu')
    if id > -1 and torch.cuda.is_available():
        device = torch.device('cuda:{}'.format(id))
    print("Number of GPUs available %i" % torch.cuda.device_count())
    print("Training on device: %s" % device)
    return device


def save_checkpoint(model, model_name, epoch):
    """
    Saving model's state dict
    TODO: also save optimizer' state dict and model options and enable restoring model from last training step
    :param model: model to save
    :param model_name: model will be saved under this name
    :param epoch: the epoch when model is saved
    :return:
    """
    path = './checkpoint/{}'.format(model_name)
    if not os.path.exists(path):
        os.makedirs(path)

    torch.save(model.state_dict(), '{}/{}_{}.pt'.format(path, model_name, epoch))
    print('Saving model at epoch %d' % (epoch + 1))


def compute_accuracy(predict, tgt, metadata):
    """
    Return number of correct prediction of each tgt label
    :param predict:
    :param tgt:
    :param metadata:
    :return:
    """
    n_correct = 0  # vector or scalar?

    categorical = metadata['categorical']
    num_classes = 0
    for idx, values in categorical.items():
        count = len(values)
        pred_class = predict[:, num_classes:(num_classes + count)]
        tgt_class = tgt[:, num_classes:(num_classes + count)]
        pred_indices = pred_class.argmax(1)  # get indices of max values in each row
        tgt_indices = tgt_class.argmax(1)
        true_positive = torch.sum(pred_indices == tgt_indices).item()
        n_correct += true_positive
        num_classes += count

    # return n_correct divided by number of labels * batch_size
    return n_correct / (len(predict) * len(categorical.keys()))


def validate(net, loss_fn, val_loader, device, metadata):
    sum_loss = 0
    N_samples = 0
    n_correct = 0
    sum_accuracy = 0
    for idx, (src, tgt) in enumerate(val_loader):
        src = src.to(device, dtype=torch.float32)
        tgt = tgt.to(device, dtype=torch.float32)
        N_samples += len(src)

        with torch.no_grad():
            predict = net(src)
            loss = loss_fn(predict, tgt)
            sum_loss += loss.item()
            # n_correct += compute_accuracy(predict, tgt, metadata)
            sum_accuracy += compute_accuracy(predict, tgt, metadata)

    # return average validation loss
    average_loss = sum_loss / len(val_loader)
    # accuracy = n_correct * 100 / N_samples
    accuracy = sum_accuracy * 100 / len(val_loader)
    return average_loss, accuracy


def train(net, optimizer, loss_fn, train_loader, val_loader, device, metadata, options, scheduler=None):
    """
    Training

    TODO: checkpoint
    :param net:
    :param optimizer:
    :param loss_fn:
    :param train_loader:
    :param val_loader:
    :param device:
    :param metadata:
    :param options:
    :param scheduler:
    :return:
    """
    epoch = options.epoch
    save_every = 1  # specify number of epochs to save model
    train_step = 0
    sum_loss = 0.0
    val_losses = []
    val_accuracies = []

    net.to(device)

    losses = []

    for e in range(epoch + 1):
        net.train()  # TODO: check docs
        epoch_loss = 0.0

        for idx, (src, tgt) in enumerate(train_loader):
            src = src.to(device, dtype=torch.float32)
            tgt = tgt.to(device, dtype=torch.float32)
            # tgt = tgt.to(device, dtype=torch.int64)

            optimizer.zero_grad()
            predict = net(src)
            loss = loss_fn(predict, tgt)
            sum_loss += loss.item()
            epoch_loss += loss.item()
            losses.append(loss.item())

            loss.backward()
            optimizer.step()

            if train_step % options.report_frequency == 0:
                # TODO: with LeeModel, take average of the loss
                print('Training loss at step {}: {:.5f}, average loss: {:.5f}'
                      .format(train_step, loss.item(), np.mean(losses[-100:])))

            train_step += 1

        epoch_loss = epoch_loss / len(train_loader)
        print('Average epoch loss: {:.5f}'.format(epoch_loss))
        metric = epoch_loss
        if val_loader is not None:
            val_loss, val_accuracy = validate(net, loss_fn, val_loader, device, metadata)
            print('Validation loss: {:.5f}, validation accuracy: {:.2f}%'.format(val_loss, val_accuracy))
            val_losses.append(val_loss)
            val_accuracies.append(val_accuracy)
            # metric = val_loss
            metric = -val_accuracy

        if isinstance(scheduler, optim.lr_scheduler.ReduceLROnPlateau):
            scheduler.step(metric)
        elif scheduler is not None:
            # other scheduler types
            scheduler.step()

        # Get current learning rate. Is there any better way?
        lr = None
        for param_group in optimizer.param_groups:
            if param_group['lr'] is not None:
                lr = param_group['lr']
                break
        print('Current learning rate: {}'.format(lr))
        if e % save_every == 0:
            save_dir = options.save_dir or options.model
            save_checkpoint(net, save_dir, e + 1)


def main():
    print('Start training...')
    print('System info: ', sys.version)
    print('Numpy version: ', np.__version__)
    print('Torch version: ', torch.__version__)
    #######
    options = parse_args()
    device = get_device(options.gpu)
    # TODO: check for minimum patch_size
    print('Training options: {}'.format(options))

    metadata = get_input_data('./data/metadata.pt')
    output_classes = metadata['num_classes']
    assert output_classes > 0, 'Number of classes has to be > 0'

    hyper_image = torch.load(options.src_path).float()
    hyper_labels = torch.load(options.tgt_path)
    # TODO: only need labels for classification task for now
    hyper_labels_cls = hyper_labels[:, :, :output_classes]
    hyper_labels_reg = hyper_labels[:, :, (output_classes + 1):]

    # maybe only copy to gpu during computation?
    hyper_image.to(device)
    # hyper_labels.to(device)
    hyper_labels_cls.to(device, dtype=torch.float32)
    hyper_labels_reg.to(device, dtype=torch.float32)

    R, C, B = hyper_image.shape

    train_set, test_set, val_set = split_data(R, C, options.patch_size, options.patch_step)

    # Model construction
    W, H, num_bands = hyper_image.shape

    model_name = options.model

    if model_name == 'ChenModel':
        model = ChenModel(num_bands, output_classes, patch_size=options.patch_size, n_planes=32)
    elif model_name == 'LeeModel':
        model = LeeModel(num_bands, output_classes)

    train_loader = get_loader(hyper_image,
                              hyper_labels_cls,
                              train_set,
                              options.batch_size,
                              model_name=model_name,
                              is_3d_convolution=True,
                              patch_size=options.patch_size,
                              shuffle=True)
    val_loader = get_loader(hyper_image,
                            hyper_labels_cls,
                            val_set,
                            options.batch_size,
                            model_name=model_name,
                            is_3d_convolution=True,
                            patch_size=options.patch_size,
                            shuffle=True)

    optimizer = optim.Adam(model.parameters(), lr=options.lr)

    loss = nn.BCELoss()  # doesn't work for multi-target
    # loss = nn.BCEWithLogitsLoss()
    # loss = nn.CrossEntropyLoss()
    # loss = nn.MultiLabelSoftMarginLoss(size_average=False)
    # End model construction

    if options.train_from:
        print('Loading checkpoint from %s' % options.train_from)
        checkpoint = torch.load(options.train_from)
        model.load_state_dict(checkpoint)

    model = model.to(device)
    with torch.no_grad():
        print('Model summary: ')
        for input, _ in train_loader:
            break

        summary(model,
                input.shape[1:],
                batch_size=options.batch_size,
                device=device.type)

    scheduler = ReduceLROnPlateau(optimizer, mode='min', factor=0.5, patience=5)
    train(model, optimizer, loss, train_loader,
          val_loader, device, metadata, options, scheduler=scheduler)
    print('End training...')


if __name__ == "__main__":
    main()
