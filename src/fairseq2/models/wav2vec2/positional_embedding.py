# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

from typing import Optional, final

import torch
import torch.nn as nn
from overrides import final as finaloverride
from overrides import override
from torch import Tensor
from torch.nn import GELU, Conv1d, LayerNorm, Module, Sequential
from torch.nn.utils.weight_norm import remove_weight_norm, weight_norm

from fairseq2.nn.incremental_state import IncrementalStateBag
from fairseq2.nn.positional_embedding import PositionalEmbedding
from fairseq2.nn.utils.mask import apply_padding_mask


@final
class Wav2Vec2PositionalEmbedding(PositionalEmbedding):
    """Produces positional embeddings as described in Section 2 of
    :cite:t:`baevski2020wav2vec`."""

    conv: Conv1d
    remove_pad: bool
    activation: GELU

    def __init__(
        self,
        embed_dim: int,
        kernel_size: int = 128,
        num_groups: int = 16,
        device: Optional[torch.device] = None,
        dtype: Optional[torch.dtype] = None,
    ) -> None:
        """
        :param embed_dim:
            The dimensionality of positional embeddings.
        :param kernel_size:
            The kernel size of the 1D convolution.
        :param num_groups:
            The number of convolution groups.
        """
        super().__init__(embed_dim, max_seq_len=None)

        self.conv = Wav2Vec2PositionalEmbeddingConv1d(
            embed_dim,
            embed_dim,
            kernel_size,
            padding=kernel_size // 2,
            groups=num_groups,
            device=device,
            dtype=dtype,
        )

        self.remove_pad = kernel_size % 2 == 0

        self.activation = GELU()

    @finaloverride
    def _do_forward(
        self,
        seqs: Tensor,
        padding_mask: Optional[Tensor],
        state_bag: Optional[IncrementalStateBag],
    ) -> Tensor:
        """:meta private:"""
        if state_bag is not None:
            raise ValueError(
                "`Wav2Vec2PositionalEmbedding` does not support incremental encoding."
            )

        # We have to ensure that the padded elements are correctly set to
        # zero; otherwise, noise will leak into the feature maps.
        seqs = apply_padding_mask(seqs, padding_mask)

        # (N, S, E) -> (N, E, S)
        embed = seqs.transpose(1, 2)

        # (N, E, S) -> (N, E, S)
        embed = self.conv(embed)

        if self.remove_pad:
            embed = embed[:, :, :-1]

        embed = self.activation(embed)

        # (N, E, S) -> (N, S, E)
        embed = embed.transpose(1, 2)

        return seqs + embed


class Wav2Vec2PositionalEmbeddingConv1d(Conv1d):
    """Represents the convolution used in :class:`Wav2Vec2PositionalEmbedding`."""

    @override
    def reset_parameters(self) -> None:
        embed_dim, kernel_size = self.in_channels, self.kernel_size[0]

        try:
            remove_weight_norm(self)
        except ValueError:
            # Raised during the `__init__` call since we don't have the weight
            # norm hook registered yet. Safe to ignore.
            pass

        nn.init.normal_(
            self.weight, mean=0.0, std=(4.0 / (kernel_size * embed_dim)) ** 0.5
        )

        weight_norm(self, dim=2)

        if self.bias is not None:
            nn.init.constant_(self.bias, 0.0)


@final
class Wav2Vec2StackedPositionalEmbedding(PositionalEmbedding):
    """Produces positional embeddings using a stack of 1D convolutions.

    This positional embedding is not mentioned in :cite:t:`baevski2020wav2vec`,
    but exists in the reference implementation."""

    layers: Sequential

    def __init__(
        self,
        embed_dim: int,
        kernel_size: int,
        num_groups: int,
        num_layers: int,
        norm_eps: float = 1e-5,
        device: Optional[torch.device] = None,
        dtype: Optional[torch.dtype] = None,
    ) -> None:
        """
        :param embed_dim:
            The dimensionality of positional embeddings.
        :param kernel_size:
            The total kernel size of the 1D convolutions. Each convolution uses
            a kernel size of ``max(3, kernel_size // num_layers)``.
        :param num_groups:
            The number of convolution groups.
        :param num_layers:
            The number of convolution layers.
        :param norm_eps:
            The epsilon value to add to the denominator of the
            :class:`~torch.nn.LayerNorm` modules for numerical stability.
        """
        super().__init__(embed_dim, max_seq_len=None)

        k = max(3, kernel_size // num_layers)

        self.layers = Sequential()

        for _ in range(num_layers):
            layer = Wav2Vec2PositionalEmbeddingLayer(
                embed_dim, k, num_groups, norm_eps, device, dtype
            )

            self.layers.append(layer)

    @finaloverride
    def _do_forward(
        self,
        seqs: Tensor,
        padding_mask: Optional[Tensor],
        state_bag: Optional[IncrementalStateBag],
    ) -> Tensor:
        """:meta private:"""
        if state_bag is not None:
            raise ValueError(
                "`Wav2Vec2StackedPositionalEmbedding` does not support incremental encoding."
            )

        # We have to ensure that the padded elements are correctly set to
        # zero; otherwise, noise will leak into the feature maps.
        seqs = apply_padding_mask(seqs, padding_mask)

        # (N, S, E) -> (N, E, S)
        embed = seqs.transpose(1, 2)

        # (N, E, S) -> (N, E, S)
        embed = self.layers(embed)

        # (N, E, S) -> (N, S, E)
        embed = embed.transpose(1, 2)

        return seqs + embed


class Wav2Vec2PositionalEmbeddingLayer(Module):
    """Represents a layer used in :class:`Wav2Vec2StackedPositionalEmbedding`."""

    conv: Conv1d
    layer_norm: LayerNorm
    activation: GELU

    def __init__(
        self,
        embed_dim: int,
        kernel_size: int,
        num_groups: int,
        norm_eps: float = 1e-5,
        device: Optional[torch.device] = None,
        dtype: Optional[torch.dtype] = None,
    ) -> None:
        super().__init__()

        self.conv = Conv1d(
            embed_dim,
            embed_dim,
            kernel_size,
            padding="same",
            groups=num_groups,
            device=device,
            dtype=dtype,
        )

        self.layer_norm = LayerNorm(
            embed_dim, norm_eps, elementwise_affine=False, device=device, dtype=dtype
        )

        self.activation = GELU()

    def forward(self, seqs: Tensor) -> Tensor:
        # (N, E, S) -> (N, E, S)
        seqs = self.conv(seqs)

        # (N, E, S) -> (N, S, E)
        seqs = seqs.transpose(1, 2)

        seqs = self.layer_norm(seqs)

        # (N, S, E) -> (N, E, S)
        seqs = seqs.transpose(1, 2)

        seqs = self.activation(seqs)

        return seqs