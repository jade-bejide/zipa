# Copyright    2024  Xiaomi Corp.        (authors: Zengwei Yao)
#
# See ../../../../LICENSE for clarification regarding multiple authors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


# Copied from https://github.com/lhotse-speech/lhotse/blob/master/lhotse/dataset/signal_transforms.py
# with minor modification for cr-ctc training.


import math
import random
from typing import Any, Dict, Optional, Tuple

import torch
from lhotse.dataset.signal_transforms import time_warp as time_warp_impl


class SpecAugment(torch.nn.Module):
    """SpecAugment from lhotse with minor modification, returning time masks.

    SpecAugment performs three augmentations:
    - time warping of the feature matrix
    - masking of ranges of features (frequency bands)
    - masking of ranges of frames (time)

    The current implementation works with batches, but processes each example separately
    in a loop rather than simultaneously to achieve different augmentation parameters for
    each example.
    """

    def __init__(
        self,
        time_warp_factor: Optional[int] = 80,
        num_feature_masks: int = 2,
        features_mask_size: int = 27,
        num_frame_masks: int = 10,
        frames_mask_size: int = 100,
        max_frames_mask_fraction: float = 0.15,
        p=0.9,
    ):
        """
        SpecAugment's constructor.

        :param time_warp_factor: parameter for the time warping; larger values mean more warping.
            Set to ``None``, or less than ``1``, to disable.
        :param num_feature_masks: how many feature masks should be applied. Set to ``0`` to disable.
        :param features_mask_size: the width of the feature mask (expressed in the number of masked feature bins).
            This is the ``F`` parameter from the SpecAugment paper.
        :param num_frame_masks: the number of masking regions for utterances. Set to ``0`` to disable.
        :param frames_mask_size: the width of the frame (temporal) masks (expressed in the number of masked frames).
            This is the ``T`` parameter from the SpecAugment paper.
        :param max_frames_mask_fraction: limits the size of the frame (temporal) mask to this value times the length
            of the utterance (or supervision segment).
            This is the parameter denoted by ``p`` in the SpecAugment paper.
        :param p: the probability of applying this transform.
            It is different from ``p`` in the SpecAugment paper!
        """
        super().__init__()
        assert 0 <= p <= 1
        assert num_feature_masks >= 0
        assert num_frame_masks >= 0
        assert features_mask_size > 0
        assert frames_mask_size > 0
        self.time_warp_factor = time_warp_factor
        self.num_feature_masks = num_feature_masks
        self.features_mask_size = features_mask_size
        self.num_frame_masks = num_frame_masks
        self.frames_mask_size = frames_mask_size
        self.max_frames_mask_fraction = max_frames_mask_fraction
        self.p = p

    def forward(
        self,
        features: torch.Tensor,
        supervision_segments: Optional[torch.IntTensor] = None,
        *args,
        **kwargs,
    ) -> Tuple[torch.Tensor, torch.Tensor]:
        """
        Computes SpecAugment for a batch of feature matrices.

        Since the batch will usually already be padded, the user can optionally
        provide a ``supervision_segments`` tensor that will be used to apply SpecAugment
        only to selected areas of the input. The format of this input is described below.

        :param features: a batch of feature matrices with shape ``(B, T, F)``.
        :param supervision_segments: an int tensor of shape ``(S, 3)``. ``S`` is the number of
            supervision segments that exist in ``features`` -- there may be either
            less or more than the batch size.
            The second dimension encoder three kinds of information:
            the sequence index of the corresponding feature matrix in `features`,
            the start frame index, and the number of frames for each segment.
        :return:
            - an augmented tensor of shape ``(B, T, F)``.
            - the corresponding time masks of shape ``(B, T)``.
        """
        assert len(features.shape) == 3, (
            "SpecAugment only supports batches of " "single-channel feature matrices."
        )
        features = features.clone()

        time_masks = []

        if supervision_segments is None:
            # No supervisions - apply spec augment to full feature matrices.
            for sequence_idx in range(features.size(0)):
                masked_feature, time_mask = self._forward_single(features[sequence_idx])
                features[sequence_idx] = masked_feature
                time_masks.append(time_mask)
        else:
            # Supervisions provided - we will apply time warping only on the supervised areas.
            for sequence_idx, start_frame, num_frames in supervision_segments:
                end_frame = start_frame + num_frames
                warped_feature, _ = self._forward_single(
                    features[sequence_idx, start_frame:end_frame], warp=True, mask=False
                )
                features[sequence_idx, start_frame:end_frame] = warped_feature
            # ... and then time-mask the full feature matrices. Note that in this mode,
            # it might happen that masks are applied to different sequences/examples
            # than the time warping.
            for sequence_idx in range(features.size(0)):
                masked_feature, time_mask = self._forward_single(
                    features[sequence_idx], warp=False, mask=True
                )
                features[sequence_idx] = masked_feature
                time_masks.append(time_mask)

        time_masks = torch.cat(time_masks, dim=0)
        assert time_masks.shape == features.shape[:-1], (time_masks.shape == features.shape[:-1])
        return features, time_masks

    def _forward_single(
        self, features: torch.Tensor, warp: bool = True, mask: bool = True
    ) -> Tuple[torch.Tensor, torch.Tensor]:
        """
        Apply SpecAugment to a single feature matrix of shape (T, F).
        """
        if random.random() > self.p:
            # Randomly choose whether this transform is applied
            time_mask = torch.zeros(
                1, features.size(0), dtype=torch.bool, device=features.device
            )
            return features, time_mask

        time_mask = None
        if warp:
            if self.time_warp_factor is not None and self.time_warp_factor >= 1:
                features = time_warp_impl(features, factor=self.time_warp_factor)

        if mask:
            mean = features.mean()
            # Frequency masking
            features, _ = mask_along_axis_optimized(
                features,
                mask_size=self.features_mask_size,
                mask_times=self.num_feature_masks,
                mask_value=mean,
                axis=2,
            )
            # Time masking
            max_tot_mask_frames = self.max_frames_mask_fraction * features.size(0)
            num_frame_masks = min(
                self.num_frame_masks,
                math.ceil(max_tot_mask_frames / self.frames_mask_size),
            )
            max_mask_frames = min(
                self.frames_mask_size, max_tot_mask_frames // num_frame_masks
            )
            features, time_mask = mask_along_axis_optimized(
                features,
                mask_size=max_mask_frames,
                mask_times=num_frame_masks,
                mask_value=mean,
                axis=1,
                return_time_mask=True,
            )

        return features, time_mask

    def state_dict(self, **kwargs) -> Dict[str, Any]:
        return dict(
            time_warp_factor=self.time_warp_factor,
            num_feature_masks=self.num_feature_masks,
            features_mask_size=self.features_mask_size,
            num_frame_masks=self.num_frame_masks,
            frames_mask_size=self.frames_mask_size,
            max_frames_mask_fraction=self.max_frames_mask_fraction,
            p=self.p,
        )

    def load_state_dict(self, state_dict: Dict[str, Any]):
        self.time_warp_factor = state_dict.get(
            "time_warp_factor", self.time_warp_factor
        )
        self.num_feature_masks = state_dict.get(
            "num_feature_masks", self.num_feature_masks
        )
        self.features_mask_size = state_dict.get(
            "features_mask_size", self.features_mask_size
        )
        self.num_frame_masks = state_dict.get("num_frame_masks", self.num_frame_masks)
        self.frames_mask_size = state_dict.get(
            "frames_mask_size", self.frames_mask_size
        )
        self.max_frames_mask_fraction = state_dict.get(
            "max_frames_mask_fraction", self.max_frames_mask_fraction
        )
        self.p = state_dict.get("p", self.p)


def mask_along_axis_optimized(
    features: torch.Tensor,
    mask_size: int,
    mask_times: int,
    mask_value: float,
    axis: int,
    return_time_mask: bool = False,
) -> torch.Tensor:
    """
    Apply Frequency and Time masking along axis.
    Frequency and Time masking as described in the SpecAugment paper.

    :param features: input tensor of shape ``(T, F)``
    :mask_size: the width size for masking.
    :mask_times: the number of masking regions.
    :mask_value: Value to assign to the masked regions.
    :axis: Axis to apply masking on (1 -> time, 2 -> frequency)
    :return_time_mask: Whether return the time mask of shape ``(1, T)``
    """
    if axis not in [1, 2]:
        raise ValueError("Only Frequency and Time masking are supported!")

    if return_time_mask and axis == 1:
        time_mask = torch.zeros(
            1, features.size(0), dtype=torch.bool, device=features.device
        )
    else:
        time_mask = None

    features = features.unsqueeze(0)
    features = features.reshape([-1] + list(features.size()[-2:]))

    values = torch.randint(int(0), int(mask_size), (1, mask_times))
    min_values = torch.rand(1, mask_times) * (features.size(axis) - values)
    mask_starts = (min_values.long()).squeeze()
    mask_ends = (min_values.long() + values.long()).squeeze()

    if axis == 1:
        if mask_times == 1:
            features[:, mask_starts:mask_ends] = mask_value
            if return_time_mask:
                time_mask[:, mask_starts:mask_ends] = True
            return features.squeeze(0), time_mask
        for (mask_start, mask_end) in zip(mask_starts, mask_ends):
            features[:, mask_start:mask_end] = mask_value
            if return_time_mask:
                time_mask[:, mask_start:mask_end] = True
    else:
        if mask_times == 1:
            features[:, :, mask_starts:mask_ends] = mask_value
            return features.squeeze(0), time_mask
        for (mask_start, mask_end) in zip(mask_starts, mask_ends):
            features[:, :, mask_start:mask_end] = mask_value

    features = features.squeeze(0)
    return features, time_mask


def time_warp(
    features: torch.Tensor,
    p: float = 0.9,
    time_warp_factor: Optional[int] = 80,
    supervision_segments: Optional[torch.Tensor] = None,
):
    if time_warp_factor is None or time_warp_factor < 1:
        return features
    assert len(features.shape) == 3, (
        "SpecAugment only supports batches of single-channel feature matrices."
    )
    features = features.clone()
    if supervision_segments is None:
        # No supervisions - apply spec augment to full feature matrices.
        for sequence_idx in range(features.size(0)):
            if random.random() > p:
                # Randomly choose whether this transform is applied
                continue
            features[sequence_idx] = time_warp_impl(
                features[sequence_idx], factor=time_warp_factor
            )
    else:
        # Supervisions provided - we will apply time warping only on the supervised areas.
        for sequence_idx, start_frame, num_frames in supervision_segments:
            if random.random() > p:
                # Randomly choose whether this transform is applied
                continue
            end_frame = start_frame + num_frames
            features[sequence_idx, start_frame:end_frame] = time_warp_impl(
                features[sequence_idx, start_frame:end_frame], factor=time_warp_factor
            )

    return features