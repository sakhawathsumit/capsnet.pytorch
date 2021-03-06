import math
import numpy as np

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.autograd import Variable
from torch.optim import Adam


class ConvLayer(nn.Module):
    def __init__(self, in_channels, out_channels=256, kernel_size=9):
        super(ConvLayer, self).__init__()

        self.conv = nn.Conv2d(in_channels=in_channels,
                               out_channels=out_channels,
                               kernel_size=kernel_size,
                               stride=1
                             )

    def forward(self, x):
        return F.relu(self.conv(x))


class PrimaryCaps(nn.Module):
    def __init__(self, num_capsules=8, in_channels=256, out_channels=32, kernel_size=9):
        super(PrimaryCaps, self).__init__()

        self.capsules = nn.ModuleList([
            nn.Conv2d(in_channels=in_channels, out_channels=out_channels, kernel_size=kernel_size, stride=2, padding=0) 
                          for _ in range(num_capsules)])
    
    def forward(self, x):
        u = [capsule(x) for capsule in self.capsules]
        u = torch.stack(u, dim=1)
        u = u.view(x.size(0), out_channels * 6 * 6, -1)
        return self.squash(u)
    
    def squash(self, input_tensor):
        squared_norm = (input_tensor ** 2).sum(-1, keepdim=True)
        output_tensor = squared_norm *  input_tensor / ((1. + squared_norm) * torch.sqrt(squared_norm))
        return output_tensor


class DigitCaps(nn.Module):
    def __init__(self, batch_size, num_routes, num_capsules=10, in_channels=8, out_channels=16, CUDA=False):
        super(DigitCaps, self).__init__()

        self.batch_size = batch_size
        self.num_routes = num_routes
        self.num_capsules = num_capsules
        self.CUDA = CUDA

        self.W = nn.Parameter(torch.randn(1, num_routes, num_capsules, out_channels, in_channels))

    def forward(self, x):
        batch_size = x.size(0)
        x = torch.stack([x] * self.num_capsules, dim=2).unsqueeze(4)

        W = torch.cat([self.W] * self.batch_size, dim=0)
        u_hat = torch.matmul(W, x)

        b_ij = Variable(torch.zeros(1, self.num_routes, self.num_capsules, 1))
        if self.CUDA:
            b_ij = b_ij.cuda()

        num_iterations = 3
        for iteration in range(num_iterations):
            c_ij = F.softmax(b_ij)
            c_ij = torch.cat([c_ij] * self.batch_size, dim=0).unsqueeze(4)

            s_j = (c_ij * u_hat).sum(dim=1, keepdim=True)
            v_j = self.squash(s_j)
            
            if iteration < num_iterations - 1:
                a_ij = torch.matmul(u_hat.transpose(3, 4), torch.cat([v_j] * self.num_routes, dim=1))
                b_ij = b_ij + a_ij.squeeze(4).mean(dim=0, keepdim=True)

        return v_j.squeeze(1)
    
    def squash(self, input_tensor):
        squared_norm = (input_tensor ** 2).sum(-1, keepdim=True)
        output_tensor = squared_norm *  input_tensor / ((1. + squared_norm) * torch.sqrt(squared_norm))
        return output_tensor



class Decoder(nn.Module):
    def __init__(self, num_class, img_height, img_width, CUDA=False):
        super(Decoder, self).__init__()
        
        self.num_class = num_class
        self.img_height = img_height
        self.img_width = img_width
        self.CUDA = CUDA
        self.reconstraction_layers = nn.Sequential(
            nn.Linear(16 * self.num_class, 512),
            nn.ReLU(inplace=True),
            nn.Linear(512, 1024),
            nn.ReLU(inplace=True),
            nn.Linear(1024, self.img_height * self.img_width),
            nn.Sigmoid()
        )
        
    def forward(self, x, data):
        classes = torch.sqrt((x ** 2).sum(2))
        classes = F.softmax(classes)
        
        _, max_length_indices = classes.max(dim=1)
        masked = Variable(torch.eye(self.num_class))
        if self.CUDA:
            masked = masked.cuda()
        masked = masked.index_select(dim=0, index=max_length_indices.squeeze(1).data)
        
        reconstructions = self.reconstraction_layers((x * masked[:, :, None, None]).view(x.size(0), -1))
        reconstructions = reconstructions.view(-1, 1, self.img_height, self.img_width)
        
        return reconstructions, masked



class CapsNet(nn.Module):
    def __init__(self, num_class, img_height, img_width, batch_size, conv_in_channels, 
    	conv_out_channels=256, kernel_size=9, num_primary_capsules=8, 
    	primary_out_channels=32, num_digit_capsules=10, 
    	digit_out_channels=16, num_routes=None, CUDA=False):
        super(CapsNet, self).__init__()
        
        padding = 0
        height = math.ceil((img_height - kernel_size + 2*padding) / 1 ) + 1
        height = math.ceil((height - kernel_size + 2*padding) / 2 ) + 1
        width = math.ceil((img_width - kernel_size + 2*padding) / 1 ) + 1
        width = math.ceil((width - kernel_size + 2*padding) / 2 ) + 1

        self.num_routes = primary_out_channels * height * width

        self.conv_layer = ConvLayer(in_channels=conv_in_channels, out_channels=conv_out_channels, 
        							kernel_size=kernel_size)
        
        self.primary_capsules = PrimaryCaps(num_capsules=num_primary_capsules, in_channels=conv_out_channels, 
        						out_channels=primary_out_channels, kernel_size=kernel_size)
        
        self.digit_capsules = DigitCaps(batch_size=batch_size, num_capsules=num_digit_capsules, 
        						num_routes=self.num_routes, in_channels=num_primary_capsules, 
        						out_channels=digit_out_channels, CUDA=CUDA)
        
        self.decoder = Decoder(num_class=num_class, img_height=img_height, 
        						img_width=img_width, CUDA=CUDA)
        
        self.mse_loss = nn.MSELoss()
        
    def forward(self, data):
        output = self.digit_capsules(self.primary_capsules(self.conv_layer(data)))
        reconstructions, masked = self.decoder(output, data)
        return output, reconstructions, masked
    
    def loss(self, data, x, target, reconstructions):
        return self.margin_loss(x, target) + self.reconstruction_loss(data, reconstructions)
    
    def marginLoss(self, x, labels, size_average=True):
        batch_size = x.size(0)

        v_c = torch.sqrt((x**2).sum(dim=2, keepdim=True))

        left = F.relu(0.9 - v_c).view(batch_size, -1)
        right = F.relu(v_c - 0.1).view(batch_size, -1)

        loss = labels * left + 0.5 * (1.0 - labels) * right
        loss = loss.sum(dim=1).mean()

        return loss
    
    def reconstructionLoss(self, data, reconstructions):
        loss = self.mse_loss(reconstructions.view(reconstructions.size(0), -1), data.view(reconstructions.size(0), -1))
        return loss * 0.0005

    @staticmethod
    def getParamSize(model):
        params = 0
        for p in model.parameters():
            tmp = 1
            for x in p.size():
                tmp *= x
            params += tmp
        return params


    @staticmethod
    def serialize(model, optimizer=None, epoch=None, loss_results=None,
                  accuracy=None, avg_loss=None):

        package = {
            'state_dict': model.state_dict(),
        }
        if optimizer is not None:
            package['optim_dict'] = optimizer.state_dict()
        if avg_loss is not None:
            package['avg_loss'] = avg_loss
        if epoch is not None:
            package['epoch'] = epoch + 1  # increment for readability
        if loss_results is not None:
            package['loss_results'] = loss_results
        if avg_loss is not None:
            package['avg_loss'] = avg_loss
        if accuracy is not None:
        	package['accuracy'] = accuracy
        return package
