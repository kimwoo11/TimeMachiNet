"""
This will contain our model
"""

from utils import *
import consts

import os
import logging
import random
from collections import OrderedDict
import cv2
import imageio
from PIL import Image
import datetime

import numpy as np
import torch
import torch.nn as nn
from torch.nn.functional import *
from torch.optim import Adam
from torch.utils.data import DataLoader
from torch.utils.data.sampler import SubsetRandomSampler
from torch.nn.functional import binary_cross_entropy_with_logits as bce_with_logits_loss


class Encoder(nn.Module):

    def __init__(self):
        super(Encoder, self).__init__()
        num_conv_layers = 6

        self.conv_layers = nn.ModuleList()

        def add_conv(layer_list, name, input, output, kernel_size, stride, activation_fcn):
            return layer_list.add_module(
                name,
                nn.Sequential(
                    nn.Conv2d(
                        in_channels=input,
                        out_channels=output,
                        kernel_size=kernel_size,
                        stride=stride,
                    ),
                    activation_fcn
                )
            )

        add_conv(self.conv_layers, 'e_conv_1', input=3, output=64, kernel_size=2, stride=2, activation_fcn=nn.ReLU())
        add_conv(self.conv_layers, 'e_conv_2', input=64, output=128, kernel_size=2, stride=2, activation_fcn=nn.ReLU())
        add_conv(self.conv_layers, 'e_conv_3', input=128, output=256, kernel_size=2, stride=2, activation_fcn=nn.ReLU())
        add_conv(self.conv_layers, 'e_conv_4', input=256, output=512, kernel_size=2, stride=2, activation_fcn=nn.ReLU())
        add_conv(self.conv_layers, 'e_conv_5', input=512, output=1024, kernel_size=2, stride=2,
                 activation_fcn=nn.ReLU())

        self.fc_1 = nn.Sequential(OrderedDict([
            ('e_fc_1', nn.Linear(
                in_features=1024 * 4 * 4,
                out_features=consts.NUM_Z_CHANNELS
            )),
            ('tanh_1', nn.Tanh())
        ]))

    def forward(self, face):
        x = face
        for conv_layer in self.conv_layers:
            x = conv_layer(x)
        x = x.view(x.size(0), -1)
        x = self.fc_1(x)
        return x


class DiscriminatorVec(nn.Module):
    def __init__(self):
        super(DiscriminatorVec, self).__init__()
        dims = (consts.NUM_Z_CHANNELS, consts.NUM_ENCODER_CHANNELS, consts.NUM_ENCODER_CHANNELS // 2,
                consts.NUM_ENCODER_CHANNELS // 4)
        self.layers = nn.ModuleList()
        for i, (in_dim, out_dim) in enumerate(zip(dims[:-1], dims[1:]), 1):
            self.layers.add_module(
                'dvec_fc_%d' % i,
                nn.Sequential(
                    nn.Linear(in_dim, out_dim),
                    nn.BatchNorm1d(out_dim),
                    nn.ReLU()
                )
            )

        self.layers.add_module(
            'dvec_fc_%d' % (i + 1),
            nn.Sequential(
                nn.Linear(out_dim, 1),
                # nn.Sigmoid()
            )
        )

    def forward(self, vector):
        output = vector
        for layer in self.layers:
            output = layer(output)
        return output


class DiscriminatorImg(nn.Module):
    def __init__(self):
        super(DiscriminatorImg, self).__init__()
        in_dims = (3, 16 + consts.NUM_AGES + consts.NUM_GENDERS, 32, 64)
        out_dims = (16, 32, 64, 128)
        self.conv_layers = nn.ModuleList()
        self.fc_layers = nn.ModuleList()
        for i, (in_dim, out_dim) in enumerate(zip(in_dims, out_dims), 1):
            self.conv_layers.add_module(
                'dimg_conv_%d' % i,
                nn.Sequential(
                    nn.Conv2d(in_dim, out_dim, kernel_size=2, stride=2),
                    nn.BatchNorm2d(out_dim),
                    nn.ReLU()
                )
            )

        self.fc_layers.add_module(
            'dimg_fc_1',
            nn.Sequential(
                nn.Linear(128 * 8 * 8, 1024),
                nn.LeakyReLU()
            )
        )

        self.fc_layers.add_module(
            'dimg_fc_2',
            nn.Sequential(
                nn.Linear(1024, 1),
                # nn.Sigmoid()
            )
        )

    def forward(self, images, labels, device):
        output = images

        for i, conv_layer in enumerate(self.conv_layers, 1):
            output = conv_layer(output)
            if i == 1:
                labels_tensor = torch.zeros(
                    torch.Size((output.size(0), labels.size(1), output.size(2), output.size(3))),
                    device=device)
                for img_idx in range(output.size(0)):
                    for label in range(labels.size(1)):
                        labels_tensor[img_idx, label, :, :] = labels[img_idx, label]  # fill a square
                output = torch.cat((output, labels_tensor), 1)

        output = output.flatten(1, -1)
        for fc_layer in self.fc_layers:
            output = fc_layer(output)

        return output


class Generator(nn.Module):
    def __init__(self):
        super(Generator, self).__init__()
        num_deconv_layers = 5
        mini_size = 8
        self.fc = nn.Sequential(
            nn.Linear(consts.NUM_Z_CHANNELS + consts.NUM_AGES + consts.NUM_GENDERS,
                      consts.NUM_GEN_CHANNELS * mini_size ** 2),
            nn.ReLU()
        )
        # need to reshape now to ?,1024,8,8

        self.deconv_layers = nn.ModuleList()

        def add_deconv(module_list, name, in_ch, out_ch, kernel, stride, act_fn):
            return module_list.add_module(
                name,
                nn.Sequential(
                    nn.ConvTranspose2d(
                        in_channels=in_ch,
                        out_channels=out_ch,
                        kernel_size=kernel,
                        stride=stride,
                    ),
                    act_fn
                )
            )

        add_deconv(self.deconv_layers, 'g_deconv_1', in_ch=1024, out_ch=1024, kernel=2, stride=2, act_fn=nn.ReLU())
        add_deconv(self.deconv_layers, 'g_deconv_2', in_ch=1024, out_ch=512, kernel=2, stride=2, act_fn=nn.ReLU())
        add_deconv(self.deconv_layers, 'g_deconv_3', in_ch=512, out_ch=256, kernel=2, stride=2, act_fn=nn.ReLU())
        add_deconv(self.deconv_layers, 'g_deconv_4', in_ch=256, out_ch=128, kernel=2, stride=2, act_fn=nn.ReLU())
        add_deconv(self.deconv_layers, 'g_deconv_5', in_ch=128, out_ch=3, kernel=1, stride=1, act_fn=nn.ReLU())
        add_deconv(self.deconv_layers, 'g_deconv_6', in_ch=3, out_ch=3, kernel=1, stride=1, act_fn=nn.Tanh())

    def forward(self, vector, age=None, gender=None):
        output = vector
        if age is not None and gender is not None:
            label = Label(age, gender).to_tensor() \
                if (isinstance(age, int) and isinstance(gender, int)) \
                else torch.cat((age, gender), 1)
            output = torch.cat((output, label), 1)  # z_l

        output = self.fc(output)
        output = output.view(output.size(0), 1024, 8, 8)
        for i, deconv_layer in enumerate(self.deconv_layers, 1):
            output = deconv_layer(output)
        return output


class Network(object):
    def __init__(self):
        self.E = Encoder()
        self.Dz = DiscriminatorVec()
        self.Dimg = DiscriminatorImg()
        self.G = Generator()

        self.eg_optimizer = Adam(list(self.E.parameters()) + list(self.G.parameters()))
        self.dz_optimizer = Adam(self.Dz.parameters())
        self.di_optimizer = Adam(self.Dimg.parameters())

        self.device = None
        if torch.cuda.is_available():
            self.cuda()
            print("On CUDA")
        else:
            self.cpu()
            print("On CPU")

    def __call__(self, *args, **kwargs):
        self.test_image(*args, **kwargs)

    def __repr__(self):
        return os.linesep.join([repr(subnet) for subnet in (self.E, self.Dz, self.G)])

    def test_image(self, image_tensor, age, gender, target, watermark):
        self.eval()
        batch = image_tensor.repeat(consts.NUM_AGES, 1, 1, 1)  # N x D x H x W
        batch.to(self.device)
        z = self.E(batch)  # N x Z
        z.to(self.device)

        gender_tensor = -torch.ones(consts.NUM_GENDERS)
        gender_tensor[int(gender)] *= -1
        gender_tensor = gender_tensor.repeat(consts.NUM_AGES, 1)  # apply gender on all images

        age_tensor = -torch.ones(consts.NUM_AGES, consts.NUM_AGES)
        for i in range(consts.NUM_AGES):
            age_tensor[i][i] *= -1  # apply the i'th age group on the i'th image

        l = torch.cat((age_tensor, gender_tensor), 1)
        l.to(self.device)
        z_l = torch.cat((z, l), 1)

        generated = self.G(z_l)

        image_tensor = image_tensor.transpose(0, 1).transpose(1, 2)  # Dimenssion transform
        image_tensor = 255 * one_sided(image_tensor.numpy())
        image_tensor = np.ascontiguousarray(image_tensor, dtype=np.uint8)

        font = cv2.FONT_HERSHEY_SIMPLEX
        bottomLeftCornerOfText = (2, 25)
        fontScale = 0.5
        fontColor = (0, 128, 0)  # dark green, should be visible on most skin colors
        lineType = 2
        cv2.putText(
            image_tensor,
            '{}, {}'.format(["Male", "Female"][gender], age),
            bottomLeftCornerOfText,
            font,
            fontScale,
            fontColor,
            lineType,

        )
        img_tensor = two_sided(torch.from_numpy(image_tensor / 255.0)).float()
        img_tensor = img_tensor.transpose(0, 1).transpose(0, 2)

        joined = torch.cat((img_tensor.unsqueeze(0), generated), 0)  # Conver one image to 1 sized batch

        save_image_normalized(tensor=joined, filename=os.path.join(target, 'menifa.png'), nrow=joined.size(0))

    def instruct(self,
              utkface_path,
              batch_size=64,
              epochs=1,
              weight_decay=1e-5,
              lr=2e-4,
              should_plot=False,
              betas=(0.9, 0.999),
              valid_size=None,
              where_to_save=None,
              models_saving='always',
              ):

        where_to_save = where_to_save or default_where_to_save()  # from utils
        train_dataset = get_utkface_dataset(utkface_path)  # from utils
        valid_dataset = get_utkface_dataset(utkface_path)
        dset_size = len(train_dataset)
        indices = list(range(dset_size))
        # split = int(np.floor(valid_size * dset_size))
        valid_size = valid_size or batch_size
        split = int(np.floor(valid_size))
        # np.random.seed(random_seed)
        np.random.shuffle(indices)
        train_idx, valid_idx = indices[split:], indices[:split]
        train_sampler = SubsetRandomSampler(train_idx)  # from utils
        valid_sampler = SubsetRandomSampler(valid_idx)  # from utils

        train_loader = DataLoader(dataset=train_dataset, batch_size=batch_size, sampler=train_sampler)
        valid_loader = DataLoader(dataset=valid_dataset, batch_size=batch_size, sampler=valid_sampler)
        idx_to_class = {v: k for k, v in train_dataset.class_to_idx.items()}

        validate_images = None
        validate_labels = None
        for ii, (images, labels) in enumerate(valid_loader, 1):
            validate_images = images.to(device=self.device)
            labels = torch.stack(
                [str_to_tensor(idx_to_class[l]).to(device=self.device) for l in list(labels.numpy())])
            validate_labels = labels.to(device=self.device)

        save_image_normalized(tensor=validate_images, filename=where_to_save + "/base.png")

        for optimizer in (self.eg_optimizer, self.dz_optimizer, self.di_optimizer):
            for param in ('weight_decay', 'betas', 'lr'):
                optimizer.param_groups[0][param] = locals()[param]

        #  TODO - write a txt file with all arguments to results folder

        loss_tracker = LossTracker('train', 'valid', 'dz', 'reg', 'ez', 'dimg', should_plot)
        where_to_save_epoch = ""
        save_count = 0
        paths_for_gif = []

        for epoch in range(1, epochs + 1):
            where_to_save_epoch = os.path.join(where_to_save, "epoch" + str(epoch))
            if not os.path.exists(where_to_save_epoch):
                os.makedirs(where_to_save_epoch)
            paths_for_gif.append(where_to_save_epoch)
            losses = defaultdict(lambda: [])
            for i, (images, labels) in enumerate(train_loader, 1):

                self.train()  # move to train mode

                images = images.to(device=self.device)
                labels = torch.stack([str_to_tensor(idx_to_class[l]).to(device=self.device)
                                      for l in list(labels.numpy())])  # todo - can remove list() ?
                labels = labels.to(device=self.device)
                # print ("DEBUG: iteration: "+str(i)+" images shape: "+str(images.shape))
                z = self.E(images)

                # Input\Output Loss
                z_l = torch.cat((z, labels), 1)
                generated = self.G(z_l)
                eg_loss = l1_loss(generated, images)
                losses['eg'].append(eg_loss.item())

                # Total Variance Regularization Loss
                reg = (
                              torch.sum(torch.abs(generated[:, :, :, :-1] - generated[:, :, :, 1:])) +
                              torch.sum(torch.abs(generated[:, :, :-1, :] - generated[:, :, 1:, :]))
                      ) / float(generated.size(0))
                reg_loss = 0.000 * l1_loss(reg, torch.zeros_like(reg))
                reg_loss.to(self.device)
                losses['reg'].append(reg_loss.item())

                # DiscriminatorZ Loss
                z_prior = two_sided(torch.rand_like(z, device=self.device))  # [-1 : 1]
                d_z_prior = self.Dz(z_prior)
                d_z = self.Dz(z)
                dz_loss_prior = bce_with_logits_loss(d_z_prior, torch.ones_like(d_z_prior))
                dz_loss = bce_with_logits_loss(d_z, torch.zeros_like(d_z))
                dz_loss_tot = (dz_loss + dz_loss_prior)
                losses['dz'].append(dz_loss_tot.item())

                # Encoder\DiscriminatorZ Loss
                ez_loss = 0.001 * bce_with_logits_loss(d_z, torch.ones_like(d_z))
                ez_loss.to(self.device)
                losses['ez'].append(ez_loss.item())

                # DiscriminatorImg Loss
                d_i_input = self.Dimg(images, labels, self.device)
                d_i_output = self.Dimg(generated, labels, self.device)

                di_input_loss = bce_with_logits_loss(d_i_input, torch.ones_like(d_i_input))
                di_output_loss = bce_with_logits_loss(d_i_output, torch.zeros_like(d_i_output))
                di_loss_tot = 0.1 * (di_input_loss + di_output_loss)
                losses['di'].append(di_loss_tot.item())

                # Generator\DiscriminatorImg Loss
                dg_loss = 0.001 * bce_with_logits_loss(d_i_output, torch.ones_like(d_i_output))
                losses['dg'].append(dg_loss.item())

                losses['uni_diff'] = uni_loss(z.cpu().detach()) - uni_loss(z_prior.cpu().detach())

                # Start back propagation

                # Back prop on Encoder\Generator
                self.eg_optimizer.zero_grad()
                loss = eg_loss + reg_loss + ez_loss + dg_loss
                loss.backward(retain_graph=True)
                self.eg_optimizer.step()

                # Back prop on DiscriminatorZ
                self.dz_optimizer.zero_grad()
                dz_loss_tot.backward(retain_graph=True)
                self.dz_optimizer.step()

                # Back prop on DiscriminatorImg
                self.di_optimizer.zero_grad()
                di_loss_tot.backward()
                self.di_optimizer.step()

                now = datetime.datetime.now()

                if save_count % 500 == 0:
                    save_count = 0
                    logging.info('[{h}:{m}[Epoch {e}, i: {c}] Loss: {t}'.format(h=now.hour, m=now.minute, e=epoch, c=i,
                                                                                t=loss.item()))
                    print(f"[{now.hour:d}:{now.minute:d}] [Epoch {epoch:d}, i {i:d}] Loss: {loss.item():f}")

                    to_save_models = models_saving == 'always'
                    cp_path = self.save(where_to_save_epoch, to_save_models=to_save_models)
                    loss_tracker.save(os.path.join(cp_path, 'losses.png'))

                save_count += 1
            cp_path = self.save(where_to_save_epoch)
            with torch.no_grad():  # validation

                self.eval()  # move to eval mode

                z = self.E(validate_images)
                z_l = torch.cat((z, validate_labels), 1)
                generated = self.G(z_l)

                loss = l1_loss(validate_images, generated)
                file_name = os.path.join(where_to_save_epoch, 'onesided_' + str(epoch) + '.png')
                save_image_normalized(tensor=generated, filename=file_name, nrow=8)

                losses['valid'].append(loss.item())

            # print(mean(epoch_eg_loss), mean(epoch_eg_valid_loss), mean(epoch_tv_loss), mean(epoch_uni_loss), cp_path)
            loss_tracker.append_many(**{k: mean(v) for k, v in losses.items()})
            loss_tracker.plot()

            logging.info(
                '[{h}:{m}[Epoch {e}] Loss: {l}'.format(h=now.hour, m=now.minute, e=epoch, l=repr(loss_tracker)))

        if models_saving == 'last':
            cp_path = self.save(where_to_save_epoch)
        loss_tracker.plot()

    def _mass_fn(self, fn_name, *args, **kwargs):
        """Apply a function to all possible Net's components.

        :return:
        """

        for class_attr in dir(self):
            if not class_attr.startswith('_'):  # ignore private members, for example self.__class__
                class_attr = getattr(self, class_attr)
                if hasattr(class_attr, fn_name):
                    fn = getattr(class_attr, fn_name)
                    fn(*args, **kwargs)

    def train(self):
        """Move Net to training mode.

        :return:
        """
        self._mass_fn('train')

    def cpu(self):
        self._mass_fn('cpu')
        self.device = torch.device('cpu')

    def load(self, path):
        """
            Load all state dicts of Net's components.
        """
        loaded = []
        for class_attr_name in dir(self):
            if not class_attr_name.startswith('_'):
                class_attr = getattr(self, class_attr_name)
                fname = os.path.join(path, consts.TRAINED_MODEL_FORMAT.format(class_attr_name))
                if hasattr(class_attr, 'load_state_dict') and os.path.exists(fname):
                    class_attr.load_state_dict(torch.load(fname)())
                    loaded.append(class_attr_name)
        print("Loaded {} from {}".format(', '.join(loaded), path))

    def save(self, path, to_save_models=True):
        """Save all state dicts of Net's components.

        :return:
        """
        if not os.path.isdir(path):
            os.mkdir(path)
        path = os.path.join(path, datetime.datetime.now().strftime("%Y%m%d%H%M%S"))
        if not os.path.isdir(path):
            os.mkdir(path)

        saved = []
        for class_attr_name in dir(self):
            if not class_attr_name.startswith('_') and to_save_models:
                class_attr = getattr(self, class_attr_name)
                if hasattr(class_attr, 'state_dict'):
                    state_dict = class_attr.state_dict
                    fname = os.path.join(path, consts.TRAINED_MODEL_FORMAT.format(class_attr_name))
                    torch.save(state_dict, fname)
                    saved.append(class_attr_name)

        print("Saved {} to {}".format(', '.join(saved) or 'nothing', path))
        return path
