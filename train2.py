import argparse
import os.path as osp
import warnings
import json
import yaml
import time
from time import gmtime, strftime
from utils.utils import *
from test import test, test_emb
from tqdm import tqdm
import torch
from torchvision.transforms import transforms as T
from torchvision.models.detection.backbone_utils import resnet_fpn_backbone

from utils.datasets import LoadImagesAndLabels, collate_fn, JointDataset, letterbox, random_affine
from utils.scheduler import GradualWarmupScheduler
from torch.optim.lr_scheduler import ReduceLROnPlateau, StepLR
from model import Jde_RCNN

import cv2
import matplotlib.pyplot as plt
import numpy as np

warnings.filterwarnings('ignore')


def train(
        save_path,
        save_every,
        img_size=(640,480),
        resume=False,
        epochs=25,
        batch_size=16,
        accumulated_batches=1,
        freeze_backbone=False,
        opt=None
):
    os.environ['CUDA_VISIBLE_DEVICES']=opt.gpu
    model_name = opt.backbone_name + '_img_size' + str(img_size[0]) + '_' + str(img_size[1]) 
    weights_path = osp.join(save_path, model_name)
    loss_log_path = osp.join(weights_path, 'loss.json')
    mkdir_if_missing(weights_path)
    cfg = {}
    cfg['width'] = img_size[0]
    cfg['height'] = img_size[1]
    cfg['backbone_name'] = opt.backbone_name
    cfg['lr'] = opt.lr
    
    if resume:
        latest_resume = osp.join(weights_path, 'latest.pt')

    torch.backends.cudnn.benchmark = True
    # root = '/home/hunter/Document/torch'
    root = '/data/dgw'

    #paths = {'CT':'./data/detect/CT_train.txt', 
    #         'ETH':'./data/detect/ETH.txt', 'M16':'./data/detect/MOT16_train.txt', 
    #         'PRW':'./data/detect/PRW_train.txt', 'CP':'./data/detect/cp_train.txt'}
    paths_trainset = {'M16':'./data/detect/MOT16_train.txt'}
    paths_valset = {'M16':'./data/detect/MOT16_val.txt'}
    transforms = T.Compose([T.ToTensor()])
    trainset = JointDataset(root=root, paths=paths_trainset, img_size=img_size, augment=True, transforms=transforms)
    valset = JointDataset(root=root, paths=paths_valset, img_size=img_size, augment=False, transforms=transforms)

    dataloader_trainset = torch.utils.data.DataLoader(trainset, batch_size=batch_size, shuffle=True,
                                                num_workers=8, pin_memory=True, drop_last=True, collate_fn=collate_fn)
    dataloader_valset = torch.utils.data.DataLoader(valset, batch_size=batch_size, shuffle=True,
                                                num_workers=8, pin_memory=True, drop_last=True, collate_fn=collate_fn)                                       
    
    backbone = resnet_fpn_backbone(opt.backbone_name, True)
    backbone.out_channels = 256

    model = Jde_RCNN(backbone, num_ID=trainset.nID, min_size=img_size[1], max_size=img_size[0],version=opt.model_version)
    model.cuda().train()

    params = [p for p in model.parameters() if p.requires_grad]
    optimizer = torch.optim.SGD(params, lr=opt.lr,
                                momentum=0.9, weight_decay=0.0005)

    # and a learning rate scheduler which decreases the learning rate by
    # 10x every 3 epochs
    lr_scheduler = torch.optim.lr_scheduler.StepLR(optimizer,
                                                step_size=10,
                                                gamma=0.1)
    cfg['num_ID'] = trainset.nID
    

    # model = torch.nn.DataParallel(model)
    start_epoch = 0
    
    if resume:
        checkpoint = torch.load(latest_resume, map_location='cpu')

        # Load weights to resume from
        print(model.load_state_dict(checkpoint['model'],strict=False))
        
        # Set optimizer
        start_epoch = checkpoint['epoch'] + 1

        del checkpoint  # current, saved
        
    else:
        with open(os.path.join(weights_path,'model.yaml'), 'w+') as f:
            yaml.dump(cfg, f)
        
    for epoch in range(epochs):
        epoch += start_epoch
        loss_epoch_log = dict(loss_total=0, loss_classifier=0, loss_box_reg=0, loss_reid=0, loss_objectness=0, loss_rpn_box_reg=0)
        for i, (imgs, labels, imgs_path, _, targets_len) in enumerate(tqdm(dataloader_trainset)):
            targets = []
            imgs = imgs.cuda()
            labels = labels.cuda()
            flag = False
            for target_len, label in zip(np.squeeze(targets_len), labels):
                ## convert the input to demanded format
                target = {}
                if target_len==0:
                    flag = True
                target['boxes'] = label[0:int(target_len), 2:6]
                target['ids'] = (label[0:int(target_len), 1]).long()
                target['labels'] = torch.ones_like(target['ids'])
                targets.append(target)
            if flag:
                continue
            losses = model(imgs, targets)
            loss = losses['loss_classifier']+ losses['loss_box_reg'] + losses['loss_objectness'] + losses['loss_rpn_box_reg']
            loss.backward()
            optimizer.step()
            lr_scheduler.step()
        ## print and log the loss

            for key, val in losses.items():
                loss_epoch_log[key] = float(val) + loss_epoch_log[key]
                
        for key, val in loss_epoch_log.items():
            loss_epoch_log[key] =loss_epoch_log[key]/i
        print("loss in epoch %d: "%(epoch))
        print(loss_epoch_log)
                

        checkpoint = {'epoch': epoch,
                      'model': model.state_dict(),
                      'optimizer':optimizer.state_dict()
                      }
        
        
        latest = osp.join(weights_path, 'latest.pt')
        torch.save(checkpoint, latest)
        if epoch % save_every == 0 and epoch != 0:
            torch.save(checkpoint, osp.join(weights_path, "weights_epoch_" + str(epoch) + ".pt"))
            mean_mAP, _, _ = test(model, dataloader_valset, print_interval=100)
        with open(loss_log_path, 'a+') as f:
            json.dump(loss_epoch_log, f) 
            f.write('\n')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--epochs', type=int, default=30, help='number of epochs')
    parser.add_argument('--batch-size', type=int, default=8, help='size of each image batch')
    parser.add_argument('--accumulated-batches', type=int, default=1, help='number of batches before optimizer step')
    parser.add_argument('--save-path', type=str, default='../',
                        help='Path for getting the trained model for resuming training (Should only be used with '
                                '--resume)')
    parser.add_argument('--save-model-after', type=int, default=5,
                        help='Save a checkpoint of model at given interval of epochs')
    parser.add_argument('--img-size', type=int, default=(960,720), nargs='+', help='pixels')
    parser.add_argument('--resume', action='store_true', help='resume training flag')
    parser.add_argument('--lr', type=float, default=-1.0, help='init lr')
    parser.add_argument('--backbone-name', type=str, default='resnet101', help='backbone name')
    parser.add_argument('--model-version', type=str, default='v1', help='model')
    parser.add_argument('--gpu', type=str, default='0', help='which gpu to use')
    opt = parser.parse_args()

    init_seeds()

    train(
        save_path=opt.save_path,
        save_every=opt.save_model_after,
        img_size=opt.img_size,
        resume=opt.resume,
        epochs=opt.epochs,
        batch_size=opt.batch_size,
        accumulated_batches=opt.accumulated_batches,
        opt=opt
    )
