import _init_paths

import argparse
import os
import sys
import logging
import pprint
import cv2
from config.config import config, update_config
from utils.image import resize, transform
import numpy as np
import glob
import json
# get config
os.environ['PYTHONUNBUFFERED'] = '1'
os.environ['MXNET_CUDNN_AUTOTUNE_DEFAULT'] = '0'
os.environ['MXNET_ENABLE_GPU_P2P'] = '0'
update_config('./road_train_all.yaml')

sys.path.insert(0, os.path.join('../external/mxnet', config.MXNET_VERSION))
import mxnet as mx
from core.tester import im_detect, Predictor
from symbols import *
from utils.load_model import load_param
from utils.show_boxes import show_boxes
from utils.tictoc import tic, toc
from nms.nms import py_nms_wrapper, cpu_nms_wrapper, gpu_nms_wrapper


def main():
    # get symbol
    pprint.pprint(config)
    config.symbol = 'resnet_v1_101_rfcn'
    sym_instance = eval(config.symbol + '.' + config.symbol)()
    sym = sym_instance.get_symbol(config, is_train=False)

    # set up class names; Don't count the background in, even we are treat the background as label '0'
    num_classes = 4
    classes = ['vehicle', 'pedestrian', 'cyclist', 'traffic lights']

    # load demo data
    image_path = './data/RoadImages/test/'
    image_names = glob.glob(image_path +  '*.jpg')
    

    print("Image amount {}".format(len(image_names)))
    data = []
    for im_name in image_names:
        assert os.path.exists(im_name), ('%s does not exist'.format(im_name))
        im = cv2.imread(im_name, cv2.IMREAD_COLOR | cv2.IMREAD_IGNORE_ORIENTATION)
        target_size = config.SCALES[0][1]
        max_size = config.SCALES[0][1]
        im, im_scale = resize(im, target_size, max_size, stride=config.network.IMAGE_STRIDE)
        im_tensor = transform(im, config.network.PIXEL_MEANS)
        im_info = np.array([[im_tensor.shape[2], im_tensor.shape[3], im_scale]], dtype=np.float32)
        data.append({'data': im_tensor, 'im_info': im_info})


    # get predictor
    data_names = ['data', 'im_info']
    label_names = []
    data = [[mx.nd.array(data[i][name]) for name in data_names] for i in xrange(len(data))]
    max_data_shape = [[('data', (1, 3, max([v[0] for v in config.SCALES]), max([v[1] for v in config.SCALES])))]]
    provide_data = [[(k, v.shape) for k, v in zip(data_names, data[i])] for i in xrange(len(data))]
    provide_label = [None for i in xrange(len(data))]
    arg_params, aux_params = load_param('./output/rfcn/road_obj/road_train_all/all/' +  'rfcn_road', 19 , process=True)
    predictor = Predictor(sym, data_names, label_names,
                          context=[mx.gpu(0)], max_data_shapes=max_data_shape,
                          provide_data=provide_data, provide_label=provide_label,
                          arg_params=arg_params, aux_params=aux_params)
    nms = gpu_nms_wrapper(config.TEST.NMS, 0)

    # test
    notation_dict = {}
    for idx, im_name in enumerate(image_names):
        data_batch = mx.io.DataBatch(data=[data[idx]], label=[], pad=0, index=idx,
                                     provide_data=[[(k, v.shape) for k, v in zip(data_names, data[idx])]],
                                     provide_label=[None])
        scales = [data_batch.data[i][1].asnumpy()[0, 2] for i in xrange(len(data_batch.data))]

        tic()
        scores, boxes, data_dict = im_detect(predictor, data_batch, data_names, scales, config)
        boxes = boxes[0].astype('f')
        scores = scores[0].astype('f')
        dets_nms = []
        for j in range(1, scores.shape[1]):
            cls_scores = scores[:, j, np.newaxis]
            cls_boxes = boxes[:, 4:8] if config.CLASS_AGNOSTIC else boxes[:, j * 4:(j + 1) * 4]
            cls_dets = np.hstack((cls_boxes, cls_scores))
            keep = nms(cls_dets)
            cls_dets = cls_dets[keep, :]
            cls_dets = cls_dets[cls_dets[:, -1] > 0.7, :]
            dets_nms.append(cls_dets)
        print 'testing {} {:.4f}s'.format(im_name, toc())
        # notation_list.append(get_notation(im_name, dets_nms, classes, scale=1.0, gen_bbox_pic=True))
        notation_dict.update(get_notation(im_name, dets_nms, classes, scale=1.0, gen_bbox_pic=True))
    save_notation_file(notation_dict)
    print 'done'

def get_notation(image_name, dets_nms, classes, scale=1.0, gen_bbox_pic=False):
    file_name = image_name.split('/')[-1]
    im = load_image(image_name)
    label_list = np.array([])
    for cls_idx, cls_name in enumerate(classes):
        cls_dets = dets_nms[cls_idx]
        for det in cls_dets:
            bbox = det[:4] * scale
            if cls_dets.shape[1] == 5:
                score = det[-1]
                if len(label_list) == 0:
                    label_list = np.copy(np.hstack((bbox, [ 20 if cls_idx == 3 else cls_idx ],[score])))
                else:
                    label_list = np.vstack((label_list, (np.hstack((bbox, [ 20 if cls_idx == 3 else cls_idx ],[score])))))
                # print(label_list)
                if gen_bbox_pic:
                    draw_bbox_on_picture(im, bbox)
    label_list = label_list.reshape(-1, 6).tolist()
    notation = {file_name: label_list}
    save_bbox_pic(im, file_name)
    return notation
            

def draw_bbox_on_picture(image, bbox):
    cv2.rectangle(image,(int(bbox[0]), int(bbox[1])),(int(bbox[2]), int(bbox[3])),(0,255,0),2)
    return image
    
def load_image(image_name):
    im = cv2.imread(image_name)
    im = cv2.cvtColor(im, cv2.COLOR_BGR2RGB)
    return im

def save_bbox_pic(image, file_name, save_path='./bbox_pic'):
    if not os.path.exists(save_path):
        os.mkdir(save_path)
    cv2.imwrite(os.path.join(save_path, file_name), image)
    print("Image '{}' has been saved!".format(os.path.join(save_path, file_name)))
    
def save_notation_file(notation_dict, file_path='./results.json'):
    with open(os.path.join(file_path), 'w') as n:
        n.write(json.dumps(notation_dict))
        print("New Label Notation has been saved!")

if __name__ == '__main__':
    main()
