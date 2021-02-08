from network.net_build import *
from superpix import slic, adaptive_slic, sscolor
from VGG19 import VGG19
from predata import Data_set, DataLoader
from guild_filter_code import guide_filter

import logging
import os
import sys
from glob import glob
import itertools

import torch.nn as nn
from torch import optim
import numpy as np
import torch
import torch.nn.functional as F
from PIL import Image
from torch.utils.data import DataLoader
from torch.distributions import Distribution
from functools import partial
from joblib import Parallel, delayed

dir_img = 'data/imgs/'
dir_real = 'data/real/'
dir_checkpoint = 'checkpoints/'
data_num = 0

def simple_superpixel(batch_image: np.ndarray, superpixel_fn: callable) -> np.ndarray:
  """ convert batch image to superpixel
  Args:
      batch_image (np.ndarray): np.ndarry, shape must be [b,h,w,c]
      seg_num (int, optional): . Defaults to 200.
  Returns:
      np.ndarray: superpixel array, shape = [b,h,w,c]
  """
  num_job = batch_image.shape[0]
  batch_out = Parallel(n_jobs=num_job)(delayed(superpixel_fn)
                                       (image) for image in batch_image)
  return np.array(batch_out)

class ColorShift(nn.Module):
  def __init__(self, mode='uniform'):
    super().__init__()
    self.dist: Distribution = None
    self.mode = mode

  def setup(self, device: torch.device):
    # NOTE 原论文输入的bgr图像，此处需要改为rgb
    if self.mode == 'normal':
      self.dist = torch.distributions.Normal(
          torch.tensor((0.299, 0.587, 0.114), device=device),
          torch.tensor((0.1, 0.1, 0.1), device=device))
    elif self.mode == 'uniform':
      self.dist = torch.distributions.Uniform(
          torch.tensor((0.199, 0.487, 0.014), device=device),
          torch.tensor((0.399, 0.687, 0.214), device=device))

  def forward(self, *img: torch.Tensor) -> Tuple[torch.Tensor]:
    rgb = self.dist.sample()
    return ((im * rgb[None, :, None, None]) / rgb.sum() for im in img)

class VariationLoss(nn.Module):
    def __init__(self):
        super().__init__()
        pass
    def forward(self, x):
        b, c, h, w=x.shape
        tv_h=torch.mean((x[:, :, 1:, :]-x[:, :, :-1, :])**2)
        tv_w=torch.mean((x[:, :, :, 1:]-x[:, :, :, :-1])**2)
        return (tv_h+tv_w)/(h*w*3)# try h*w*c?


def train(
          G_net,
          D_net,
          device,
          epochs=1,
          batch_size=1,
          lr=0.001,
          val_percent=0.1,
          save_cp=True,
          img_scale=0.5):


    superpixel_fn='sscolor'
    SuperPixelDict = {
      'slic': slic,
      'adaptive_slic': adaptive_slic,
      'sscolor': sscolor}
    superpixel_kwarg: dict = {'seg_num': 200}

    train_data=Data_set(dir_img, 0.5)
    train_loader = DataLoader(train_data, batch_size=batch_size, shuffle=True, num_workers=4)

    G=G_net()
    D_blur=D_net()
    D_gray=D_net()
    G_optimizer=torch.optim.Adam(G.parameters, lr=lr)
    D_optimizer=torch.optim.Adam(itertools.chain(D_blur.parameters, D_gray.parameters), lr=lr)

    vgg=VGG19()

    color_shift=ColorShift()
    color_shift.setup(device=device)

    criterion = nn.BCELoss()
    variation_loss=VariationLoss()
    l1_loss=nn.L1Loss('mean')

    self.superpixel_fn = partial(SuperPixelDict[superpixel_fn],
                                 **superpixel_kwarg)
    for epoch in range(epochs):
        G.train()
        D_blur.train()
        D_gray.train()

        for batch in train_loader:
            fake=batch['fake']
            real=batch['real']
            
            num+=1
            size=fake.size(0)

            real_label=torch.ones(size)
            fake_lable=torch.zeros(size)

            
            fake=fake.to(device=device, dtype=torch.float32)
            real=real.to(device=device, dtype=torch.float32)
            real_label=real_label.to(device=device, dtype=torch.float32)
            fake_label=fake_label.to(device=device, dtype=torch.float32)
           
            ###part of training generator
            fake_out=G(fake)
            fake_output=guide_filter(fake, fake_out, r=1)

            fake_blur=guild_filter(fake_output, fake_output, r=5)
            fake_disc_blur=D_blur(fake_blur)
            loss_fake_blur=criterion(fake_disc_blur, fake_label)

            fake_gray, =color_shift(fake_output)
            fake_disc_gray=D_gray(fake_gray)
            loss_fake_gray=criterion(fake_disc_gray, fake_label)

            VGG1=vgg(fake)
            VGG2=vgg(fake_output)

            superpixel_img=torch.from_numpy(
                        simple_superpixel(fake_output.detach().permute((0, 2, 3, 1)).cpu().numpy(),
                        self.superpixel_fn)
                        ).to(self.device).permute((0, 3, 1, 2))

            VGG3=vgg(superpixel_img)
            _, c, h, w=VGG2.shape()
            loss_superpixel=l1_loss(VGG3, VGG2)/(c*h*w)

            loss_content=l1_loss(VGG1, VGG2)/(c*h*w)

            loss_tv=variation_loss(fake_output)

            #parameters here
            w1=0.1
            w2=0.1
            w3=200.0
            w4=200.0
            w5=10000.0
            loss_sum=loss_fake_blur*w1+loss_fake_gray*w2+loss_superpixel*w3+loss_content*w4+loss_tv*w5

            G_optimizer.zero_grad()
            loss_sum.backward()
            G_optimizer.step()



