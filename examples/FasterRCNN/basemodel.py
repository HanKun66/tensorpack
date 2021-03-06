# -*- coding: utf-8 -*-
# File: basemodel.py

from contextlib import contextmanager
import tensorflow as tf
from tensorpack.tfutils import argscope
from tensorpack.tfutils.scope_utils import auto_reuse_variable_scope
from tensorpack.tfutils.varreplace import custom_getter_scope
from tensorpack.models import (
    Conv2D, MaxPooling, BatchNorm, layer_register)

from config import config as cfg


@layer_register(log_shape=True)
def GroupNorm(x, group=32, gamma_initializer=tf.constant_initializer(1.)):
    shape = x.get_shape().as_list()
    ndims = len(shape)
    assert ndims == 4, shape
    chan = shape[1]
    assert chan % group == 0, chan
    group_size = chan // group

    orig_shape = tf.shape(x)
    h, w = orig_shape[2], orig_shape[3]

    x = tf.reshape(x, tf.stack([-1, group, group_size, h, w]))

    mean, var = tf.nn.moments(x, [2, 3, 4], keep_dims=True)

    new_shape = [1, group, group_size, 1, 1]

    beta = tf.get_variable('beta', [chan], initializer=tf.constant_initializer())
    beta = tf.reshape(beta, new_shape)

    gamma = tf.get_variable('gamma', [chan], initializer=gamma_initializer)
    gamma = tf.reshape(gamma, new_shape)

    out = tf.nn.batch_normalization(x, mean, var, beta, gamma, 1e-5, name='output')
    return tf.reshape(out, orig_shape, name='output')


def maybe_freeze_affine(getter, *args, **kwargs):
    # custom getter to freeze affine params inside bn
    name = args[0] if len(args) else kwargs.get('name')
    if name.endswith('/gamma') or name.endswith('/beta'):
        if cfg.BACKBONE.FREEZE_AFFINE:
            kwargs['trainable'] = False
    return getter(*args, **kwargs)


def maybe_reverse_pad(topleft, bottomright):
    if cfg.BACKBONE.TF_PAD_MODE:
        return [topleft, bottomright]
    return [bottomright, topleft]


@contextmanager
def backbone_argscope():
    def nonlin(x):
        x = get_norm()(x)
        return tf.nn.relu(x)

    with argscope([Conv2D, MaxPooling, BatchNorm], data_format='channels_first'), \
            argscope(Conv2D, use_bias=False, activation=nonlin), \
            argscope(BatchNorm, training=False), \
            custom_getter_scope(maybe_freeze_affine):
        yield


@contextmanager
def maybe_syncbn_scope():
    if cfg.BACKBONE.NORM == 'SyncBN':
        assert cfg.BACKBONE.FREEZE_AT == 2  # TODO add better support
        with argscope(BatchNorm, training=None,
                      sync_statistics='nccl' if cfg.TRAINER == 'replicated' else 'horovod'):
            yield
    else:
        yield


def image_preprocess(image, bgr=True):
    with tf.name_scope('image_preprocess'):
        if image.dtype.base_dtype != tf.float32:
            image = tf.cast(image, tf.float32)

        mean = cfg.PREPROC.PIXEL_MEAN
        std = cfg.PREPROC.PIXEL_STD
        if bgr:
            mean = mean[::-1]
            std = std[::-1]
        image_mean = tf.constant(mean, dtype=tf.float32)
        image_std = tf.constant(std, dtype=tf.float32)
        image = (image - image_mean) / image_std
        return image


def get_norm(zero_init=False):
    if cfg.BACKBONE.NORM == 'GN':
        Norm = GroupNorm
        layer_name = 'gn'
    else:
        Norm = BatchNorm
        layer_name = 'bn'
    return lambda x: Norm(layer_name, x, gamma_initializer=tf.zeros_initializer() if zero_init else None)


def resnet_shortcut(l, n_out, stride, activation=tf.identity):
    n_in = l.shape[1]
    if n_in != n_out:   # change dimension when channel is not the same
        # TF's SAME mode output ceil(x/stride), which is NOT what we want when x is odd and stride is 2
        # In FPN mode, the images are pre-padded already.
        if not cfg.MODE_FPN and stride == 2:
            l = l[:, :, :-1, :-1]
        return Conv2D('convshortcut', l, n_out, 1,
                      strides=stride, activation=activation)
    else:
        return l


def resnet_bottleneck(l, ch_out, stride):
    shortcut = l
    if cfg.BACKBONE.STRIDE_1X1:
        if stride == 2:
            l = l[:, :, :-1, :-1]
        l = Conv2D('conv1', l, ch_out, 1, strides=stride)
        l = Conv2D('conv2', l, ch_out, 3, strides=1)
    else:
        l = Conv2D('conv1', l, ch_out, 1, strides=1)
        if stride == 2:
            l = tf.pad(l, [[0, 0], [0, 0], maybe_reverse_pad(0, 1), maybe_reverse_pad(0, 1)])
            l = Conv2D('conv2', l, ch_out, 3, strides=2, padding='VALID')
        else:
            l = Conv2D('conv2', l, ch_out, 3, strides=stride)
    l = Conv2D('conv3', l, ch_out * 4, 1, activation=get_norm(zero_init=True))
    ret = l + resnet_shortcut(shortcut, ch_out * 4, stride, activation=get_norm(zero_init=False))
    return tf.nn.relu(ret, name='output')


def resnet_group(name, l, block_func, features, count, stride):
    with tf.variable_scope(name):
        for i in range(0, count):
            with tf.variable_scope('block{}'.format(i)):
                l = block_func(l, features, stride if i == 0 else 1)
    return l


def resnet_c4_backbone(image, num_blocks):
    assert len(num_blocks) == 3
    with backbone_argscope():
        l = tf.pad(image, [[0, 0], [0, 0], maybe_reverse_pad(2, 3), maybe_reverse_pad(2, 3)])
        l = Conv2D('conv0', l, 64, 7, strides=2, padding='VALID')
        l = tf.pad(l, [[0, 0], [0, 0], maybe_reverse_pad(0, 1), maybe_reverse_pad(0, 1)])
        l = MaxPooling('pool0', l, 3, strides=2, padding='VALID')
        c2 = resnet_group('group0', l, resnet_bottleneck, 64, num_blocks[0], 1)
        # TODO replace var by const to enable optimization
        if cfg.BACKBONE.FREEZE_AT == 2:
            c2 = tf.stop_gradient(c2)
        with maybe_syncbn_scope():
            c3 = resnet_group('group1', c2, resnet_bottleneck, 128, num_blocks[1], 2)
            c4 = resnet_group('group2', c3, resnet_bottleneck, 256, num_blocks[2], 2)
    # 16x downsampling up to now
    return c4


@auto_reuse_variable_scope
def resnet_conv5(image, num_block):
    with backbone_argscope(), maybe_syncbn_scope():
        l = resnet_group('group3', image, resnet_bottleneck, 512, num_block, 2)
        return l


def resnet_fpn_backbone(image, num_blocks):
    shape2d = tf.shape(image)[2:]
    mult = float(cfg.FPN.RESOLUTION_REQUIREMENT)
    new_shape2d = tf.to_int32(tf.ceil(tf.to_float(shape2d) / mult) * mult)
    pad_shape2d = new_shape2d - shape2d
    assert len(num_blocks) == 4, num_blocks
    with backbone_argscope():
        chan = image.shape[1]
        pad_base = maybe_reverse_pad(2, 3)
        l = tf.pad(image, tf.stack(
            [[0, 0], [0, 0],
             [pad_base[0], pad_base[1] + pad_shape2d[0]],
             [pad_base[0], pad_base[1] + pad_shape2d[1]]]))
        l.set_shape([None, chan, None, None])
        l = Conv2D('conv0', l, 64, 7, strides=2, padding='VALID')
        l = tf.pad(l, [[0, 0], [0, 0], maybe_reverse_pad(0, 1), maybe_reverse_pad(0, 1)])
        l = MaxPooling('pool0', l, 3, strides=2, padding='VALID')
        c2 = resnet_group('group0', l, resnet_bottleneck, 64, num_blocks[0], 1)
        if cfg.BACKBONE.FREEZE_AT == 2:
            c2 = tf.stop_gradient(c2)
        with maybe_syncbn_scope():
            c3 = resnet_group('group1', c2, resnet_bottleneck, 128, num_blocks[1], 2)
            c4 = resnet_group('group2', c3, resnet_bottleneck, 256, num_blocks[2], 2)
            c5 = resnet_group('group3', c4, resnet_bottleneck, 512, num_blocks[3], 2)
    # 32x downsampling up to now
    # size of c5: ceil(input/32)
    return c2, c3, c4, c5
