import os
import torch
from torchvision.transforms import transforms
from torch.utils.data import Dataset as dataset
from PIL import Image
from torch.utils.data import DataLoader
import numpy as np


from sklearn.preprocessing import OneHotEncoder

def read_img(path):
    img = Image.open(path)
    return img


class csvloader(torch.utils.data.Dataset):
    def __init__(self,path,split='train', transform=None):
        self.split=split
        self.path=path
        self.train_img_list,self.test_img_list,self.train_label_list,self.test_label_list = self.load_all_file(os.path.join(path,"annotation"))
        self.train_transforms = transforms.Compose([
                       transforms.RandomResizedCrop(size=256, scale=(0.2, 1.)),
                        transforms.RandomHorizontalFlip(),
                        transforms.RandomApply([
                        transforms.ColorJitter(0.4, 0.4, 0.4, 0.1)], p=0.8),
                        transforms.RandomGrayscale(p=0.2),
                       transforms.ToTensor(),
                       transforms.Normalize(mean=[0.42361727, 0.22381866, 0.24462153],std=[0.2541053, 0.2311892, 0.23641701])
                   ])
        self.test_transforms = transforms.Compose([
                        transforms.Resize([256,256]),
                       transforms.ToTensor(),
                       transforms.Normalize(mean=[0.42361727, 0.22381866, 0.24462153],std=[0.2541053, 0.2311892, 0.23641701])
                   ])

        

    def __len__(self):
        if self.split=='train' :
            return len(self.train_label_list)
        if self.split=='test':
            return len(self.test_label_list)

    def __getitem__(self, index):
        img=[]
        if self.split=='train' :
            for i in range(3):
                img.append(self.train_transforms(read_img(os.path.join(self.path,self.train_img_list[(index+i)%len(self.train_img_list)]))))
                img[i]=torch.unsqueeze(img[i],dim=0)
            image=torch.cat([img[0], img[1],img[2]], dim=0)
            return image,self.train_label_list[(index+2)%len(self.train_img_list)]
        else:
            for i in range(3):
                img.append(self.test_transforms(read_img(os.path.join(self.path,self.test_img_list[(index+i)%len(self.test_img_list)]))))
                img[i]=torch.unsqueeze(img[i],dim=0)
            image=torch.cat([img[0], img[1],img[2]], dim=0)
            return image,self.test_label_list[(index+2)%len(self.test_img_list)]


    def load_all_file(self,dir_path):
        file_list = os.listdir(dir_path)
        file_numbers = len(file_list)
        train_img_list=[]
        test_img_list=[]
        train_label_list=[]
        test_label_list=[]
        for csv_file,i in zip(file_list,range(file_numbers)):
            with open(os.path.join(dir_path,csv_file), 'r') as f:
                lines =f.readlines()[1:]
                path=csv_file
                if path.split(".")[0]=='41':
                  for l in lines:
                      tokens = l.rstrip().split(',')
                      jpg_path, label = tokens
                      test_img_list.append(path.split(".")[0]+"/"+jpg_path)
                      test_label_list.append(int(label))
                else:
                    for l in lines:
                      tokens = l.rstrip().split(',')
                      jpg_path, label = tokens
                      train_img_list.append(path.split(".")[0]+"/"+jpg_path)
                      train_label_list.append(int(label))
        return train_img_list,test_img_list,train_label_list,test_label_list
        
def getcsvloader(path,batch_size):
    train_dst=csvloader(path,split='train')
    test_dst=csvloader(path,split='test')
    train_loader = DataLoader(
        train_dst, 
        batch_size=batch_size, 
        shuffle=True,
        num_workers=0, 
        pin_memory=True
    )
    test_loader = DataLoader(
        test_dst, 
        batch_size=16, 
        shuffle=True,
        num_workers=0, 
        pin_memory=True
    )
    return train_loader,test_loader

