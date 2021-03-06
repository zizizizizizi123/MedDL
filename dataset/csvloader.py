import os
import torch
from torchvision.transforms import transforms
from torch.utils.data import Dataset as dataset
from PIL import Image
from torch.utils.data import WeightedRandomSampler
from torch.utils.data import DataLoader
import numpy as np
import random


from sklearn.preprocessing import OneHotEncoder

def read_img(path):
    img = Image.open(path)
    return img


class csvloader(torch.utils.data.Dataset):
    def __init__(self,path,split='train', transform=None):
        self.split=split
        self.path=path
        self.img_list,self.label_list = self.load_all_file(os.path.join(path,"annotation"))
        self.train_transforms = transforms.Compose([
                       transforms.RandomResizedCrop(size=256, scale=(0.2, 1.)),
                        transforms.RandomHorizontalFlip(),
                        transforms.RandomApply([
                        transforms.ColorJitter(0.4, 0.4, 0.4, 0.1)], p=0.8),
                       transforms.ToTensor(),
                       transforms.Normalize(mean=[0.78277665, 0.53458846, 0.55787057],std=[0.13301167, 0.14758444, 0.16624169])
                   ])
        self.test_transforms = transforms.Compose([
                        transforms.Resize([256,256]),
                       transforms.ToTensor(),
                       transforms.Normalize(mean=[0.78277665, 0.53458846, 0.55787057],std=[0.13301167, 0.14758444, 0.16624169])
                   ])

        

    def __len__(self):
        return len(self.img_list)

    def __getitem__(self, index):
        '''if self.split=='train' :
            img=read_img(os.path.join(self.path,"images",self.rand_get_img(self.img_list[index])))
            img=self.train_transforms(img)
            return img,self.label_list[index]
        else:
            img=read_img(os.path.join(self.path,"images",self.img_list[index]))
            img=self.test_transforms(img)
            return img,self.label_list[index]'''
        img=read_img(os.path.join(self.path,"images",self.img_list[index]))
        if self.split=='train' :
            img=self.train_transforms(img)
            return img,self.label_list[index]
        else:
            img=self.test_transforms(img)
            return img,self.label_list[index]

    def rand_get_img(self,img_list):
        return img_list[random.randint(0,len(img_list)-1)]
        
        
    def load_all_file(self,dir_path):
        '''if self.split=='train' :
            file_list = dir_path+"/"+self.split+".csv"
            with open(file_list, 'r') as f:
                lines =f.readlines()[1:]
                img_list=[[] for i in range(7)]
                label_list=[]
                for l in lines:
                    tokens = l.rstrip().split(',')
                    jpg_path, label = tokens
                    img_list[int(label)].append(jpg_path)
                    label_list.append(int(label)'''
        #else:
        file_list = dir_path+"/"+self.split+".csv"
        with open(file_list, 'r') as f:
            lines =f.readlines()[1:]
            img_list=[]
            label_list=[]
            for l in lines:
                tokens = l.rstrip().split(',')
                jpg_path, label = tokens
                img_list.append(jpg_path)
                label_list.append(int(label))
        return img_list,label_list
        
def getcsvloader(path,batch_size):
    train_dst=csvloader(path,split='train')
    test_dst=csvloader(path,split='test')
    samples_weight=[8.97,1.48,20.33,30.5,9.24,101.66,76.25]
    sampler = WeightedRandomSampler(samples_weight,500)
    train_loader = DataLoader(
        train_dst, 
        batch_size=batch_size, 
        shuffle=True,
        num_workers=0, 
        pin_memory=True
    )
    test_loader = DataLoader(
        test_dst, 
        batch_size=1, 
        shuffle=True,
        num_workers=0, 
        pin_memory=True
    )
    return train_loader,test_loader

