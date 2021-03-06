import random
import os
import shutil

import torch
import torch.utils.data as data_utils
import torch.nn as nn
import torch.nn.functional as F
from tensorboardX import SummaryWriter
from tqdm import tqdm
import numpy as np
import cv2
import pandas as pd

from albumentations import (
    PadIfNeeded,
    HorizontalFlip,
    VerticalFlip,
    CenterCrop,
    Crop,
    Compose,
    Transpose,
    RandomRotate90,
    ElasticTransform,
    RandomCrop,
    GridDistortion,
    OpticalDistortion,
    RandomSizedCrop,
    OneOf,
    CLAHE,
    RandomBrightnessContrast,
    RandomGamma,
    RandomScale,
    Rotate,
    Resize
)

from utils.region import Region
from .datasets import MaskDataset
from .collector import Collector
from .metrics import iou_pytorch, accuracy_wrapper, special_accuracy, mAP_wrapper, BoundingBoxes, maP_create_boxes, mAP_wrapper_from_boxes
from .projections import process_batch_torch_wrap, process_patches


class Trainer(object):
    def __init__(self, exp_path, config, device):
        self.exp_path = exp_path
        self.device = device
        self.config = config
        self.train_data, self.val_data, self.test_data = self.load_datasets()
        print("Train", len(self.train_data))
        print("Val", len(self.val_data))
        self.criterion = self.load_criterion()
        self.model, self.proj_model = self.load_model()
        self.optim = self.load_optim()
        self.writer = self.init_board()
        self.metrics = self.init_metrics()

    def init_metrics(self):
        metrics = dict()
        metrics["iou"] = iou_pytorch
        return metrics

    def init_augmentations(self):
        # TODO: change this
        width, height = 724, 1024
        wanted_size = 256
        aug = Compose([
            Resize(height=height, width=width),
            RandomScale(scale_limit=0.5, always_apply=True),
            RandomCrop(height=wanted_size, width=wanted_size),
            PadIfNeeded(min_height=wanted_size, min_width=wanted_size, p=0.5),
            Rotate(limit=4, p=0.5),
            VerticalFlip(p=0.5),
            GridDistortion(p=0.5),
            CLAHE(p=0.8),
            RandomBrightnessContrast(p=0.8),
            RandomGamma(p=0.8)
        ])
        return aug

    def init_board(self):
        return SummaryWriter(os.path.join(self.exp_path, "runs"))

    def load_optim(self):
        parameters = list(self.model.parameters()) + list(self.proj_model.parameters())
        return torch.optim.Adam(parameters,
                                lr=self.config["train"]["lr"])

    def load_model(self):
        config = self.config["model"]
        # Different models
        from .model import UNet, Fast1D, SqueezeNet
        # TODO: fix `5` magic constant
        return UNet(**config["params"]).to(self.device), SqueezeNet(num_classes=5, pretrained=True).to(self.device)#Fast1D(outputs=5).to(self.device)

    def load_datasets(self):
        config = self.config["data"]
        aug = self.init_augmentations()

        files = []
        for in_path in config["list"]:
            in_path = os.path.join(in_path, MaskDataset.ANNOTATION_FOLDER)
            files += list(os.path.join(in_path, fn) for fn in os.listdir(in_path))

        random.seed(config["seed"])
        random.shuffle(files)
        train_files = files[:int(len(files) * config["train_fraction"])]
        val_files = files[len(train_files):]

        in_path = self.config["test"]["list"][0]
        in_path = os.path.join(in_path, MaskDataset.ANNOTATION_FOLDER)
        test_files = list(os.path.join(in_path, fn) for fn in os.listdir(in_path))

        train_dset = MaskDataset(train_files, augmentations=aug)
        val_dset = MaskDataset(val_files)
        test_dset = MaskDataset(test_files)
        return train_dset, val_dset, test_dset

    def load_criterion(self):
        return nn.BCEWithLogitsLoss()

    def train_epoch(self, epoch_number):
        config = self.config["train"]
        it = data_utils.DataLoader(self.train_data, batch_size=config["batch"], num_workers=8, shuffle=True)
        it = tqdm(it, desc="train[%d]" % epoch_number)
        self.model.train()

        collection = Collector()
        for batch_index, (img, mask, class_mask) in enumerate(it):
            img, mask = img.to(self.device), mask.to(self.device)

            self.optim.zero_grad()
            out = self.model(img)
            loss = self.criterion(out, mask)

            # loss.backward()

            out_mask = out.detach().sigmoid() > 0.5
            proj, rectangles, proj_class, image_index, true_pred_map = process_batch_torch_wrap(img.detach().cpu(), out_mask.cpu(), class_mask, filter_masks=True)
            sizes = [[w, h] for _, _, w, h in rectangles]
            if len(sizes):
                self.writer.add_scalars("batch/mean", dict(W=np.mean(sizes, 0)[0],
                                                          H=np.mean(sizes, 0)[1]), self.global_step)

            total_loss = loss
            proj_loss = torch.zeros(1)
            if proj.shape[0] == 0:
                not_enough_rects = True
            else:
                not_enough_rects = False
                proj, proj_class = proj.to(self.device), proj_class.to(self.device)
                self.writer.add_scalars("batch/proj_B_size", dict(train=proj.size(0)), self.global_step)
                # TODO: Squeeze Patch
                proj_out = self.proj_model(process_patches(img, rectangles, image_index).to(self.device))

                if (proj_class >= 0).sum() > 0:
                    proj_loss = F.cross_entropy(proj_out[proj_class >= 0], proj_class[proj_class >= 0])
                    total_loss = loss + proj_loss
            total_loss.backward()

            self.optim.step()

            # collection.add("proj_loss", proj_loss.item())
            # self.writer.add_scalars("proj_batch", dict(loss=proj_loss.item()), self.global_step)
            collection.add("total_loss", total_loss.item())
            collection.add("proj_loss", proj_loss.item())
            collection.add("segm_loss", loss.item())
            self.writer.add_scalars("batch", dict(total_loss=total_loss.item(),
                                                  segm_loss=loss.item(),
                                                  proj_loss=proj_loss.item()), self.global_step)
            batch_metrics = dict()
            for metric_name, metric_f in self.metrics.items():
                metric_slug = "metric_{}".format(metric_name)
                with torch.no_grad():
                    metric_value = metric_f(out, mask, reduce=True).item()
                collection.add(metric_slug, metric_value)
                batch_metrics[metric_slug] = metric_value
            with torch.no_grad():
                metric_slug = "metric_proj_acc"
                if not_enough_rects:
                    metric_value = 0
                else:
                    # print(proj_out.shape, proj_class.shape, (proj_class >= 0).sum())
                    metric_value = special_accuracy(proj_out[proj_class >= 0].cpu(), proj_class[proj_class >= 0].cpu(), true_pred_map).item()
                collection.add(metric_slug, metric_value)
                batch_metrics[metric_slug] = metric_value

                metric_slug = "metric_found_rects"
                metric_value = (true_pred_map >= 0).float().mean().item()
                collection.add(metric_slug, metric_value)
                batch_metrics[metric_slug] = metric_value

                if not_enough_rects is False:
                    VOC_metrics = mAP_wrapper(rectangles,
                                              pred_classes=proj_out,
                                              image_indeces=image_index,
                                              label_mask=class_mask)
                    AP = np.mean([row["AP"] for row in VOC_metrics])
                    metric_slug = "VOC_Metrics_AP"
                    collection.add(metric_slug, AP)
                    batch_metrics[metric_slug] = AP


            self.writer.add_scalars("batch", batch_metrics, self.global_step)

            it.set_postfix(loss=loss.item(), **batch_metrics)
            class_counts = pd.Series.value_counts(proj_class.cpu().detach().numpy()).to_dict()
            class_counts = {class_name: class_counts.get(class_index, 0) for class_index, class_name in enumerate(Region.CATEGORIES)}
            self.writer.add_scalars("batch/proj_class_dist", class_counts, self.global_step)

            self.global_step += 1


            if batch_index == 0 and not_enough_rects is False:
                self._write_images_with_class("train", img, out_mask, rectangles, proj_out, proj_class, image_index, epoch_number)
            elif batch_index == 0:
                self._write_images("train", img, out.sigmoid(), epoch_number)

        epoch_reduced_metrics = {metric_name: np.mean(collection[metric_name]) for metric_name in collection.keys()}
        epoch_loss = epoch_reduced_metrics.pop("total_loss")
        return epoch_loss, epoch_reduced_metrics

    def val_epoch(self, epoch_number, name="val", data=None):
        config = self.config[name]
        it = data_utils.DataLoader(data, batch_size=config["batch"], num_workers=8, shuffle=False)
        it = tqdm(it, desc="%s[%d]" % (name, epoch_number))
        self.model.eval()

        collection = Collector()
        for batch_index, (img, mask, class_mask) in enumerate(it):
            img, mask = img.to(self.device), mask.to(self.device)

            with torch.no_grad():
                out = self.model(img)
                loss = self.criterion(out, mask)

                out_mask = out.detach().sigmoid() > 0.5
                proj, rectangles, proj_class, image_index, true_pred_map = process_batch_torch_wrap(img.detach().cpu(), out_mask.cpu(), class_mask, filter_masks=False)

                total_loss = loss
                proj_loss = torch.zeros(1)
                if proj.shape[0] == 0:
                    not_enough_rects = True
                else:
                    not_enough_rects = False
                    proj, proj_class = proj.to(self.device), proj_class.to(self.device)
                    patches = process_patches(img, rectangles, image_index).to(self.device)
                    if patches.shape[0]:
                        proj_out = []
                        for start in range(0, len(patches), 32):
                            proj_out_slice = self.proj_model(patches[start: start+32])
                            proj_out.append(proj_out_slice)
                        proj_out = torch.cat(proj_out)

                    if (proj_class >= 0).sum() > 0:
                        proj_loss = F.cross_entropy(proj_out[proj_class >= 0], proj_class[proj_class >= 0])
                        total_loss = loss + proj_loss

            collection.add("total_loss", total_loss.item())
            collection.add("proj_loss", proj_loss.item())
            collection.add("segm_loss", loss.item())
            batch_metrics = dict()
            for metric_name, metric_f in self.metrics.items():
                metric_slug = "metric_{}".format(metric_name)
                with torch.no_grad():
                    metric_value = metric_f(out, mask, reduce=True).item()
                collection.add(metric_slug, metric_value)
                batch_metrics[metric_slug] = metric_value
            with torch.no_grad():
                metric_slug = "metric_proj_acc"
                if not_enough_rects or (proj_class >= 0).sum() == 0:
                    metric_value = 0
                else:
                    metric_value = special_accuracy(proj_out.cpu(), proj_class.cpu(), true_pred_map).item()
                collection.add(metric_slug, metric_value)
                batch_metrics[metric_slug] = metric_value

                metric_slug = "metric_found_rects"
                metric_value = (true_pred_map >= 0).float().mean().item()
                collection.add(metric_slug, metric_value)
                batch_metrics[metric_slug] = metric_value

                if not_enough_rects is False:
                    VOC_metrics = mAP_wrapper(rectangles,
                                      pred_classes=proj_out,
                                      image_indeces=image_index,
                                      label_mask=class_mask)
                    AP = np.mean([row["AP"] for row in VOC_metrics])
                    metric_slug = "VOC_Metrics_AP"
                    collection.add(metric_slug, AP)
                    batch_metrics[metric_slug] = AP


            it.set_postfix(loss=loss.item(), **batch_metrics)

            if batch_index == 0 and not_enough_rects is False:
                self._write_images_with_class(name, img, out_mask, rectangles, proj_out, proj_class, image_index, epoch_number)
            elif batch_index == 0:
                self._write_images(name, img, out.sigmoid(), epoch_number)

        epoch_reduced_metrics = {metric_name: np.mean(collection[metric_name]) for metric_name in collection.keys()}
        epoch_loss = epoch_reduced_metrics.pop("total_loss")
        return epoch_loss, epoch_reduced_metrics

    def calc_metrics(self, epoch_number, name="val", data=None, batchsize=4):
        it = data_utils.DataLoader(data, batch_size=batchsize, num_workers=8, shuffle=False)
        it = tqdm(it, desc="%s[%d]" % (name, epoch_number))
        self.model.eval()

        collection = Collector()
        boxes = BoundingBoxes()
        iou = []
        for batch_index, (img, mask, class_mask) in enumerate(it):
            img, mask = img.to(self.device), mask.to(self.device)

            with torch.no_grad():
                out = self.model(img)
                loss = self.criterion(out, mask)

                out_mask = out.detach().sigmoid() > 0.5
                proj, rectangles, proj_class, image_index, true_pred_map = process_batch_torch_wrap(img.detach().cpu(), out_mask.cpu(), class_mask, filter_masks=False)

                total_loss = loss
                proj_loss = torch.zeros(1)
                if proj.shape[0] == 0:
                    not_enough_rects = True
                else:
                    not_enough_rects = False
                    proj, proj_class = proj.to(self.device), proj_class.to(self.device)
                    patches = process_patches(img, rectangles, image_index).to(self.device)
                    if patches.shape[0]:
                        proj_out = []
                        for start in range(0, len(patches), 32):
                            proj_out_slice = self.proj_model(patches[start: start+32])
                            proj_out.append(proj_out_slice)
                        proj_out = torch.cat(proj_out)

                    if (proj_class >= 0).sum() > 0:
                        proj_loss = F.cross_entropy(proj_out[proj_class >= 0], proj_class[proj_class >= 0])
                        total_loss = loss + proj_loss

            collection.add("total_loss", total_loss.item())
            collection.add("proj_loss", proj_loss.item())
            collection.add("segm_loss", loss.item())

            iou += iou_pytorch(out, mask, reduce=False).detach().cpu().numpy().tolist()

            batch_metrics = dict()
            with torch.no_grad():
                metric_slug = "metric_proj_acc"
                if not_enough_rects or (proj_class >= 0).sum() == 0:
                    metric_value = 0
                else:
                    metric_value = special_accuracy(proj_out.cpu(), proj_class.cpu(), true_pred_map).item()
                collection.add(metric_slug, metric_value)
                batch_metrics[metric_slug] = metric_value

                metric_slug = "metric_found_rects"
                metric_value = (true_pred_map >= 0).float().mean().item()
                collection.add(metric_slug, metric_value)
                batch_metrics[metric_slug] = metric_value

                if not_enough_rects is False:
                    VOC_boxes = maP_create_boxes(rectangles,
                                      pred_classes=proj_out,
                                      image_indeces=image_index,
                                      label_mask=class_mask, relative_index=len(boxes))
                    for box in VOC_boxes:
                        boxes.addBoundingBox(box)

            it.set_postfix(loss=loss.item(), **batch_metrics)

            if batch_index == 0 and not_enough_rects is False:
                self._write_images_with_class(name, img, out_mask, rectangles, proj_out, proj_class, image_index, epoch_number)
            elif batch_index == 0:
                self._write_images(name, img, out.sigmoid(), epoch_number)

        VOC_metrics = mAP_wrapper_from_boxes(boxes)
        print("AP", [row["AP"] for row in VOC_metrics])
        AP = np.mean([row["AP"] for row in VOC_metrics])
        IOU = np.mean(iou)

        epoch_reduced_metrics = {metric_name: np.mean(collection[metric_name]) for metric_name in collection.keys()}
        epoch_loss = epoch_reduced_metrics.pop("total_loss")
        epoch_reduced_metrics["AP"] = AP
        epoch_reduced_metrics["IOU"] = IOU
        return epoch_loss, epoch_reduced_metrics

    def _write_images_with_class(self, general_tag, imgs, pred_masks, rectangles, pred_classes, true_classes, image_index, epoch):
        # B, C, W, H
        imgs = imgs.detach().cpu().squeeze(1).numpy() * 255
        pred_masks = pred_masks.detach().cpu().squeeze(1).numpy() > 0.5
        # print(pred_masks.shape, pred_masks.dtype)
        N = imgs.shape[0]

        def map_class(index):
            if index >= 0:
                return Region.CATEGORIES[index]
            return "bad"

        # print(classes.shape)
        rectangles = rectangles.detach().cpu().numpy()
        pred_classes = pred_classes.detach().cpu().argmax(1).numpy()
        true_classes = true_classes.detach().cpu().numpy()

        grouped_rectangles = [[] for _ in range(N)]
        grouped_classes = [[] for _ in range(N)]
        for i, image_i in enumerate(image_index):
            grouped_classes[image_i].append([true_classes[i], pred_classes[i]])
            grouped_rectangles[image_i].append(rectangles[i])

        for image_index in range(N):
            img = cv2.cvtColor(imgs[image_index], cv2.COLOR_GRAY2RGB).astype(np.float)
            mask = pred_masks[image_index]
            img[mask] = img[mask] / 2.0 + [127.5, 0.0, 0.0]
            regions = grouped_rectangles[image_index]
            classes = grouped_classes[image_index]
            for i, (region_i, class_i) in enumerate(zip(regions, classes)):
                x,y,w,h = region_i
                cv2.rectangle(img, (x, y), (x + w, y + h), (0, 255, 0), 2)
                true_class_i, pred_class_i = class_i
                label = "t:{};p:{}".format(map_class(true_class_i),
                                           map_class(pred_class_i))
                # cv2.drawContours(img, [region.contour], 0, (0, 255, 0), 2)

                font = cv2.FONT_HERSHEY_SIMPLEX
                fontScale = 0.55
                lineType = 1
                t_size = cv2.getTextSize(label, font, fontScale, lineType)[0]
                top_left_corner = x, y
                right_bottom_corner = x + t_size[0] + 3, y + t_size[1] + 4

                cv2.rectangle(img, top_left_corner, right_bottom_corner, (0, 255, 0), -1)
                cv2.putText(img, label, (top_left_corner[0]+1, top_left_corner[1]+t_size[1]+2),
                            font, fontScale,
                            [225, 255, 255], lineType)

            img = torch.from_numpy(img.transpose(2, 0, 1) / 255.0)
            self.writer.add_image("{}/image-{}".format(general_tag, image_index + 1), img, epoch)

    def _write_images(self, general_tag, imgs, masks, epoch):
        # B, C, W, H
        imgs = imgs.detach().cpu().squeeze(1).numpy() * 255
        # B, C, W, H
        masks = masks.detach().cpu().squeeze(1).numpy() > 0.5
        for image_index in range(len(imgs)):
            img = cv2.cvtColor(imgs[image_index], cv2.COLOR_GRAY2RGB).astype(np.float)
            mask = masks[image_index]
            img[mask] = img[mask] / 2 + [0.0, 127.5, 0.0]
            img = torch.from_numpy(img.transpose(2, 0, 1) / 255.0)
            self.writer.add_image("{}/image-{}".format(general_tag, image_index + 1), img, epoch)

    def train(self):
        self.global_step = 0
        self.val_global_step = 0
        best_value = None
        for i_epoch in range(self.config["train"]["epochs"]):
            self.epoch = i_epoch
            self.train_epoch(self.epoch)
            train_loss, train_metrics = self.calc_metrics(self.epoch,
                                                          name="train",
                                                          data=self.train_data,
                                                          batchsize=self.config["train"]["batch"])
            print("Train loss epoch[{}] = {}".format(i_epoch, train_loss))
            val_loss, val_metrics = self.calc_metrics(self.epoch, name="val", data=self.val_data,
                                                      batchsize=self.config["val"]["batch"])
            print("Val loss epoch[{}] = {}".format(i_epoch, val_loss))
            test_loss, test_metrics = self.calc_metrics(self.epoch, name="test", data=self.test_data,
                                                        batchsize=self.config["test"]["batch"])
            print("Test loss epoch[{}] = {}".format(i_epoch, test_loss))
            self.writer.add_scalars("epoch/total_loss", dict(train=train_loss, val=val_loss, test=test_loss), i_epoch)
            for metric_name in train_metrics.keys():
                self.writer.add_scalars("epoch/{}".format(metric_name),
                                        dict(train=train_metrics.get(metric_name, 0),
                                             val=val_metrics.get(metric_name, 0),
                                             test=test_metrics.get(metric_name, 0),
                                             ), i_epoch)

            torch.save(self.model.state_dict(), os.path.join(self.exp_path, "current_model.h5"))
            if best_value is None or val_loss < best_value:
                print("Upgrade in LOSS!")
                best_value = val_loss
                shutil.copy(os.path.join(self.exp_path, "current_model.h5"),
                            os.path.join(self.exp_path, "best_model.h5"))
