"""YOLO_v3 Model Defined in Keras."""

from functools import wraps
from yolo3.enum import BOX_LOSS
import numpy as np
import tensorflow as tf
from typing import List, Tuple
from yolo3.utils import compose
from yolo3.override import mobilenet_v2

@wraps(tf.keras.layers.Conv2D)
def DarknetConv2D(*args, **kwargs):
    """Wrapper to set Darknet parameters for Convolution2D."""
    darknet_conv_kwargs = {'kernel_regularizer': tf.keras.regularizers.l2(5e-4)}
    darknet_conv_kwargs['padding'] = 'valid' if kwargs.get('strides') == (2, 2) else 'same'
    darknet_conv_kwargs.update(kwargs)
    return tf.keras.layers.Conv2D(*args, **darknet_conv_kwargs)


def DarknetConv2D_BN_Leaky(*args, **kwargs):
    """Darknet Convolution2D followed by BatchNormalization and LeakyReLU."""
    no_bias_kwargs = {'use_bias': False}
    no_bias_kwargs.update(kwargs)
    return compose(
        DarknetConv2D(*args, **no_bias_kwargs),
        tf.keras.layers.BatchNormalization(),
        tf.keras.layers.LeakyReLU(alpha=0.1))


def resblock_body(x, num_filters, num_blocks):
    '''A series of resblocks starting with a downsampling Convolution2D'''
    # Darknet uses left and top padding instead of 'same' mode
    x = tf.keras.layers.ZeroPadding2D(((1, 0), (1, 0)))(x)
    x = DarknetConv2D_BN_Leaky(num_filters, (3, 3), strides=(2, 2))(x)
    for i in range(num_blocks):
        y = compose(
            DarknetConv2D_BN_Leaky(num_filters // 2, (1, 1)),
            DarknetConv2D_BN_Leaky(num_filters, (3, 3)))(x)
        x = tf.keras.layers.Add()([x, y])
    return x


def darknet_body(x):
    '''Darknent body having 52 Convolution2D layers'''
    x = DarknetConv2D_BN_Leaky(32, (3, 3))(x)
    x = resblock_body(x, 64, 1)
    x = resblock_body(x, 128, 2)
    x = resblock_body(x, 256, 8)
    x = resblock_body(x, 512, 8)
    x = resblock_body(x, 1024, 4)
    return x


def make_last_layers(x, num_filters, out_filters):
    '''6 Conv2D_BN_Leaky layers followed by a Conv2D_linear layer'''
    x = compose(
        DarknetConv2D_BN_Leaky(num_filters, (1, 1)),
        DarknetConv2D_BN_Leaky(num_filters * 2, (3, 3)),
        DarknetConv2D_BN_Leaky(num_filters, (1, 1)),
        DarknetConv2D_BN_Leaky(num_filters * 2, (3, 3)),
        DarknetConv2D_BN_Leaky(num_filters, (1, 1)))(x)
    y = compose(
        DarknetConv2D_BN_Leaky(num_filters * 2, (3, 3)),
        DarknetConv2D(out_filters, (1, 1)))(x)
    return x, y


def darknet_yolo_body(inputs, num_anchors, num_classes):
    """Create YOLO_V3 model CNN body in Keras."""
    darknet = tf.keras.models.Model(inputs, darknet_body(inputs))
    x, y1 = make_last_layers(darknet.output, 512, num_anchors * (num_classes + 5))

    x = compose(
        DarknetConv2D_BN_Leaky(256, (1, 1)),
        tf.keras.layers.UpSampling2D(2))(x)
    x = tf.keras.layers.Concatenate()([x, darknet.layers[152].output])
    x, y2 = make_last_layers(x, 256, num_anchors * (num_classes + 5))

    x = compose(
        DarknetConv2D_BN_Leaky(128, (1, 1)),
        tf.keras.layers.UpSampling2D(2))(x)
    x = tf.keras.layers.Concatenate()([x, darknet.layers[92].output])
    x, y3 = make_last_layers(x, 128, num_anchors * (num_classes + 5))
    y1 = tf.keras.layers.Lambda(lambda feats: tf.reshape(
        feats, (-1, tf.shape(y1)[1], tf.shape(y1)[2], num_anchors, num_classes + 5)), name='y1')(y1)
    y2 = tf.keras.layers.Lambda(lambda feats: tf.reshape(
        feats, (-1, tf.shape(y2)[1], tf.shape(y2)[2], num_anchors, num_classes + 5)), name='y2')(y2)
    y3 = tf.keras.layers.Lambda(lambda feats: tf.reshape(
        feats, (-1, tf.shape(y3)[1], tf.shape(y3)[2], num_anchors, num_classes + 5)), name='y3')(y3)
    return tf.keras.models.Model(inputs, [y1, y2, y3])


def _make_divisible(v, divisor, min_value=None):
    if min_value is None:
        min_value = divisor
    new_v = max(min_value, int(v + divisor / 2) // divisor * divisor)
    # Make sure that round down does not go down by more than 10%.
    if new_v < 0.9 * v:
        new_v += divisor
    return new_v

def SeparableConv2D(
               filters,
               kernel_size,
               strides=(1, 1),
               padding='valid',
               use_bias=True):
    return compose(
        tf.keras.layers.DepthwiseConv2D(
            kernel_size,
            padding=padding,
            use_bias=use_bias,
            strides=strides),
        tf.keras.layers.BatchNormalization(momentum=0.9),
        tf.keras.layers.ReLU(6.),
        tf.keras.layers.Conv2D(filters, 1,
          padding='same',
          use_bias=use_bias,
          strides=1),
        tf.keras.layers.BatchNormalization(momentum=0.9),
        tf.keras.layers.ReLU(6.))

def make_last_layers_mobilenet(x,id, num_filters, out_filters):
    x=compose(
        tf.keras.layers.Conv2D(num_filters,
                               kernel_size=1,
                               padding='same',
                               use_bias=False,
                               name='block_'+str(id) + '_conv'),
        tf.keras.layers.BatchNormalization(momentum=0.9,name='block_'+str(id) + '_BN'),
        tf.keras.layers.ReLU(6., name='block_'+str(id) + '_relu6'),
        SeparableConv2D(2 * num_filters, kernel_size=(3, 3), use_bias=False,padding='same'),
        tf.keras.layers.Conv2D(num_filters,
                               kernel_size=1,
                               padding='same',
                               use_bias=False,
                               name='block_'+str(id+1) + '_conv'),
        tf.keras.layers.BatchNormalization(momentum=0.9,name='block_'+str(id+1) + '_BN'),
        tf.keras.layers.ReLU(6., name='block_'+str(id+1) + '_relu6'),
        SeparableConv2D(2 * num_filters, kernel_size=(3, 3), use_bias=False,padding='same'),
        tf.keras.layers.Conv2D(num_filters,
                               kernel_size=1,
                               padding='same',
                               use_bias=False,
                               name='block_'+str(id+2) + '_conv'),
        tf.keras.layers.BatchNormalization(momentum=0.9,name='block_'+str(id+2) + '_BN'),
        tf.keras.layers.ReLU(6., name='block_'+str(id+2) + '_relu6'))(x)
    y=compose(SeparableConv2D(2 * num_filters, kernel_size=(3, 3), use_bias=False,padding='same'),
              tf.keras.layers.Conv2D(out_filters,
                                     kernel_size=1,
                                     padding='same',
                                     use_bias=False))(x)
    return x,y

def MobilenetConv2D_BN_Relu(kernel,alpha, filters):
    last_block_filters = _make_divisible(filters * alpha, 8)
    return compose(tf.keras.layers.Conv2D(last_block_filters,
                                          kernel,
                                          padding='same',
                                          use_bias=False),
                   tf.keras.layers.BatchNormalization(momentum=0.9),
                   tf.keras.layers.ReLU(6.))

def mobilenetv2_yolo_body(inputs, num_anchors, num_classes, alpha=1.0):
    mobilenetv2 = mobilenet_v2(default_batchnorm_momentum=0.9,alpha=alpha,input_tensor=inputs, include_top=False,
                                                    weights='imagenet')
    x,y1=make_last_layers_mobilenet(mobilenetv2.output,17,512,num_anchors * (num_classes + 5))
    x = compose(
        tf.keras.layers.Conv2D(256,
                               kernel_size=1,
                               padding='same',
                               use_bias=False,
                               name='block_20_conv'),
        tf.keras.layers.BatchNormalization(momentum=0.9,name='block_20_BN'),
        tf.keras.layers.ReLU(6., name='block_20_relu6'),
        tf.keras.layers.UpSampling2D(2))(x)
    x = tf.keras.layers.Concatenate()(
        [x, MobilenetConv2D_BN_Relu((1,1),alpha, 384)(mobilenetv2.get_layer('block_12_project_BN').output)])
    x, y2 = make_last_layers_mobilenet(x, 21, 256, num_anchors * (num_classes + 5))
    x = compose(
        tf.keras.layers.Conv2D(128,
                               kernel_size=1,
                               padding='same',
                               use_bias=False,
                               name='block_24_conv'),
        tf.keras.layers.BatchNormalization(momentum=0.9,name='block_24_BN'),
        tf.keras.layers.ReLU(6., name='block_24_relu6'),
        tf.keras.layers.UpSampling2D(2))(x)
    x = tf.keras.layers.Concatenate()(
        [x, MobilenetConv2D_BN_Relu((1,1),alpha, 128)(mobilenetv2.get_layer('block_5_project_BN').output)])
    x, y3 = make_last_layers_mobilenet(x, 25,128, num_anchors * (num_classes + 5))

    y1 = tf.keras.layers.Lambda(lambda feats: tf.reshape(
        feats, (-1, tf.shape(y1)[1], tf.shape(y1)[2], num_anchors, num_classes + 5)),name='y1')(y1)
    y2 = tf.keras.layers.Lambda(lambda feats: tf.reshape(
        feats, (-1, tf.shape(y2)[1], tf.shape(y2)[2], num_anchors, num_classes + 5)),name='y2')(y2)
    y3 = tf.keras.layers.Lambda(lambda feats: tf.reshape(
        feats, (-1, tf.shape(y3)[1], tf.shape(y3)[2], num_anchors, num_classes + 5)),name='y3')(y3)
    return tf.keras.models.Model(inputs, [y1,y2,y3])

def inception_block(filters, kernel):
    return compose(
        tf.keras.layers.Conv2D(filters, kernel, use_bias=False, kernel_regularizer=tf.keras.regularizers.l2(5e-4)),
        tf.keras.layers.BatchNormalization(),
        tf.keras.layers.LeakyReLU(alpha=0.1))


def inception_yolo_body(inputs, num_anchors, num_classes):
    inception = tf.keras.applications.InceptionResNetV2(input_tensor=inputs, include_top=False, weights='imagenet')
    x, y1 = make_last_layers(inception.output, 512, num_anchors * (num_classes + 5))
    x = compose(
        DarknetConv2D_BN_Leaky(256, (1, 1)),
        tf.keras.layers.UpSampling2D(2))(x)
    x = tf.keras.layers.Concatenate()([x, inception_block(256, (3, 3))(inception.get_layer('activation_160').output)])
    x, y2 = make_last_layers(x, 256, num_anchors * (num_classes + 5))

    x = compose(
        DarknetConv2D_BN_Leaky(128, (1, 1)),
        tf.keras.layers.UpSampling2D(2))(x)
    x = tf.keras.layers.Concatenate()([x, inception_block(128, (6, 6))(inception.get_layer('activation_73').output)])
    x, y3 = make_last_layers(x, 128, num_anchors * (num_classes + 5))

    return tf.keras.models.Model(inputs, [y1, y2, y3])


def densenet_yolo_body(inputs, num_anchors, num_classes):
    densenet = tf.keras.applications.DenseNet201(input_tensor=inputs, include_top=False, weights='imagenet')
    x, y1 = make_last_layers(densenet.output, 512, num_anchors * (num_classes + 5))
    x = compose(
        DarknetConv2D_BN_Leaky(256, (1, 1)),
        tf.keras.layers.UpSampling2D(2))(x)
    x = tf.keras.layers.Concatenate()([x, densenet.get_layer('pool4_relu').output])
    x, y2 = make_last_layers(x, 256, num_anchors * (num_classes + 5))

    x = compose(
        DarknetConv2D_BN_Leaky(128, (1, 1)),
        tf.keras.layers.UpSampling2D(2))(x)
    x = tf.keras.layers.Concatenate()([x, densenet.get_layer('pool3_relu').output])
    x, y3 = make_last_layers(x, 128, num_anchors * (num_classes + 5))

    return tf.keras.models.Model(inputs, [y1, y2, y3])


def yolo_head(feats: tf.Tensor, anchors: np.ndarray,input_shape: tf.Tensor,
              calc_loss: bool = False) -> Tuple[tf.Tensor, tf.Tensor, tf.Tensor, tf.Tensor]:
    """Convert final layer features to bounding box parameters."""
    num_anchors = len(anchors)
    # Reshape to batch, height, width, num_anchors, box_params.
    anchors_tensor = tf.reshape(tf.constant(anchors), [1, 1, 1, num_anchors, 2])
    grid_shape=tf.shape(feats)[1:3]
    grid_y = tf.tile(tf.reshape(tf.range(0, grid_shape[0]), [-1, 1, 1, 1]),
                     [1, grid_shape[1], 1, 1])
    grid_x = tf.tile(tf.reshape(tf.range(0, grid_shape[1]), [1, -1, 1, 1]),
                     [grid_shape[0], 1, 1, 1])
    grid = tf.concat([grid_x, grid_y], -1)
    grid = tf.cast(grid, feats.dtype)

    # Adjust preditions to each spatial grid point and anchor size.
    box_xy = (tf.sigmoid(feats[..., :2]) + grid) / tf.cast(grid_shape[::-1], feats.dtype)
    box_wh = tf.exp(feats[..., 2:4]) * tf.cast(anchors_tensor, feats.dtype) / tf.cast(input_shape[::-1], feats.dtype)
    box_confidence = tf.sigmoid(feats[..., 4:5])
    if calc_loss == True:
        return grid, box_xy, box_wh,box_confidence
    box_class_probs = tf.sigmoid(feats[..., 5:])
    return box_xy, box_wh, box_confidence, box_class_probs


def yolo_correct_boxes(box_xy: tf.Tensor, box_wh: tf.Tensor, input_shape: tf.Tensor, image_shape) -> tf.Tensor:
    '''Get corrected boxes'''
    box_yx = box_xy[..., ::-1]
    box_hw = box_wh[..., ::-1]
    input_shape = tf.cast(input_shape, box_yx.dtype)
    image_shape = tf.cast(image_shape, box_yx.dtype)
    max_shape=tf.maximum(image_shape[0],image_shape[1])
    ratio=image_shape/max_shape
    boxed_shape=input_shape*ratio
    offset = (input_shape - boxed_shape) / 2.
    scale = image_shape/boxed_shape
    box_yx = (box_yx*input_shape - offset) * scale
    box_hw *= input_shape*scale

    box_mins = box_yx - (box_hw / 2.)
    box_maxes = box_yx + (box_hw / 2.)
    boxes = tf.concat([
        tf.clip_by_value(box_mins[..., 0:1],0,image_shape[0]),  # y_min
        tf.clip_by_value(box_mins[..., 1:2],0,image_shape[1]),  # x_min
        tf.clip_by_value(box_maxes[..., 0:1],0,image_shape[0]),  # y_max
        tf.clip_by_value(box_maxes[..., 1:2],0,image_shape[1]) # x_max
    ], -1)
    return boxes


def yolo_boxes_and_scores(feats: tf.Tensor, anchors: List[Tuple[float, float]], num_classes: int,
                          input_shape: Tuple[int, int], image_shape) -> Tuple[tf.Tensor, tf.Tensor]:
    '''Process Conv layer output'''
    box_xy, box_wh, box_confidence, box_class_probs = yolo_head(feats,
                                                                anchors, input_shape)
    boxes = yolo_correct_boxes(box_xy, box_wh, input_shape, image_shape)
    boxes = tf.reshape(boxes, [-1, 4])
    box_scores = box_confidence * box_class_probs
    box_scores = tf.reshape(box_scores, [-1, num_classes])
    return boxes, box_scores


def yolo_eval(yolo_outputs: List[tf.Tensor],
              anchors: np.ndarray,
              num_classes: int,
              image_shape,
              max_boxes: int = 20,
              score_threshold: float = .6,
              iou_threshold: float = .5) -> Tuple[List[tf.Tensor], List[tf.Tensor], List[tf.Tensor]]:
    """Evaluate YOLO model on given input and return filtered boxes."""

    num_layers = len(yolo_outputs)
    anchor_mask = [[6, 7, 8], [3, 4, 5], [0, 1, 2]]

    input_shape = tf.shape(yolo_outputs[0])[1:3] * 32
    boxes = []
    box_scores = []
    for l in range(num_layers):
        _boxes, _box_scores = yolo_boxes_and_scores(yolo_outputs[l],
                                                    anchors[anchor_mask[l]], num_classes, input_shape, image_shape)
        boxes.append(_boxes)
        box_scores.append(_box_scores)
    boxes = tf.concat(boxes, axis=0)
    box_scores = tf.concat(box_scores, axis=0)

    mask = box_scores >= score_threshold
    max_boxes_tensor = tf.constant(max_boxes, dtype=tf.int32)
    boxes_ = []
    scores_ = []
    classes_ = []
    for c in range(num_classes):
        # TODO: use keras backend instead of tf.
        class_boxes = tf.boolean_mask(boxes, mask[:, c])
        class_box_scores = tf.boolean_mask(box_scores[:, c], mask[:, c])
        nms_index = tf.image.non_max_suppression(
            class_boxes, class_box_scores, max_boxes_tensor, iou_threshold=iou_threshold)
        class_boxes = tf.gather(class_boxes, nms_index)
        class_box_scores = tf.gather(class_box_scores, nms_index)
        classes = tf.ones_like(class_box_scores, tf.int32) * c
        boxes_.append(class_boxes)
        scores_.append(class_box_scores)
        classes_.append(classes)
    boxes_ = tf.concat(boxes_, axis=0,name='boxes')
    scores_ = tf.concat(scores_, axis=0,name='scores')
    classes_ = tf.concat(classes_, axis=0,name='classes')
    boxes_=tf.cast(boxes_,tf.int32)
    return boxes_, scores_, classes_


def preprocess_true_boxes(true_boxes, input_shape, anchors, num_classes):
    '''Preprocess true boxes to training input format

    Parameters
    ----------
    true_boxes: array, shape=(m, T, 5)
        Absolute x_min, y_min, x_max, y_max, class_id relative to input_shape.
    input_shape: array-like, hw, multiples of 32
    anchors: array, shape=(N, 2), wh
    num_classes: integer

    Returns
    -------
    y_true: list of array, shape like yolo_outputs, xywh are reletive value

    '''
    num_layers = len(anchors) // 3  # default setting
    anchor_mask = [[6, 7, 8], [3, 4, 5], [0, 1, 2]]
    true_boxes = np.array(true_boxes, dtype='float32')
    input_shape = np.array(input_shape, dtype='int32')
    boxes_xy = (true_boxes[..., 0:2] + true_boxes[..., 2:4]) // 2
    boxes_wh = true_boxes[..., 2:4] - true_boxes[..., 0:2]
    true_boxes[..., 0:2] = boxes_xy / input_shape[::-1]
    true_boxes[..., 2:4] = boxes_wh / input_shape[::-1]

    grid_shapes = [input_shape // [32, 16, 8][l] for l in range(num_layers)]
    y_true = [np.zeros((grid_shapes[l][0], grid_shapes[l][1], len(anchor_mask[l]), 5 + num_classes),
                       dtype='float32') for l in range(num_layers)]

    # Expand dim to apply broadcasting.
    anchors = np.expand_dims(anchors, 0)
    anchor_maxes = anchors / 2.
    anchor_mins = -anchor_maxes
    valid_mask = boxes_wh[..., 0] > 0
    wh = boxes_wh[valid_mask]
    # Expand dim to apply broadcasting.
    wh = np.expand_dims(wh, -2)
    box_maxes = wh / 2.
    box_mins = -box_maxes

    intersect_mins = np.maximum(box_mins, anchor_mins)
    intersect_maxes = np.minimum(box_maxes, anchor_maxes)
    intersect_wh = np.maximum(intersect_maxes - intersect_mins, 0.)
    intersect_area = intersect_wh[..., 0] * intersect_wh[..., 1]
    box_area = wh[..., 0] * wh[..., 1]
    anchor_area = anchors[..., 0] * anchors[..., 1]
    iou = intersect_area / (box_area + anchor_area - intersect_area)

    # Find best anchor for each true box
    best_anchor = np.argmax(iou, axis=-1)

    for t, n in enumerate(best_anchor):
        for l in range(num_layers):
            if n in anchor_mask[l]:
                i = np.floor(true_boxes[t, 0] * grid_shapes[l][1]).astype('int32')
                j = np.floor(true_boxes[t, 1] * grid_shapes[l][0]).astype('int32')
                k = anchor_mask[l].index(n)
                c = true_boxes[t, 4].astype('int32')
                y_true[l][j, i, k, 0:4] = true_boxes[t, 0:4]
                y_true[l][j, i, k, 4] = 1
                y_true[l][j, i, k, 5 + c] = 1

    return y_true[0], y_true[1], y_true[2]


def box_iou(b1, b2):
    '''Return iou tensor

    Parameters
    ----------
    b1: tensor, shape=(i1,...,iN, 4), xywh
    b2: tensor, shape=(j, 4), xywh

    Returns
    -------
    iou: tensor, shape=(i1,...,iN, j)

    '''

    # Expand dim to apply broadcasting.
    b1_xy = b1[..., :2]
    b1_wh = b1[..., 2:4]
    b1_wh_half = b1_wh / 2.
    b1_mins = b1_xy - b1_wh_half
    b1_maxes = b1_xy + b1_wh_half

    # Expand dim to apply broadcasting.
    b2_xy = b2[..., :2]
    b2_wh = b2[..., 2:4]
    b2_wh_half = b2_wh / 2.
    b2_mins = b2_xy - b2_wh_half
    b2_maxes = b2_xy + b2_wh_half

    intersect_mins = tf.maximum(b1_mins, b2_mins)
    intersect_maxes = tf.minimum(b1_maxes, b2_maxes)
    intersect_wh = tf.maximum(intersect_maxes - intersect_mins, 0.)
    intersect_area = intersect_wh[..., 0] * intersect_wh[..., 1]
    b1_area = b1_wh[..., 0] * b1_wh[..., 1]
    b2_area = b2_wh[..., 0] * b2_wh[..., 1]
    iou = intersect_area / (b1_area + b2_area - intersect_area)

    return iou

def box_giou(b1, b2):
    # Expand dim to apply broadcasting.
    b1_xy = b1[...,:2]
    b1_wh = b1[...,2:4]
    b1_wh_half = b1_wh / 2.
    b1_mins = b1_xy - b1_wh_half
    b1_maxes = b1_xy + b1_wh_half

    # Expand dim to apply broadcasting.
    b2_xy = b2[...,:2]
    b2_wh = b2[...,2:4]
    b2_wh_half = b2_wh / 2.
    b2_mins = b2_xy - b2_wh_half
    b2_maxes = b2_xy + b2_wh_half

    intersect_mins = tf.maximum(b1_mins, b2_mins)
    intersect_maxes = tf.minimum(b1_maxes, b2_maxes)
    intersect_wh = tf.maximum(intersect_maxes - intersect_mins, 0.)
    intersect_area = intersect_wh[..., 0] * intersect_wh[..., 1]
    b1_area = b1_wh[..., 0] * b1_wh[..., 1]
    b2_area = b2_wh[..., 0] * b2_wh[..., 1]
    union_area=b1_area + b2_area - intersect_area
    iou = intersect_area / union_area

    bc_mins=tf.minimum(b1_mins,b2_mins)
    bc_maxes=tf.maximum(b1_maxes,b2_maxes)
    enclose_wh=tf.maximum(bc_maxes-bc_mins,0.)
    enclose_area=enclose_wh[...,0]*enclose_wh[...,1]
    giou=iou-(enclose_area-union_area)/enclose_area
    return giou

def focal_loss(target,actual,alpha=0.25,gamma=2):
    return alpha*tf.pow(tf.abs(target-actual),gamma)

def yolo_loss(yolo_output,y_true,idx, anchors, ignore_thresh: float = .5, box_loss=BOX_LOSS.GIOU,print_loss: bool = False):
    '''Return yolo_loss tensor

    Parameters
    ----------
    yolo_output: the output of yolo_body or tiny_yolo_body
    y_true: the output of preprocess_true_boxes
    anchors: array, shape=(N, 2), wh
    num_classes: integer
    ignore_thresh: float, the iou threshold whether to ignore object confidence loss

    Returns
    -------
    loss: tensor, shape=(1,)

    '''
    grid_steps=[32,16,8]
    grid_step=grid_steps[idx]
    anchor_mask = [[6, 7, 8], [3, 4, 5], [0, 1, 2]]
    loss = 0
    m = tf.shape(yolo_output)[0]  # batch size, tensor
    mf = tf.cast(m, yolo_output.dtype)
    object_mask = y_true[..., 4:5]
    true_class_probs = y_true[..., 5:]
    input_shape=tf.shape(yolo_output)[1:3]*grid_step
    grid, pred_xy, pred_wh,box_confidence = yolo_head(yolo_output,anchors[anchor_mask[idx]], input_shape, calc_loss=True)
    pred_box = tf.concat([pred_xy, pred_wh], -1)
    # Find ignore mask, iterate over each of batch.
    ignore_mask = tf.TensorArray(y_true.dtype, size=1, dynamic_size=True)
    object_mask_bool = tf.cast(object_mask, 'bool')
    def loop_body(b, ignore_mask):
        true_box = tf.boolean_mask(y_true[b, ..., 0:4], object_mask_bool[b, ..., 0])
        iou = box_iou(tf.expand_dims(pred_box[b],-2), tf.expand_dims(true_box,0))
        best_iou = tf.reduce_max(iou, axis=-1)
        ignore_mask = ignore_mask.write(b, tf.cast(best_iou < ignore_thresh, true_box.dtype))
        return b + 1, ignore_mask

    _, ignore_mask = tf.while_loop(lambda b, *args: b < m, loop_body, [0, ignore_mask])
    ignore_mask = ignore_mask.stack()
    ignore_mask = tf.expand_dims(ignore_mask, -1)
    confidence_loss = focal_loss(object_mask,box_confidence)*(object_mask * tf.nn.sigmoid_cross_entropy_with_logits(labels=object_mask,
                                                                            logits=yolo_output[..., 4:5]) + \
                      (1 - object_mask) * tf.nn.sigmoid_cross_entropy_with_logits(labels=object_mask,
                                                                                  logits=yolo_output[...,4:5]) * ignore_mask)
    class_loss = object_mask * tf.nn.sigmoid_cross_entropy_with_logits(labels=true_class_probs,
                                                                       logits=yolo_output[..., 5:])
    confidence_loss = tf.reduce_sum(confidence_loss) / mf
    class_loss = tf.reduce_sum(class_loss) / mf

    if box_loss==BOX_LOSS.GIOU:
        giou=box_giou(pred_box[...,:4],y_true[...,:4])
        box_loss_scale = 2 - y_true[..., 2:3] * y_true[..., 3:4]/tf.cast(tf.reduce_prod(input_shape),tf.float32)
        giou_loss=object_mask*box_loss_scale*(1-tf.expand_dims(giou,-1))
        giou_loss = tf.reduce_sum(giou_loss) / mf
        loss+=giou_loss+confidence_loss+class_loss
        if print_loss:
            tf.print(giou_loss,confidence_loss,class_loss)
    elif box_loss==BOX_LOSS.MSE:
        grid_shape = tf.cast(tf.shape(yolo_output)[1:3], y_true.dtype)
        raw_true_xy = y_true[..., :2] * grid_shape[::-1] - grid
        raw_true_wh = tf.math.log(y_true[..., 2:4] / anchors[anchor_mask[idx]] * input_shape[::-1])
        raw_true_wh = tf.keras.backend.switch(object_mask, raw_true_wh, tf.zeros_like(raw_true_wh))
        box_loss_scale = 2 - y_true[..., 2:3] * y_true[..., 3:4]
        xy_loss = object_mask * box_loss_scale * tf.nn.sigmoid_cross_entropy_with_logits(labels=raw_true_xy,
                                                                                       logits=yolo_output[..., 0:2])
        wh_loss = object_mask * box_loss_scale * 0.5 * tf.square(raw_true_wh - yolo_output[..., 2:4])
        xy_loss = tf.reduce_sum(xy_loss) / mf
        wh_loss = tf.reduce_sum(wh_loss) / mf
        loss += xy_loss + wh_loss + confidence_loss + class_loss
        if print_loss:
            tf.print(loss, xy_loss, wh_loss, confidence_loss, class_loss, tf.reduce_sum(ignore_mask))
    return loss

