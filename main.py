import os
import numpy as np
import torch
import torch.nn.parallel
import torch.distributed as dist
import torch.multiprocessing as mp
import torch.utils.data.distributed
from monai.inferers import sliding_window_inference
from monai.losses import DiceLoss, DiceCELoss
from monai.metrics import DiceMetric
from monai.utils.enums import MetricReduction
from monai.transforms import AsDiscrete,Activations,Compose
from networks.unetr import UNETR
from networks.TCMix import TCMix
from monai.networks.nets import UNet
from monai.networks.nets import SegResNet
from utils.data_utils import get_loader
from trainer import run_training
from optimizers.lr_scheduler import LinearWarmupCosineAnnealingLR
from functools import partial
import argparse

parser = argparse.ArgumentParser(description='UNETR segmentation pipeline')
parser.add_argument('--checkpoint', default=None, help='start training from saved checkpoint')
parser.add_argument('--logdir', default='test', type=str, help='directory to save the tensorboard logs')
parser.add_argument('--pretrained_dir', default='./pretrained_models/', type=str, help='pretrained checkpoint directory')
parser.add_argument('--data_dir', default='/dataset/dataset0/', type=str, help='dataset directory')
parser.add_argument('--json_list', default='dataset_0.json', type=str, help='dataset json file')
parser.add_argument('--save', default='model.pth', type=str, help='dataset json file')
parser.add_argument('--pretrained_model_name', default='UNETR_model_best_acc.pth', type=str, help='pretrained model name')
parser.add_argument('--save_checkpoint', action='store_true', help='save checkpoint during training')
parser.add_argument('--max_epochs', default=5000, type=int, help='max number of training epochs')
parser.add_argument('--batch_size', default=1, type=int, help='number of batch size')
parser.add_argument('--sw_batch_size', default=1, type=int, help='number of sliding window batch size')
parser.add_argument('--optim_lr', default=1e-4, type=float, help='optimization learning rate')
parser.add_argument('--optim_name', default='adamw', type=str, help='optimization algorithm')
parser.add_argument('--reg_weight', default=1e-5, type=float, help='regularization weight')
parser.add_argument('--momentum', default=0.99, type=float, help='momentum')
parser.add_argument('--noamp', action='store_true', help='do NOT use amp for training')
parser.add_argument('--val_every', default=10, type=int, help='validation frequency')
parser.add_argument('--distributed', action='store_true', help='start distributed training')
parser.add_argument('--world_size', default=1, type=int, help='number of nodes for distributed training')
parser.add_argument('--rank', default=0, type=int, help='node rank for distributed training')
parser.add_argument('--dist-url', default='tcp://127.0.0.1:23456', type=str, help='distributed url')
parser.add_argument('--dist-backend', default='nccl', type=str, help='distributed backend')
parser.add_argument('--workers', default=4, type=int, help='number of workers')
parser.add_argument('--model_name', default='unetr', type=str, help='model name')
parser.add_argument('--pos_embed', default='perceptron', type=str, help='type of position embedding')
parser.add_argument('--norm_name', default='instance', type=str, help='normalization layer type in decoder')
parser.add_argument('--num_heads', default=12, type=int, help='number of attention heads in ViT encoder')
parser.add_argument('--mlp_dim', default=3072, type=int, help='mlp dimention in ViT encoder')
parser.add_argument('--hidden_size', default=768, type=int, help='hidden size dimention in ViT encoder')
parser.add_argument('--feature_size', default=16, type=int, help='feature size dimention')
parser.add_argument('--in_channels', default=1, type=int, help='number of input channels')
parser.add_argument('--out_channels', default=2, type=int, help='number of output channels')
parser.add_argument('--res_block', action='store_true', help='use residual blocks')
parser.add_argument('--conv_block', action='store_true', help='use conv blocks')
parser.add_argument('--use_normal_dataset', action='store_true', help='use monai Dataset class')
parser.add_argument('--a_min', default=-175.0, type=float, help='a_min in ScaleIntensityRanged')
parser.add_argument('--a_max', default=250.0, type=float, help='a_max in ScaleIntensityRanged')
parser.add_argument('--b_min', default=0.0, type=float, help='b_min in ScaleIntensityRanged')
parser.add_argument('--b_max', default=1.0, type=float, help='b_max in ScaleIntensityRanged')
parser.add_argument('--space_x', default=1.5, type=float, help='spacing in x direction')
parser.add_argument('--space_y', default=1.5, type=float, help='spacing in y direction')
parser.add_argument('--space_z', default=2.0, type=float, help='spacing in z direction')
parser.add_argument('--roi_x', default=96, type=int, help='roi size in x direction')
parser.add_argument('--roi_y', default=96, type=int, help='roi size in y direction')
parser.add_argument('--roi_z', default=96, type=int, help='roi size in z direction')
parser.add_argument('--dropout_rate', default=0.0, type=float, help='dropout rate')
parser.add_argument('--RandFlipd_prob', default=0.2, type=float, help='RandFlipd aug probability')
parser.add_argument('--RandRotate90d_prob', default=0.2, type=float, help='RandRotate90d aug probability')
parser.add_argument('--RandScaleIntensityd_prob', default=0.1, type=float, help='RandScaleIntensityd aug probability')
parser.add_argument('--RandShiftIntensityd_prob', default=0.1, type=float, help='RandShiftIntensityd aug probability')
parser.add_argument('--infer_overlap', default=0.5, type=float, help='sliding window inference overlap')
parser.add_argument('--lrschedule', default='warmup_cosine', type=str, help='type of learning rate scheduler')
parser.add_argument('--warmup_epochs', default=50, type=int, help='number of warmup epochs')
parser.add_argument('--resume_ckpt', action='store_true', help='resume training from pretrained checkpoint')
parser.add_argument('--resume_jit', action='store_true', help='resume training from pretrained torchscript checkpoint')
parser.add_argument('--smooth_dr', default=1e-6, type=float, help='constant added to dice denominator to avoid nan')
parser.add_argument('--smooth_nr', default=0.0, type=float, help='constant added to dice numerator to avoid zero')
parser.add_argument('--gpus', type=int,default=0)


def main():
    args = parser.parse_args()
    # opt. for autograd
    args.amp = not args.noamp
    # log dir
    args.logdir = './runs/' + args.logdir
    # start distributed learning
    if args.distributed:
        args.ngpus_per_node = torch.cuda.device_count()
        print('Found total gpus', args.ngpus_per_node)
        args.world_size = args.ngpus_per_node * args.world_size
        mp.spawn(main_worker,
                 nprocs=args.ngpus_per_node,
                 args=(args,))
    # mono card learning
    else:
        main_worker(gpu=args.gpus, args=args)

def main_worker(gpu, args):
    # start multiprocess for distributed learning
    if args.distributed:
        torch.multiprocessing.set_start_method('fork', force=True)
    np.set_printoptions(formatter={'float': '{: 0.3f}'.format}, suppress=True)
    # assign gpu number
    args.gpu = gpu
    # init distributed learning
    if args.distributed:
        # set rank
        args.rank = args.rank * args.ngpus_per_node + gpu
        dist.init_process_group(backend=args.dist_backend,
                                init_method=args.dist_url,
                                world_size=args.world_size,
                                rank=args.rank)
    # download to card
    torch.cuda.set_device(args.gpu)
    torch.backends.cudnn.benchmark = True
    # for training mode
    args.test_mode = False
    # load dataset
    loader = get_loader(args)
    print(args.rank, ' gpu', args.gpu)
    # print batchsize
    if args.rank == 0:
        print('Batch size is:', args.batch_size, 'epochs', args.max_epochs)
    # Segment voxel into small voxels for memory efficient learning
    inf_size = [args.roi_x, args.roi_y, args.roi_x]
    pretrained_dir = args.pretrained_dir

    # three types of network for benchmark
    if (args.model_name is None) or args.model_name == 'unetr':
        model = UNETR(
            in_channels=args.in_channels,
            out_channels=args.out_channels,
            img_size=(args.roi_x, args.roi_y, args.roi_z),
            feature_size=args.feature_size,
            hidden_size=args.hidden_size,
            mlp_dim=args.mlp_dim,
            num_heads=args.num_heads,
            pos_embed=args.pos_embed,
            norm_name=args.norm_name,
            conv_block=True,
            res_block=True,
            dropout_rate=args.dropout_rate)

        if args.resume_ckpt:
            # Transfer Learning loading pretrained weights
            model_dict = torch.load(os.path.join(pretrained_dir, args.pretrained_model_name))
            model_state_dict = model.state_dict()
            # load corresponding layer weights
            state_dict = {k:v for k,v in model_dict.items() if k in model_state_dict.keys()}
            #del state_dict["out.conv.conv.weight"]
            #del state_dict["out.conv.conv.bias"]
            model_state_dict.update(state_dict)
            model.load_state_dict(model_state_dict)
            print('Use pretrained weights')

    if (args.model_name is None) or args.model_name == 'unet':
        # five layers of unet. followed standardized paradigm
        model = UNet(
            spatial_dims=3,
            in_channels=args.in_channels,
            out_channels=args.out_channels,
            channels=(4,8,16,32,64),
            strides=(2,2,2,2),
            kernel_size=3,
            up_kernel_size=3,
            num_res_units=0,
            )

    if (args.model_name is None) or args.model_name == 'segres':
        # improved unet architecture
        model = SegResNet(
            spatial_dims=3,
            in_channels=args.in_channels,
            out_channels=args.out_channels,
            upsample_mode="deconv")
    if (args.model_name is None) or args.model_name == 'tcmix':
        model = TCMix(_use_multi_mix=True)

    # using dice_loss for loss function
    dice_loss = DiceCELoss(to_onehot_y=True,
                           softmax=True,
                           squared_pred=True,
                           smooth_nr=args.smooth_nr,
                           smooth_dr=args.smooth_dr)
    # validation pipeline - metric & post process
    post_label = AsDiscrete(to_onehot=True,
                            n_classes=args.out_channels)
    post_pred = AsDiscrete(argmax=True,
                           to_onehot=True,
                           n_classes=args.out_channels)
    dice_acc = DiceMetric(include_background=True,
                          reduction=MetricReduction.MEAN,
                          get_not_nans=True)
    
    #using sliding window inference for memory efficient inference
    model_inferer = partial(sliding_window_inference,
                            roi_size=inf_size,
                            sw_batch_size=args.sw_batch_size,
                            predictor=model,
                            overlap=args.infer_overlap)
    # param number
    pytorch_total_params = sum(p.numel() for p in model.parameters() if p.requires_grad)
    print('Total parameters count', pytorch_total_params)

    best_acc = 0
    start_epoch = 0
    # set model down to card
    model.cuda(args.gpu)

    if args.distributed:
        torch.cuda.set_device(args.gpu)
        if args.norm_name == 'batch':
            # synchronizing batch normalization in distrb.ln.
            model = torch.nn.SyncBatchNorm.convert_sync_batchnorm(model)
        model.cuda(args.gpu)
        # set parallel.
        model = torch.nn.parallel.DistributedDataParallel(model,
                                                          device_ids=[args.gpu],
                                                          output_device=args.gpu,
                                                          find_unused_parameters=True)
    # define optimizer
    if args.optim_name == 'adam':
        optimizer = torch.optim.Adam(model.parameters(),
                                     lr=args.optim_lr,
                                     weight_decay=args.reg_weight)
    elif args.optim_name == 'adamw':
        optimizer = torch.optim.AdamW(model.parameters(),
                                      lr=args.optim_lr,
                                      weight_decay=args.reg_weight)
    elif args.optim_name == 'sgd':
        optimizer = torch.optim.SGD(model.parameters(),
                                    lr=args.optim_lr,
                                    momentum=args.momentum,
                                    nesterov=True,
                                    weight_decay=args.reg_weight)
    else:
        raise ValueError('Unsupported Optimization Procedure: ' + str(args.optim_name))

    # define schduler (warmup 50 epochs
    if args.lrschedule == 'warmup_cosine':
        scheduler = LinearWarmupCosineAnnealingLR(optimizer,
                                                  warmup_epochs=args.warmup_epochs,
                                                  max_epochs=args.max_epochs)
    elif args.lrschedule == 'cosine_anneal':
        scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer,
                                                               T_max=args.max_epochs)
        if args.checkpoint is not None:
            scheduler.step(epoch=start_epoch)
    else:
        scheduler = None
    # push downstream for learning.
    accuracy = run_training(model=model,
                            train_loader=loader[0],
                            val_loader=loader[1],
                            optimizer=optimizer,
                            loss_func=dice_loss,
                            acc_func=dice_acc,
                            args=args,
                            model_inferer=model_inferer,
                            scheduler=scheduler,
                            start_epoch=start_epoch,
                            post_label=post_label,
                            post_pred=post_pred)
    return accuracy

if __name__ == '__main__':
    main()