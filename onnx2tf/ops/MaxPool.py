import random
random.seed(0)
import numpy as np
np.random.seed(0)
import tensorflow as tf
import onnx_graphsurgeon as gs
from utils.common_functions import (
    get_constant_or_variable,
    calc_pads_same_pooling,
    pad_input,
    remove_dilations,
)


def make_node(
    *,
    graph_node: gs.Node,
    tf_layers_dict: dict,
    **kwargs: dict,
):
    """MaxPool

    Parameters
    ----------
    graph_node: gs.Node
        graph_surgeon Node

    tf_layers_dict: dict
        optype, shape, dtype, tensorflow graph
    """
    graph_node_input = get_constant_or_variable(graph_node.inputs[0])
    graph_node_output: gs.Variable = graph_node.outputs[0]
    shape = graph_node_output.shape
    dtype = graph_node_output.dtype

    input_tensor = tf_layers_dict[graph_node_input.name]['tf_node'] \
        if isinstance(graph_node_input, gs.Variable) else graph_node_input

    # 0: False, 1: True
    ceil_mode = bool(graph_node.attrs.get('ceil_mode', 0))
    # 0: False, 1: True
    count_include_pad = bool(graph_node.attrs.get('count_include_pad', 0))
    kernel_shape = graph_node.attrs['kernel_shape']
    spatial_size = len(kernel_shape)
    x_rank = spatial_size + 2
    strides = graph_node.attrs.get('strides', [1] * spatial_size)
    dilations = graph_node.attrs.get('dilations', [1] * spatial_size)
    is_known_shape = input_tensor.shape.is_fully_defined()
    input_tensor_shape = input_tensor.shape
    input_tensor_dtype = input_tensor.dtype

    pads = graph_node.attrs.get('auto_pad', 'NOTSET')
    if pads == 'NOTSET':
        pads = graph_node.attrs.get('pads', [0] * spatial_size * 2)
        if is_known_shape and pads != [0] * spatial_size * 2:
            in_shape = input_tensor.get_shape()
            same_paddings = calc_pads_same_pooling(
                in_spatial_shape=in_shape[1:x_rank - 1],
                kernel_shape=kernel_shape,
                strides=strides,
                dilations=dilations,
                padding='SAME_UPPER',
            )
            if pads == same_paddings:
                pads = 'SAME_UPPER'

    is_explicit_padding = type(pads) is list
    padding_ = ''

    if is_explicit_padding or pads == 'SAME_LOWER' or (pads == 'SAME_UPPER' and count_include_pad):
        # pad the input
        padded_tensor = pad_input(
            input_tensor=input_tensor,
            is_known_shape=is_known_shape,
            kernel_shape=kernel_shape,
            ceil_mode=ceil_mode,
            spatial_size=spatial_size,
            strides=strides,
            dilations=dilations,
            padding=pads,
            padding_constant=0,
        )
        padding_ = 'VALID'

    elif pads == 'SAME_UPPER':
        padded_tensor = input_tensor
        padding_ = 'SAME'

    else:
        padded_tensor = input_tensor
        padding_ = 'SAME'

    # Preserving Graph Structure (Dict)
    tf_layers_dict[graph_node_output.name] = {
        'optype': graph_node.op,
        'shape': shape,
        'dtype': dtype,
    }

    # Generation of TF OP

    # tf.nn.dilation2d
    if spatial_size == 2 and dilations != [1] * spatial_size:
        strides = [1] + list(strides) + [1]
        dilations = [1] + list(dilations) + [1]

        # tf.nn.dilation2d only support data_format='NHWC'
        filter = tf.zeros(
            [kernel_shape[0], kernel_shape[1], input_tensor_shape[1]],
            input_tensor_dtype,
        )
        pooled_tensor = tf.nn.dilation2d(
            input=input_tensor,
            filters=filter,
            strides=strides,
            dilations=dilations,
            padding=padding_,
        )

    # if spatial_size < 4 and strides == 1 or dilation == 1 use tf.nn.pool
    elif spatial_size < 4 and (strides == [1] * spatial_size or dilations == [1] * spatial_size):
        # if strides == 1 and not LpPool use tf.nn.pool directly
        if strides == [1] * spatial_size:
            pooled_tensor = tf.nn.pool(
                input=input_tensor,
                window_shape=kernel_shape,
                dilations=dilations,
                strides=strides,
                padding=padding_,
                pooling_type='MAX',
            )
        else:
            # othwerwise check the pooling_type and use the correct op
            pooled_tensor = tf.nn.max_pool(
                input=input_tensor,
                ksize=kernel_shape,
                strides=strides,
                padding=padding_,
            )
    # in any other case we use custom implementation _remove_dilations
    # to reduce atrous/dilated pooling into regular pooling and selecting
    # only the values of the input that should have been selected by
    # applying the strides and dilations. Then use tf.nn.pool with
    # strides = kernel_shape and no dilations
    else:
        padded_tensor = input_tensor
        if padding_ == 'SAME':
            # pad the input
            padded_tensor = pad_input(
                input_tensor=input_tensor,
                is_known_shape=is_known_shape,
                kernel_shape=kernel_shape,
                ceil_mode=ceil_mode,
                spatial_size=spatial_size,
                strides=strides,
                dilations=dilations,
                padding=pads,
                padding_constant=0,
            )
        input_ = remove_dilations(
            input_tensor=padded_tensor,
            kernel_shape=kernel_shape,
            spatial_size=spatial_size,
            strides=strides,
            dilations=dilations,
        )
        pooled_tensor = tf.nn.pool(
            input=input_,
            window_shape=kernel_shape,
            strides=kernel_shape,
            padding='VALID',
            pooling_type='MAX',
        )

    tf_layers_dict[graph_node_output.name]['tf_node'] = pooled_tensor
