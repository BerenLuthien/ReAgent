#!/usr/bin/env python3
# Copyright (c) Facebook, Inc. and its affiliates. All rights reserved.

import logging
from typing import Callable, List, Optional

import numpy as np
import reagent.core.types as rlt
import torch
import torch.nn.functional as F
from reagent.core.parameters import NormalizationData
from reagent.preprocessing.preprocessor import Preprocessor
from reagent.preprocessing.sparse_preprocessor import make_sparse_preprocessor


logger = logging.getLogger(__name__)


class Compose:
    """
    Applies an iterable collection of transform functions
    """

    def __init__(self, *transforms):
        self.transforms = transforms

    def __call__(self, data):
        for t in self.transforms:
            data = t(data)
        return data

    def __repr__(self):
        transforms = "\n    ".join([repr(t) for t in self.transforms])
        return f"{self.__class__.__name__}(\n{transforms}\n)"


# TODO: this wouldn't work for possible_actions_mask (list of value, presence)
class ValuePresence:
    """
    For every key `x`, looks for `x_presence`; if `x_presence` exists,
    replace `x` with tuple of `x` and `x_presence`, delete `x_presence` key
    """

    def __call__(self, data):
        keys = list(data.keys())

        for k in keys:
            presence_key = f"{k}_presence"
            if presence_key in data:
                data[k] = (data[k], data[presence_key])
                del data[presence_key]

        return data


class Lambda:
    """Applies an arbitrary callable transform"""

    def __init__(self, keys: List[str], fn: Callable):
        self.keys = keys
        self.fn = fn

    def __call__(self, data):
        for k in self.keys:
            data[k] = self.fn(data[k])
        return data


class SelectValuePresenceColumns:
    """
    Select columns from value-presence source key
    """

    def __init__(self, source: str, dest: str, indices: List[int]):
        self.source = source
        self.dest = dest
        self.indices = indices

    def __call__(self, data):
        value, presence = data[self.source]
        data[self.dest] = (value[:, self.indices], presence[:, self.indices])
        return data


class DenseNormalization:
    """
    Normalize the `keys` using `normalization_data`.
    The keys are expected to be `Tuple[torch.Tensor, torch.Tensor]`,
    where the first element is the value and the second element is the
    presence mask.
    This transform replaces the keys in the input data.
    """

    def __init__(
        self,
        keys: List[str],
        normalization_data: NormalizationData,
        device: Optional[torch.device] = None,
    ):
        """
        Args:
            keys: the name of the keys to be transformed
        """
        self.keys = keys
        self.normalization_data = normalization_data
        self.device = device or torch.device("cpu")
        # Delay the initialization of the preprocessor so this class
        # is pickleable
        self._preprocessor: Optional[Preprocessor] = None

    def __call__(self, data):
        if self._preprocessor is None:
            self._preprocessor = Preprocessor(
                self.normalization_data.dense_normalization_parameters,
                device=self.device,
            )

        for k in self.keys:
            value, presence = data[k]
            value, presence = value.to(self.device), presence.to(self.device)
            presence[torch.isnan(value)] = 0
            value[torch.isnan(value)] = 0
            data[k] = self._preprocessor(value, presence).float()

        return data


class MapIDListFeatures:
    """
    Applies a SparsePreprocessor (see sparse_preprocessor.SparsePreprocessor)
    """

    def __init__(
        self,
        id_list_keys: List[str],
        id_score_list_keys: List[str],
        feature_config: rlt.ModelFeatureConfig,
        device: torch.device,
    ):
        self.id_list_keys = id_list_keys
        self.id_score_list_keys = id_score_list_keys
        assert (
            set(id_list_keys).intersection(set(id_score_list_keys)) == set()
        ), f"id_list_keys: {id_list_keys}; id_score_list_keys: {id_score_list_keys}"
        self.feature_config = feature_config
        self.sparse_preprocessor = make_sparse_preprocessor(
            feature_config=feature_config, device=device
        )

    def __call__(self, data):
        for k in self.id_list_keys + self.id_score_list_keys:
            # if no ids, it means we're not using sparse features.
            if not self.feature_config.id2name or k not in data:
                data[k] = None
                continue

            assert isinstance(data[k], dict), f"{k} has type {type(data[k])}. {data[k]}"
            if k in self.id_list_keys:
                data[k] = self.sparse_preprocessor.preprocess_id_list(data[k])
            else:
                data[k] = self.sparse_preprocessor.preprocess_id_score_list(data[k])
        return data


class OneHotActions:
    """
    Keys should be in the set {0,1,2,...,num_actions}, where
    a value equal to num_actions denotes that it's not valid.
    """

    def __init__(self, keys: List[str], num_actions: int):
        self.keys = keys
        self.num_actions = num_actions

    def __call__(self, data):
        for k in self.keys:
            # we do + 1 and then index up to n because value could be num_actions,
            # in which case the result is a zero-vector
            data[k] = F.one_hot(data[k], self.num_actions + 1).index_select(
                -1, torch.arange(self.num_actions)
            )
        return data


class ColumnVector:
    """
    Ensure that the keys are column vectors
    """

    def __init__(self, keys: List[str]):
        self.keys = keys

    def __call__(self, data):
        for k in self.keys:
            raw_value = data[k]
            if isinstance(raw_value, tuple):
                value, _presence = raw_value
            elif isinstance(raw_value, list):
                # TODO(T67265031): make mdp_id a tensor, which we will be able to
                # when column type changes to int
                value = np.array(raw_value)
            elif isinstance(raw_value, torch.Tensor):
                # TODO(T67265031): this is an identity mapping, which is only necessary
                # when mdp_id in traced batch preprocessors becomes a tensor (mdp_id
                # is a list of strings in normal batch preprocessors).
                value = raw_value
            else:
                raise NotImplementedError(f"value of type {type(raw_value)}.")

            assert value.ndim == 1 or (
                value.ndim == 2 and value.shape[1] == 1
            ), f"Invalid shape for key {k}: {value.shape}"
            data[k] = value.reshape(-1, 1)

        return data


class MaskByPresence:
    """
    Expect data to be (value, presence) and return value * presence.
    This zeros out values that aren't present.
    """

    def __init__(self, keys: List[str]):
        self.keys = keys

    def __call__(self, data):
        for k in self.keys:
            value_presence = data[k]
            assert (
                isinstance(value_presence, tuple) and len(value_presence) == 2
            ), f"Not valid value, presence tuple: {value_presence}"
            value, presence = value_presence
            assert value.shape == presence.shape, (
                f"Unmatching value shape ({value.shape})"
                f" and presence shape ({presence.shape})"
            )
            data[k] = value * presence.float()

        return data


class StackDenseFixedSizeArray:
    """
    If data is a tensor, ensures it has the correct shape. If data is a list of
    (value, presence) discards the presence tensors and concatenates the values
    to output a tensor of shape (batch_size, feature_dim).
    """

    def __init__(self, keys: List[str], size: int, dtype=torch.float):
        self.keys = keys
        self.size = size
        self.dtype = dtype

    def __call__(self, data):
        for k in self.keys:
            value = data[k]
            if isinstance(value, torch.Tensor):
                # Just ensure the shape
                if not (value.ndim == 2 and value.shape[1] == self.size):
                    raise ValueError(f"Wrong shape for key {k}: {value.shape}")
                data[k] = value.to(self.dtype)
            else:
                # Assuming that value is List[Tuple[torch.Tensor, torch.Tensor]]
                data[k] = (
                    torch.cat([v for v, p in value], dim=0)
                    .view(-1, self.size)
                    .to(dtype=self.dtype)
                )
        return data


class FixedLengthSequences:
    """
    Does two things:
        1. makes sure each sequence in the list of keys has the expected fixed length
        2. if to_keys is provided, copies the relevant sequence_id to the new key,
        otherwise overwrites the old key

    Expects each data[key] to be `Dict[Int, Tuple[Tensor, T]]`. Where:
    - key is the feature id
    - sequence_id is the key of the dict data[key]
    - The first element of the tuple is the offset for each example, which is expected to be in fixed interval.
    - The second element is the data at each step in the sequence

    This is mainly for FB internal use,
    see fbcode/caffe2/caffe2/fb/proto/io_metadata.thrift
    for the data format extracted from SequenceFeatureMetadata

    NOTE: this is not product between two lists (keys and to_keys);
    it's setting keys[sequence_id] to to_keys in a parallel way
    """

    def __init__(
        self,
        keys: List[str],
        sequence_id: int,
        expected_length: Optional[int] = None,
        *,
        to_keys: Optional[List[str]] = None,
    ):
        self.keys = keys
        self.sequence_id = sequence_id
        self.to_keys = to_keys or keys
        assert len(self.to_keys) == len(keys)
        self.expected_length = expected_length

    def __call__(self, data):
        for key, to_key in zip(self.keys, self.to_keys):
            offsets, value = data[key][self.sequence_id]
            # TODO assert regarding offsets length compared to value
            expected_length = self.expected_length
            if expected_length is None:
                if len(offsets) > 1:
                    # If batch size is larger than 1, just use the offsets
                    expected_length = (offsets[1] - offsets[0]).item()
                else:
                    # If batch size is 1
                    expected_length = value[0].shape[0]
                self.expected_length = expected_length
            expected_offsets = torch.arange(
                0, offsets.shape[0] * expected_length, expected_length
            )
            assert all(
                expected_offsets == offsets
            ), f"Unexpected offsets for {key} {self.sequence_id}: {offsets}. Expected {expected_offsets}"

            data[to_key] = value
        return data


class SlateView:
    """
    Assuming that the keys are flatten fixed-length sequences with length of
    `slate_size`, unflatten it by inserting `slate_size` to the 1st dim.
    I.e., turns the input from the shape of `[B * slate_size, D]` to
    `[B, slate_size, D]`.
    """

    def __init__(self, keys: List[str], slate_size: int):
        self.keys = keys
        self.slate_size = slate_size

    def __call__(self, data):
        for k in self.keys:
            value = data[k]
            _, dim = value.shape
            data[k] = value.view(-1, self.slate_size, dim)

        return data


class FixedLengthSequenceDenseNormalization:
    """
    Combines the FixedLengthSequences, DenseNormalization, and SlateView transforms
    """

    def __init__(
        self,
        keys: List[str],
        sequence_id: int,
        normalization_data: NormalizationData,
        expected_length: Optional[int] = None,
        device: Optional[torch.device] = None,
    ):
        to_keys = [f"{k}:{sequence_id}" for k in keys]
        self.fixed_length_sequences = FixedLengthSequences(
            keys, sequence_id, to_keys=to_keys, expected_length=expected_length
        )
        self.dense_normalization = DenseNormalization(
            to_keys, normalization_data, device=device
        )
        # We will override this in __call__()
        self.slate_view = SlateView(to_keys, slate_size=-1)

    def __call__(self, data):
        data = self.fixed_length_sequences(data)
        data = self.dense_normalization(data)
        self.slate_view.slate_size = self.fixed_length_sequences.expected_length
        return self.slate_view(data)


class AppendConstant:
    """
    Append a column of constant value at the beginning of the specified dimension
    Can be used to add a column of "1" to the Linear Regression input data to capture intercept/bias
    """

    def __init__(self, keys: List[str], dim: int = -1, const: float = 1.0):
        self.keys = keys
        self.dim = dim
        self.const = const

    def __call__(self, data):
        for k in self.keys:
            value = data[k]
            extra_col = self.const * torch.ones(value.shape[:-1]).unsqueeze(-1)
            data[k] = torch.cat((extra_col, value), dim=self.dim)
        return data


class UnsqueezeRepeat:
    """
    This transform adds an extra dimension to the tensor and repeats
        the tensor along that dimension
    """

    def __init__(self, keys: List[str], dim: int, num_repeat: int = 1):
        self.keys = keys
        self.dim = dim
        self.num_repeat = num_repeat

    def __call__(self, data):
        for k in self.keys:
            data[k] = data[k].unsqueeze(self.dim)
            if self.num_repeat != 1:
                repeat_counters = [1 for _ in range(data[k].ndim)]
                repeat_counters[self.dim] = self.num_repeat
                data[k] = data[k].repeat(*repeat_counters)
        return data


def _get_product_features(x: torch.Tensor, y: torch.Tensor) -> torch.Tensor:
    """
    Get outer product of 2 tensors along the last dimension.
    All dimensions except last are preserved. The last dimension is replaced
        with flattened outer products of last-dimension-vectors from input tensors

    This is a vectorized implementation of (for 2D case):
    for i in range(x.shape[0]):
        out[i, :] = torch.outer(x[i, :], y[i, :]).flatten()

    For 2D inputs:
        Input shapes:
            x: (batch, feature_dim_x)
            y: (batch, feature_dim_y)
        Output shape:
            (batch, feature_dim_x*feature_dim_y)
    """
    return torch.einsum("...i,...j->...ij", (x, y)).flatten(start_dim=-2)


class OuterProduct:
    """
    This transform creates a tensor with an outer product of elements of 2 tensors.
    The outer product is stored under the new key.
    The 2 input tensors might be dropped, depending on input arguments
    """

    def __init__(
        self,
        key1: str,
        key2: str,
        output_key: str,
        drop_inputs: bool = False,
    ):
        self.key1 = key1
        self.key2 = key2
        self.output_key = output_key
        self.drop_inputs = drop_inputs

    def __call__(self, data):
        x = data[self.key1]
        y = data[self.key2]
        prod = _get_product_features(x, y)
        data[self.output_key] = prod
        if self.drop_inputs:
            del data[self.key1], data[self.key2]
        return data


class GetEye:
    """
    Place a diagonal tensor into the data dictionary
    """

    def __init__(self, key: str, size: int):
        self.key = key
        self.size = size

    def __call__(self, data):
        x = torch.eye(self.size)
        data[self.key] = x
        return data


def _broadcast_tensors_for_cat(
    tensors: List[torch.Tensor], dim: int
) -> List[torch.Tensor]:
    """
    Broadcast all tensors so that they could be concatenated along the specific dim.
    The tensor shapes have to be broadcastable (after the concatenation dim is taken out)

    Example:
    Input tensors of shapes [(10,3,5), (1,3,3)] (dim=2) would get broadcasted to [(10,3,5), (10,3,3)],
        so that they could be concatenated along the last dim.
    """
    if dim >= 0:
        dims = [dim] * len(tensors)
    else:
        dims = [t.ndim + dim for t in tensors]
    shapes = [list(t.shape) for t in tensors]
    for s, d in zip(shapes, dims):
        s.pop(d)
    shapes_except_cat_dim = [tuple(s) for s in shapes]
    broadcast_shape = torch.broadcast_shapes(*shapes_except_cat_dim)
    final_shapes = [list(broadcast_shape) for t in tensors]
    for s, t, d in zip(final_shapes, tensors, dims):
        s.insert(d, t.shape[dim])
    final_shapes = [tuple(s) for s in final_shapes]
    return [t.expand(s) for t, s in zip(tensors, final_shapes)]


class Cat:
    """
    This transform concatenates the tensors along a specified dim
    """

    def __init__(
        self, input_keys: List[str], output_key: str, dim: int, broadcast: bool = True
    ):
        self.input_keys = input_keys
        self.output_key = output_key
        self.dim = dim
        self.broadcast = broadcast

    def __call__(self, data):
        tensors = []
        for k in self.input_keys:
            tensors.append(data[k])
        if self.broadcast:
            tensors = _broadcast_tensors_for_cat(tensors, self.dim)
        data[self.output_key] = torch.cat(tensors, dim=self.dim)
        return data


class Rename:
    """
    Change key names
    """

    def __init__(self, old_names: List[str], new_names: List[str]):
        self.old_names = old_names
        self.new_names = new_names

    def __call__(self, data):
        new_data = dict(data)
        for o, n in zip(self.old_names, self.new_names):
            new_data[n] = new_data.pop(o)
        return new_data


class Filter:
    """
    Remove some keys from the dict.
    Can specify keep_keys (they will be kept) or remove_keys (they will be removed)
    """

    def __init__(
        self,
        *,
        keep_keys: Optional[List[str]] = None,
        remove_keys: Optional[List[str]] = None,
    ):
        assert (keep_keys is None) != (remove_keys is None)
        self.keep_keys = keep_keys
        self.remove_keys = remove_keys

    def __call__(self, data):
        if self.keep_keys:
            new_data = {}
            for k in self.keep_keys:
                if k in data:
                    new_data[k] = data[k]
        else:
            new_data = dict(data)
            for k in self.remove_keys:
                if k in new_data:
                    del new_data[k]
        return new_data
