# Modified by Qianyu Zhou and Lu He
# ------------------------------------------------------------------------
# Modified from Deformable DETR
# Copyright (c) 2020 SenseTime. All Rights Reserved.
# Licensed under the Apache License, Version 2.0 [see LICENSE for details]
# ------------------------------------------------------------------------
# Modified from DETR (https://github.com/facebookresearch/detr)
# Copyright (c) Facebook, Inc. and its affiliates. All Rights Reserved
# ------------------------------------------------------------------------

"""
Train and eval functions used in main.py
"""
import math
import os
import sys
from typing import Iterable

import torch
import util.misc as utils
from datasets.coco_eval import CocoEvaluator
from datasets.panoptic_eval import PanopticEvaluator
from datasets.data_prefetcher_multi import data_prefetcher
import wandb

def train_one_epoch(model: torch.nn.Module, criterion: torch.nn.Module,
                    data_loader: Iterable, optimizer: torch.optim.Optimizer,
                    device: torch.device, epoch: int, max_norm: float = 0):
    model.train()
    criterion.train()
    metric_logger = utils.MetricLogger(delimiter="  ")
    metric_logger.add_meter('lr', utils.SmoothedValue(window_size=1, fmt='{value:.6f}'))
    metric_logger.add_meter('class_error', utils.SmoothedValue(window_size=1, fmt='{value:.2f}'))
    # metric_logger.add_meter('grad_norm', utils.SmoothedValue(window_size=1, fmt='{value:.2f}'))
    header = 'Epoch: [{}]'.format(epoch)
    print_freq = 10


    # prefetcher = data_prefetcher(data_loader, device, prefetch=True)
    # data_loader_iter = iter(data_loader)
    # samples, targets = data_loader_iter.next()
    # samples = samples.to(device)
    # targets = [{k: v.to(device) for k, v in t.items()} for t in targets]
    NUM_ACCUMULATION_STEPS = 8
    iter = 0
    for samples, targets in metric_logger.log_every(data_loader, print_freq, header):
    # for _ in metric_logger.log_every(range(len(data_loader)), print_freq, header):

        # assert samples is None, samples
        # outputs = model(samples)
        samples = samples.to(device)
        #print("engine_target_shape",targets)
        targets = [{k: v.to(device) for k, v in t.items()} for t in targets[0]]
        # print("targets", targets)
        # print("input model", type(samples))
        outputs = model(samples)
        loss_dict = criterion(outputs, targets)
        weight_dict = criterion.weight_dict
        losses = sum(loss_dict[k] * weight_dict[k] for k in loss_dict.keys() if k in weight_dict)
 
        # reduce losses over all GPUs for logging purposes
        loss_dict_reduced = utils.reduce_dict(loss_dict)
        loss_dict_reduced_unscaled = {f'{k}_unscaled': v
                                      for k, v in loss_dict_reduced.items()}
        loss_dict_reduced_scaled = {k: v * weight_dict[k]
                                    for k, v in loss_dict_reduced.items() if k in weight_dict}
        losses_reduced_scaled = sum(loss_dict_reduced_scaled.values())

        loss_value = losses_reduced_scaled.item()

        if not math.isfinite(loss_value):
            print("Loss is {}, stopping training".format(loss_value))
            print(loss_dict_reduced)
            sys.exit(1)

        losses = losses / NUM_ACCUMULATION_STEPS
        losses.backward()

        if ((iter + 1) % NUM_ACCUMULATION_STEPS == 0) or (iter + 1 == len(data_loader)):
            if max_norm > 0:
                grad_total_norm = torch.nn.utils.clip_grad_norm_(model.parameters(), max_norm)

            optimizer.step()
            optimizer.zero_grad()

            wandb.log({"lr": optimizer.param_groups[0]["lr"]})
            wandb.log({"class_error": loss_dict_reduced['class_error']})
            wandb.log({"grad_norm": grad_total_norm})
            wandb.log({"loss": loss_value})
            wandb.log({"loss_ce": loss_dict_reduced_unscaled['loss_ce_unscaled'].item()})
            wandb.log({"loss_bbox": loss_dict_reduced_unscaled['loss_bbox_unscaled'].item()})
            wandb.log({"loss_giou": loss_dict_reduced_unscaled['loss_giou_unscaled'].item()})
       

        metric_logger.update(loss=loss_value, **loss_dict_reduced_scaled, **loss_dict_reduced_unscaled)
        metric_logger.update(class_error=loss_dict_reduced['class_error'])
        metric_logger.update(lr=optimizer.param_groups[0]["lr"])

        # try:
        #     metric_logger.update(grad_norm=grad_total_norm)
        # except:
        #     grad_total_norm_noclip = utils.get_total_grad_norm(model.parameters(), max_norm)
        #     metric_logger.update(grad_norm=grad_total_norm_noclip)

        iter += 1
        # samples, ref_samples, targets = prefetcher.next()
        # try: 
        #     samples, targets = data_loader_iter.next()
        # except StopIteration:
        #     data_loader_iter = iter(data_loader)
        #     samples,targets = data_loader_iter.next()
        # samples = samples.to(device)
        # targets = [{k: v.to(device) for k, v in t.items()} for t in targets]
    # gather the stats from all processes
    metric_logger.synchronize_between_processes()
    print("Averaged stats:", metric_logger)
    return {k: meter.global_avg for k, meter in metric_logger.meters.items()}
import time 
import numpy as np 

@torch.no_grad()
def evaluate(model, criterion, postprocessors, data_loader, base_ds, device, output_dir, data_root):
    model.eval()
    criterion.eval()

    metric_logger = utils.MetricLogger(delimiter="  ")
    metric_logger.add_meter('class_error', utils.SmoothedValue(window_size=1, fmt='{value:.2f}'))
    header = 'Test:'

    iou_types = tuple(k for k in ('segm', 'bbox') if k in postprocessors.keys())
    coco_evaluator = CocoEvaluator(base_ds, iou_types)
    # coco_evaluator.coco_eval[iou_types[0]].params.iouThrs = [0, 0.1, 0.5, 0.75]

    panoptic_evaluator = None
    if 'panoptic' in postprocessors.keys():
        panoptic_evaluator = PanopticEvaluator(
            data_loader.dataset.ann_file,
            data_loader.dataset.ann_folder,
            output_dir=os.path.join(output_dir, "panoptic_eval"),
        )
    iter_ = 0
    overall_result = {}
    for samples, targets  in metric_logger.log_every(data_loader, 10, header):
        samples = samples.to(device)
        targets = [{k: v.to(device) for k, v in t.items() if k!='path'} for t in targets[0]]

        outputs = model(samples)
        loss_dict = criterion(outputs, targets)
        weight_dict = criterion.weight_dict

        # reduce losses over all GPUs for logging purposes
        loss_dict_reduced = utils.reduce_dict(loss_dict)
        loss_dict_reduced_scaled = {k: v * weight_dict[k]
                                    for k, v in loss_dict_reduced.items() if k in weight_dict}
        loss_dict_reduced_unscaled = {f'{k}_unscaled': v
                                      for k, v in loss_dict_reduced.items()}
        metric_logger.update(loss=sum(loss_dict_reduced_scaled.values()),
                             **loss_dict_reduced_scaled,
                             **loss_dict_reduced_unscaled)
        metric_logger.update(class_error=loss_dict_reduced['class_error'])

        orig_target_sizes = torch.stack([t["orig_size"] for t in targets], dim=0)
        results = postprocessors['bbox'](outputs, orig_target_sizes)
        if 'segm' in postprocessors.keys():
            target_sizes = torch.stack([t["size"] for t in targets], dim=0)
            results = postprocessors['segm'](results, outputs, orig_target_sizes, target_sizes)
        res = {target['image_id'].item(): output for target, output in zip(targets, results)}
        if coco_evaluator is not None:
            coco_evaluator.update(res)
        
        ############################################################################################
        # LOG PREDICTION IMAGE TO WANDB TODO PRIY
        ############################################################################################
        for img_id_ in res.keys():
            boxes = res[img_id_]['boxes']
            labels = res[img_id_]['labels']
            scores = res[img_id_]['scores'] 

            boxes = boxes[labels!=0]
            scores = scores[labels!=0]
            labels = labels[labels!=0]

            THRESHOLD = 0.10

            boxes = boxes[scores>=THRESHOLD]
            labels = labels[scores>=THRESHOLD]
            scores = scores[scores>=THRESHOLD]

            img_info = base_ds.loadImgs([img_id_])[0]
            file_name = img_info['file_name']
            import PIL
            img_pil = PIL.Image.open(os.path.join(data_root, file_name))
            width, height = img_pil.size
            overall_result[img_id_] = {"predictions": {'box_data': [{'position': 
                                                            {"xmin": 1.*(box[0].to(torch.float32).item())/width, 
                                                            'xmax': 1.*(box[2].to(torch.float32).item())/width, 
                                                            'ymin': 1.*(box[1].to(torch.float32).item())/height, 
                                                            'ymax': 1.*(box[3].to(torch.float32).item())/height}, 
                                                            "class_id": int(labels[i].item()),
                                                            "score": round(1.*scores[i].to(torch.float32).item(), 3),
                                                            "box_caption": f"score: {round(1.*scores[i].to(torch.float32).item(), 3)}, class: {base_ds.loadCats([int(labels[i].item())])[0]['name']}"
                                                            } for i, box in enumerate(boxes)]}}


        # if iter_ == 0:

        #     THRESHOLD = 0.25

        #     boxes = boxes[scores>=THRESHOLD]
        #     labels = labels[scores>=THRESHOLD]
        #     scores = scores[scores>=THRESHOLD]

        #     boxes_wandb = {"predictions": {'box_data': [{'position': 
        #                                                  {"minX": 1.*(box[0].to(torch.float32).item())/width, 
        #                                                   'maxX': 1.*(box[2].to(torch.float32).item())/width, 
        #                                                   'minY': 1.*(box[1].to(torch.float32).item())/height, 
        #                                                   'maxY': 1.*(box[3].to(torch.float32).item())/height}, 
        #                                                   "class_id": int(labels[i].item()),
        #                                                   "box_caption": f"score: {round(1.*scores[i].to(torch.float32).item(), 3)}, class: {base_ds.loadCats([int(labels[i].item())])[0]['name']}"
        #                                                   } for i, box in enumerate(boxes)]}}

        #     # print(boxes_wandb)
        #     img_wandb = wandb.Image(img_pil, boxes=boxes_wandb)
        #     wandb.log({'test image': img_wandb})
        # iter_ += 1
        ############################################################################################
        ############################################################################################
        
        # ############################################################################################
        # # LOG PREDICTION IMAGE TO WANDB TODO PRIY
        # ############################################################################################
        # if iter_ == 1:
        #     img_wandbs = []
        #     for img_id in res.keys():
        #         img_id_= img_id
        #         boxes = res[img_id_]['boxes']
        #         labels = res[img_id_]['labels']
        #         scores = res[img_id_]['scores'] 

        #         boxes = boxes[labels!=0]
        #         scores = scores[labels!=0]
        #         labels = labels[labels!=0]

        #         THRESHOLD = 0.25

        #         boxes = boxes[scores>=THRESHOLD]
        #         labels = labels[scores>=THRESHOLD]
        #         scores = scores[scores>=THRESHOLD]

        #         img_info = base_ds.loadImgs([img_id_])[0]
        #         file_name = img_info['file_name']
        #         import PIL
        #         img_pil = PIL.Image.open(os.path.join(data_root, file_name))
        #         width, height = img_pil.size

        #         boxes_wandb = {"predictions": {'box_data': [{'position': 
        #                                                     {"minX": 1.*(box[0].to(torch.float32).item())/width, 
        #                                                     'maxX': 1.*(box[2].to(torch.float32).item())/width, 
        #                                                     'minY': 1.*(box[1].to(torch.float32).item())/height, 
        #                                                     'maxY': 1.*(box[3].to(torch.float32).item())/height}, 
        #                                                     "class_id": int(labels[i].item()),
        #                                                     "box_caption": f"score: {round(1.*scores[i].to(torch.float32).item(), 3)}, class: {base_ds.loadCats([int(labels[i].item())])[0]['name']}"
        #                                                     } for i, box in enumerate(boxes)]}}

        #         # print(boxes_wandb)
        #         img_wandb = wandb.Image(img_pil, boxes=boxes_wandb)
        #         img_wandbs.append(img_wandb)

        #     wandb.log({'test image': img_wandbs})
        # iter_ += 1
        # ############################################################################################
        # ############################################################################################

        if panoptic_evaluator is not None:
            res_pano = postprocessors["panoptic"](outputs, target_sizes, orig_target_sizes)
            for i, target in enumerate(targets):
                image_id = target["image_id"].item()
                file_name = f"{image_id:012d}.png"
                res_pano[i]["image_id"] = image_id
                res_pano[i]["file_name"] = file_name

            panoptic_evaluator.update(res_pano)

    # gather the stats from all processes
    metric_logger.synchronize_between_processes()
    print("Averaged stats:", metric_logger)
    if coco_evaluator is not None:
        coco_evaluator.synchronize_between_processes()
    if panoptic_evaluator is not None:
        panoptic_evaluator.synchronize_between_processes()

    # accumulate predictions from all images
    if coco_evaluator is not None:
        coco_evaluator.accumulate()
        coco_evaluator.summarize()
    panoptic_res = None
    if panoptic_evaluator is not None:
        panoptic_res = panoptic_evaluator.summarize()
    stats = {k: meter.global_avg for k, meter in metric_logger.meters.items()}
    if coco_evaluator is not None:
        if 'bbox' in postprocessors.keys():
            stats['coco_eval_bbox'] = coco_evaluator.coco_eval['bbox'].stats.tolist()
        if 'segm' in postprocessors.keys():
            stats['coco_eval_masks'] = coco_evaluator.coco_eval['segm'].stats.tolist()
    if panoptic_res is not None:
        stats['PQ_all'] = panoptic_res["All"]
        stats['PQ_th'] = panoptic_res["Things"]
        stats['PQ_st'] = panoptic_res["Stuff"]
    return stats, coco_evaluator, overall_result


@torch.no_grad()
def evaluate1(model, criterion, postprocessors, data_loader, base_ds, device, output_dir):
    model.eval()
    criterion.eval()

    metric_logger = utils.MetricLogger(delimiter="  ")
    metric_logger.add_meter('class_error', utils.SmoothedValue(window_size=1, fmt='{value:.2f}'))
    header = 'Test:'

    iou_types = tuple(k for k in ('segm', 'bbox') if k in postprocessors.keys())
    coco_evaluator = CocoEvaluator(base_ds, iou_types)
    # coco_evaluator.coco_eval[iou_types[0]].params.iouThrs = [0, 0.1, 0.5, 0.75]

    panoptic_evaluator = None
    if 'panoptic' in postprocessors.keys():
        panoptic_evaluator = PanopticEvaluator(
            data_loader.dataset.ann_file,
            data_loader.dataset.ann_folder,
            output_dir=os.path.join(output_dir, "panoptic_eval"),
        )

    for samples, targets  in metric_logger.log_every(data_loader, 10, header):
        samples = samples.to(device)
        import pdb
        pdb.set_trace()
        targets = [{k: v.to(device) for k, v in t.items()} for t in targets[0]]

        outputs = model(samples)
        loss_dict = criterion(outputs, targets)
        weight_dict = criterion.weight_dict

        # reduce losses over all GPUs for logging purposes
        loss_dict_reduced = utils.reduce_dict(loss_dict)
        loss_dict_reduced_scaled = {k: v * weight_dict[k]
                                    for k, v in loss_dict_reduced.items() if k in weight_dict}
        loss_dict_reduced_unscaled = {f'{k}_unscaled': v
                                      for k, v in loss_dict_reduced.items()}
        metric_logger.update(loss=sum(loss_dict_reduced_scaled.values()),
                             **loss_dict_reduced_scaled,
                             **loss_dict_reduced_unscaled)
        metric_logger.update(class_error=loss_dict_reduced['class_error'])

        orig_target_sizes = torch.stack([t["orig_size"] for t in targets], dim=0)
        results = postprocessors['bbox'](outputs, orig_target_sizes)
        if 'segm' in postprocessors.keys():
            target_sizes = torch.stack([t["size"] for t in targets], dim=0)
            results = postprocessors['segm'](results, outputs, orig_target_sizes, target_sizes)
        res = {target['image_id'].item(): output for target, output in zip(targets, results)}
        if coco_evaluator is not None:
            coco_evaluator.update(res)

        if panoptic_evaluator is not None:
            res_pano = postprocessors["panoptic"](outputs, target_sizes, orig_target_sizes)
            for i, target in enumerate(targets):
                image_id = target["image_id"].item()
                file_name = f"{image_id:012d}.png"
                res_pano[i]["image_id"] = image_id
                res_pano[i]["file_name"] = file_name

            panoptic_evaluator.update(res_pano)

    # gather the stats from all processes
    metric_logger.synchronize_between_processes()
    print("Averaged stats:", metric_logger)
    if coco_evaluator is not None:
        coco_evaluator.synchronize_between_processes()
    if panoptic_evaluator is not None:
        panoptic_evaluator.synchronize_between_processes()

    # accumulate predictions from all images
    if coco_evaluator is not None:
        coco_evaluator.accumulate()
        coco_evaluator.summarize()
    panoptic_res = None
    if panoptic_evaluator is not None:
        panoptic_res = panoptic_evaluator.summarize()
    stats = {k: meter.global_avg for k, meter in metric_logger.meters.items()}
    if coco_evaluator is not None:
        if 'bbox' in postprocessors.keys():
            stats['coco_eval_bbox'] = coco_evaluator.coco_eval['bbox'].stats.tolist()
        if 'segm' in postprocessors.keys():
            stats['coco_eval_masks'] = coco_evaluator.coco_eval['segm'].stats.tolist()
    if panoptic_res is not None:
        stats['PQ_all'] = panoptic_res["All"]
        stats['PQ_th'] = panoptic_res["Things"]
        stats['PQ_st'] = panoptic_res["Stuff"]
    return stats, coco_evaluator
