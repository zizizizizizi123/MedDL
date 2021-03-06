import os
import time
import shutil
import numpy as np
from tqdm import tqdm
import torch
import torch.nn.functional as F 
from torch.cuda.amp import GradScaler, autocast
from tensorboardX import SummaryWriter
import torch.nn.parallel
from utils.utils import distributed_all_gather
import torch.utils.data.distributed
from monai.data import decollate_batch
from utils.valid_utils import AverageMeter,to_one_hot_3d,to_one_hot_3d_target

'''func for training'''
def train_epoch(model,
                loader,
                acc_func,
                optimizer,
                scaler,
                epoch,
                loss_func,
                loss_con,
                args):
    # set in train mode
    model.train()
    start_time = time.time()
    # calculate losses
    run_loss = AverageMeter()
    # DS coeffis
    alpha = 0.4
    y_val=[]
    y_pred=[]
    if epoch % 30 == 0: alpha *= 0.8
    for idx, batch_data in enumerate(loader):
        # clean cuda cached useless grad graph
        torch.cuda.empty_cache()
        # try batch_data is a list to select ways of loading. for compatibility
        if isinstance(batch_data, list):
            data, target = batch_data
        else:
            data, target = batch_data['image'], batch_data['label']
        # set data downstream to cuda 
        images, target = data.cuda(), target.cuda()
        # set non grad for training
        for param in model.parameters(): param.grad = None
        # cuda opt
            # training and loss calculation
        logits = model(images)
        loss = loss_con(logits,F.one_hot(target,num_classes=7).float())#+loss_func(logits,target)
        logits=F.softmax(logits,dim=1)
        ctarget=target.cpu().tolist()
        for i in ctarget:
            y_val.append(i)
        clogits=logits.argmax(dim=1).cpu().tolist()
        for i in clogits:
            y_pred.append(i)
            # normal bp
        optimizer.zero_grad()
        loss.backward()
        optimizer.step()
        run_loss.update(loss.item(), n=args.batch_size)
        # print data
        
        print('Epoch {}/{} {}/{}'.format(epoch, args.max_epochs, idx, len(loader)),
                  'loss: {:.4f}'.format(run_loss.avg),
                  'time {:.2f}s'.format(time.time() - start_time))
        torch.cuda.empty_cache()
        start_time = time.time()
    for param in model.parameters() : param.grad = None
    acc = acc_func(y_val,y_pred)
    return run_loss.avg,acc

def val_epoch(model,
              loader,
              epoch,
              acc_func,
              args,
              loss_func,
              loss_con,
              model_inferer=None,
              post_label=None,
              post_pred=None):
    # evalution mode
    model.eval()
    start_time = time.time()
    run_loss = AverageMeter()
    # standard evaluation paradigm
    with torch.no_grad():
        y_val=[]
        y_pred=[]
        for idx, batch_data in enumerate(loader):
            torch.cuda.empty_cache()   
            if isinstance(batch_data, list):
                data, target = batch_data
            else:
                data, target = batch_data['image'], batch_data['label']
            data, target = data.cuda(), target.cuda()
            logits = model(data)
            if not logits.is_cuda:
                target = target.cpu()
            torch.cuda.empty_cache()
            
            loss = loss_func(logits,target)
            
            run_loss.update(loss.item(), n=args.batch_size)
            # calulate metric.
            logits=F.softmax(logits,dim=1)
            ctarget=target.cpu().tolist()
            for i in ctarget:
              y_val.append(i)
            clogits=logits.argmax(dim=1).cpu().tolist()
            for i in clogits:
              y_pred.append(i)
            torch.cuda.empty_cache()
            start_time = time.time()
        acc = acc_func(y_val,y_pred)
        print(acc)
    return acc,run_loss.avg

def save_checkpoint(model,
                    epoch,
                    args,
                    filename='model',
                    best_acc=0,
                    optimizer=None,
                    scheduler=None):
    # save state dict & best epoch & epoch & optimizer & scheduler for checkpoint reload
    state_dict = model.state_dict()
    save_dict = {
            'epoch': epoch,
            'best_acc': best_acc,
            'state_dict': state_dict
            }
    if optimizer is not None:
        save_dict['optimizer'] = optimizer.state_dict()
    if scheduler is not None:
        save_dict['scheduler'] = scheduler.state_dict()
    filename_state_dict = filename + "_state_dict.pth"
    filename_state_dict=os.path.join(args.logdir, filename_state_dict)
    torch.save(save_dict, filename_state_dict)
    
    # save a wrap for convenience
    filename_model = filename + "_model.pth"
    filename_model=os.path.join(args.logdir, filename_model)
    torch.save(model,filename_model)
    print('Saving checkpoint', filename)


'''core training'''
def run_training(model,
                 train_loader,
                 val_loader,
                 optimizer,
                 loss_func,
                 loss_con,
                 acc_func,
                 args,
                 model_inferer=None,
                 scheduler=None,
                 start_epoch=0,
                 post_label=None,
                 post_pred=None
                 ):
    writer = None
    # tensorboard logging
    if args.logdir is not None:
        writer = SummaryWriter(log_dir=args.logdir)
        print('Writing Tensorboard logs to ', args.logdir)
    val_acc_max = 0.
    
    # epoch iteration
    for epoch in range(start_epoch, args.max_epochs):
        torch.cuda.empty_cache()
        # distrib. ln. setting
        print(time.ctime(), 'Epoch:', epoch)
        epoch_time = time.time()
        # training
        train_loss,train_acc = train_epoch(model,
                                 train_loader,
                                 acc_func,
                                 optimizer,
                                 scaler=None,
                                 epoch=epoch,
                                 loss_func=loss_func,
                                 loss_con = loss_con,
                                 args=args)
        print('Final training  {}/{}'.format(epoch, args.max_epochs - 1), 'loss: {:.4f}'.format(train_loss),
                  'time {:.2f}s'.format(time.time() - epoch_time))

        # write loss to tbx
        if writer is not None:
            writer.add_scalar('train_loss', train_loss, epoch)
            writer.add_scalar('train_acc', train_acc, epoch)
        b_new_best = False

        # save model & weight in selected epochs.
        if args.logdir is not None and args.save_checkpoint:
            save_checkpoint(model,
                                epoch,
                                args,
                                best_acc=val_acc_max,
                                filename='model_latest')
        
        # evaluate model & weight in selected epochs.
        if (epoch+1) % args.val_every == 0:
            torch.cuda.empty_cache()
            epoch_time = time.time()
            val_avg_acc,val_avg_loss = val_epoch(model,
                                    val_loader,
                                    epoch=epoch,
                                    acc_func=acc_func,
                                    loss_func=loss_func,
                                    loss_con=loss_con,
                                    model_inferer=model_inferer,
                                    args=args,
                                    post_label=post_label,
                                    post_pred=post_pred)
            # write to tbx
            if True:
                print('Final validation  {}/{}'.format(epoch, args.max_epochs - 1),
                      'acc', val_avg_acc, 'time {:.2f}s'.format(time.time() - epoch_time))
                if writer is not None:
                    writer.add_scalar('val_acc', val_avg_acc, epoch)
                    writer.add_scalar('val_loss', val_avg_loss, epoch)
                if val_avg_acc > val_acc_max:
                    print('new best ({:.6f} --> {:.6f}). '.format(val_acc_max, val_avg_acc))
                    val_acc_max = val_avg_acc
                    b_new_best = True
                    if args.logdir is not None and args.save_checkpoint:
                        save_checkpoint(model, epoch, args,filename="model_best",
                                        best_acc=val_acc_max,
                                        optimizer=optimizer,
                                        scheduler=scheduler)
        # update scheduler epoch-wise
        if scheduler is not None:
            scheduler.step()

    print('Training Finished !, Best Accuracy: ', val_acc_max)

    return val_acc_max

