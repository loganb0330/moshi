# Copyright (c) Kyutai
# Adapted from
# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

from collections import defaultdict
from dataclasses import dataclass, field
from itertools import chain
import logging
import typing as tp

import torch
from torch import nn

from ..modules.streaming import StreamingModule
from ..modules.transformer import create_sin_embedding


logger = logging.getLogger(__name__)
TextCondition = tp.Optional[
    str
]  # a text condition can be a string or None (if doesn't exist)


class ConditionType(tp.NamedTuple):
    """Return type for a conditioner: both a condition tensor, and a mask indicating valid positions."""

    condition: torch.Tensor
    mask: torch.Tensor


class WavCondition(tp.NamedTuple):
    """Input for waveform based conditionings.
    Wav should always be 3-dim `[B, C, T]` even before collation.
    """

    wav: torch.Tensor
    length: torch.Tensor
    sample_rate: int
    path: tp.List[tp.Optional[str]] = []
    seek_time: tp.List[tp.Optional[float]] = []

    @property
    def is_nullified(self) -> bool:
        return self.wav.shape[-1] == 1

    @staticmethod
    def dummy_wav_condition(
        batch_size: int = 1,
        duration: float = 30,
        sample_rate: int = 24000,
        channels: int = 1,
    ):
        """Create a dummy wav condition."""
        length = int(sample_rate * duration)
        return WavCondition(
            wav=torch.zeros(batch_size, channels, length),
            length=torch.full((batch_size,), length, dtype=torch.long),
            sample_rate=sample_rate,
            path=[None] * batch_size,
            seek_time=[None] * batch_size,
        )


@dataclass
class ConditionAttributes:
    """Standard class for representing the set of potential inputs to the conditioners.
    Typically, `audiocraft.data.audio_dataset.SegmentInfo` will convert
    to this class to make conditioning agnostic to the type of dataset.
    """

    text: tp.Dict[str, tp.Optional[str]] = field(default_factory=dict)
    wav: tp.Dict[str, WavCondition] = field(default_factory=dict)

    def __getitem__(self, item):
        return getattr(self, item)

    @property
    def text_attributes(self) -> tp.Iterable[str]:
        return self.text.keys()

    @property
    def wav_attributes(self) -> tp.Iterable[str]:
        return self.wav.keys()

    @staticmethod
    def condition_types() -> tp.FrozenSet[str]:
        return frozenset(["text", "wav"])

    def copy(self) -> "ConditionAttributes":
        return ConditionAttributes(dict(self.text), dict(self.wav))


Prepared = tp.TypeVar("Prepared")  # represents the prepared condition input type.


class BaseConditioner(nn.Module, tp.Generic[Prepared]):
    """Base model for all conditioner modules.

    Args:
        dim (int): internal dim of the model.
        output_dim (int): Output dim of the conditioner.
        force_linear (bool, optional): Force linear projection even when `dim == output_dim`.
        pad_empty (bool): if True, conditionings of 0 length will be padded to have length 1.
        output_bias (bool): if True, the output projection will have a bias.
        learn_padding (bool): if True, the padding value will be learnt, zero otherwise.
    """

    def __init__(
        self,
        dim: int,
        output_dim: int,
        device: tp.Union[torch.device, str],
        force_linear: bool = True,
        pad_empty: bool = True,
        output_bias: bool = False,
        learn_padding: bool = True,
    ):
        super().__init__()
        self.dim = dim
        self.output_dim = output_dim
        self.pad_empty = pad_empty
        self.device = device
        self.output_proj: nn.Module
        if force_linear or dim != output_dim:
            self.output_proj = nn.Linear(
                dim, output_dim, bias=output_bias, device=device
            )
            if output_bias:
                self.output_proj.bias.data.zero_()
        else:
            self.output_proj = nn.Identity()
        self.learnt_padding: tp.Optional[torch.Tensor]
        if learn_padding:
            self.learnt_padding = nn.Parameter(
                torch.randn(1, 1, output_dim, device=device), requires_grad=True
            )
            self.learnt_padding.data *= 0.2
        else:
            self.learnt_padding = None

    def prepare(self, *args, **kwargs) -> Prepared:
        """Should be any part of the processing that will lead to a synchronization
        point, e.g. BPE tokenization with transfer to the GPU.

        The returned value will be saved and return later when calling forward().
        """
        raise NotImplementedError()

    def _get_condition(self, inputs: Prepared) -> ConditionType:
        """Gets input that should be used as conditioning (e.g, genre, description or a waveform).
        Outputs a ConditionType, after the input data was embedded as a dense vector.

        Returns:
            ConditionType:
                - A tensor of size [B, T, dim] where B is the batch size, T is the length of the
                  output embedding and `dim` is the internal dimension of the embedding.
                - And a mask indicating where the padding tokens, of shape `[B, T]`.
        """
        raise NotImplementedError()

    def forward(self, inputs: Prepared) -> ConditionType:
        cond, mask = self._get_condition(inputs)
        B, T, C = cond.shape
        if T == 0 and self.pad_empty:
            cond = torch.zeros(B, T, C, device=cond.device, dtype=cond.dtype)
            mask = torch.zeros_like(cond[..., 0], dtype=torch.bool)

        dtype = cond.dtype
        for weight in self.output_proj.parameters():
            dtype = weight.dtype
        cond = self.output_proj(cond.to(dtype))

        maskf = mask.float()[..., None]
        if self.learnt_padding is not None:
            cond = cond * maskf + self.learnt_padding * (1 - maskf)
        else:
            cond = cond * maskf
        return ConditionType(cond, mask)


class _BaseTextConditioner(BaseConditioner[Prepared]):
    pass


class _BaseWaveformConditioner(BaseConditioner[Prepared]):
    pass


def nullify_wav(wav: WavCondition) -> WavCondition:
    """Utility function for nullifying a WavCondition object."""
    return WavCondition(
        wav=torch.zeros_like(wav.wav[:, :, :1]),
        length=torch.zeros_like(wav.length),
        sample_rate=wav.sample_rate,
        path=[None] * len(wav.path),
        seek_time=[None] * len(wav.seek_time),
    )


def dropout_condition_(
    sample: ConditionAttributes, condition_type: str, condition: str
) -> None:
    """Utility function for nullifying an attribute inside a ConditionAttributes object.
    Works in-place.
    """
    valid_conditions = ConditionAttributes.condition_types()
    if condition_type not in valid_conditions:
        raise ValueError(
            "dropout_condition got an unexpected condition type!"
            f" expected one of {valid_conditions} but got '{condition_type}'"
        )

    if condition not in getattr(sample, condition_type):
        raise ValueError(
            "dropout_condition received an unexpected condition!"
            f" expected wav={sample.wav.keys()} and text={sample.text.keys()}"
            f" but got '{condition}' of type '{condition_type}'!"
        )

    if condition_type == "wav":
        wav_cond = sample.wav[condition]
        sample.wav[condition] = nullify_wav(wav_cond)
    else:
        sample.text[condition] = None


class DropoutModule(nn.Module):
    """Base module for all dropout modules."""

    def __init__(self, seed: int = 1234):
        super().__init__()
        # rng is used to synchronize decisions across GPUs, in particular useful for
        # expansive conditioners, so that all GPUs skip or evaluate it.
        self.rng = torch.Generator()
        self.rng.manual_seed(seed)


class AttributeDropout(DropoutModule):
    """Dropout with a given probability per attribute.
    This is different from the behavior of ClassifierFreeGuidanceDropout as this allows for attributes
    to be dropped out separately. For example, "artist" can be dropped while "genre" remains.
    This is in contrast to ClassifierFreeGuidanceDropout where if "artist" is dropped "genre"
    must also be dropped.

    Args:
        dropouts (tp.Dict[str, float]): A dict mapping between attributes and dropout probability. For example:
            ...
            "genre": 0.1,
            "artist": 0.5,
            "wav": 0.25,
            ...
        active_on_eval (bool, optional): Whether the dropout is active at eval. Default to False.
        seed (int, optional): Random seed.
    """

    def __init__(
        self,
        dropouts: tp.Dict[str, float],
        active_on_eval: bool = False,
        seed: int = 1234,
    ):
        super().__init__(seed=seed)
        self.active_on_eval = active_on_eval
        self.dropouts = dropouts

    def forward(
        self, condition_attributes_batch: tp.List[ConditionAttributes]
    ) -> tp.List[ConditionAttributes]:
        """
        Args:
            condition_attributes_batch (list[ConditionAttributes]): List of condition attributes.
        Returns:
            list[ConditionAttributes]: List of condition attributes after certain attributes were set to None.
        """
        if not self.training and not self.active_on_eval:
            return condition_attributes_batch

        condition_attributes_batch = [ca.copy() for ca in condition_attributes_batch]
        for condition_attributes in condition_attributes_batch:
            # We don't know initially what type is the condition in self.dropouts, so we iterate over all types.
            for condition_type in ConditionAttributes.condition_types():
                for condition in getattr(condition_attributes, condition_type):
                    if condition in self.dropouts:
                        if (
                            torch.rand(1, generator=self.rng).item()
                            < self.dropouts[condition]
                        ):
                            dropout_condition_(
                                condition_attributes, condition_type, condition
                            )
        return condition_attributes_batch

    def __repr__(self):
        return f"AttributeDropout({dict(self.dropouts)})"


class ClassifierFreeGuidanceDropout(DropoutModule):
    """Classifier Free Guidance dropout.
    All attributes are dropped with the same probability.

    Args:
        p (float): Probability to apply condition dropout during training.
        seed (int): Random seed.
    """

    def __init__(self, p: float, seed: int = 1234):
        super().__init__(seed=seed)
        self.p = p

    def forward(
        self, samples: tp.List[ConditionAttributes]
    ) -> tp.List[ConditionAttributes]:
        """
        Args:
            samples (list[ConditionAttributes]): List of conditions.
        Returns:
            list[ConditionAttributes]: List of conditions after all attributes were set to None.
        """
        if not self.training:
            return samples

        # decide on which attributes to drop in a batched fashion
        drop = torch.rand(1, generator=self.rng).item() < self.p

        if not drop:
            return samples
        # nullify conditions of all attributes
        samples = [sample.copy() for sample in samples]
        for condition_type in ConditionAttributes.condition_types():
            for sample in samples:
                for condition in getattr(sample, condition_type):
                    dropout_condition_(sample, condition_type, condition)
        return samples

    def __repr__(self):
        return f"ClassifierFreeGuidanceDropout(p={self.p})"


class ConditionProvider(nn.Module):
    """Prepare and provide conditions given all the supported conditioners.

    Args:
        conditioners (dict): Dictionary of conditioners.
        device (torch.device or str, optional): Device for conditioners and output condition types.
    """

    def __init__(
        self,
        conditioners: tp.Dict[str, BaseConditioner],
        device: tp.Union[torch.device, str] = "cpu",
    ):
        super().__init__()
        self.device = device
        self.conditioners = nn.ModuleDict(conditioners).to(device)
        # Hack for making sure we always return the dtype that will be expected by FSDP.
        from ..optim.fsdp import get_fsdp_dtype

        self._fsdp_dtype = get_fsdp_dtype()

    @property
    def text_conditions(self):
        return [
            k
            for k, v in self.conditioners.items()
            if isinstance(v, _BaseTextConditioner)
        ]

    @property
    def wav_conditions(self):
        return [
            k
            for k, v in self.conditioners.items()
            if isinstance(v, _BaseWaveformConditioner)
        ]

    def _collate_text(
        self, samples: tp.List[ConditionAttributes]
    ) -> tp.Dict[str, tp.List[tp.Optional[str]]]:
        """Given a list of ConditionAttributes objects, compile a dictionary where the keys
        are the attributes and the values are the aggregated input per attribute.
        For example:
        Input:
        [
            ConditionAttributes(text={"genre": "Rock", "description": "A rock song with a guitar solo"}, wav=...),
            ConditionAttributes(text={"genre": "Hip-hop", "description": "A hip-hop verse"}, wav=...),
        ]
        Output:
        {
            "genre": ["Rock", "Hip-hop"],
            "description": ["A rock song with a guitar solo", "A hip-hop verse"]
        }

        Args:
            samples (list of ConditionAttributes): List of ConditionAttributes samples.
        Returns:
            dict[str, list[str, optional]]: A dictionary mapping an attribute name to text batch.
        """
        out: tp.Dict[str, tp.List[tp.Optional[str]]] = defaultdict(list)
        texts = [x.text for x in samples]
        for text in texts:
            for condition in self.text_conditions:
                out[condition].append(text[condition])
        return out

    def _collate_wavs(
        self, samples: tp.List[ConditionAttributes]
    ) -> tp.Dict[str, WavCondition]:
        """For each wav attribute, collate the wav from individual batch items.

        Args:
            samples (list of ConditionAttributes): List of ConditionAttributes samples.
        Returns:
            dict[str, WavCondition]: A dictionary mapping an attribute name to wavs.
        """
        wavs = defaultdict(list)
        lengths = defaultdict(list)
        sample_rates = defaultdict(list)
        paths = defaultdict(list)
        seek_times = defaultdict(list)
        out: tp.Dict[str, WavCondition] = {}
        for sample in samples:
            for attribute in self.wav_conditions:
                wav, length, sample_rate, path, seek_time = sample.wav[attribute]
                assert (
                    wav.dim() == 3
                ), f"Got wav with dim={wav.dim()}, but expected 3 [1, C, T]"
                assert (
                    wav.size(0) == 1
                ), f"Got wav [B, C, T] with shape={wav.shape}, but expected B == 1"
                wavs[attribute].append(wav[0])
                lengths[attribute].append(length)
                sample_rates[attribute].append(sample_rate)
                paths[attribute].extend(path)
                seek_times[attribute].extend(seek_time)

        # stack all wavs to a single tensor
        for attribute in self.wav_conditions:
            sample_rate = sample_rates[attribute][0]
            assert all(
                sr == sample_rate for sr in sample_rates[attribute]
            ), f"All sample rates in batch should match: {sample_rates[attribute]}."
            from ..data.audio_utils import stack_and_pad_audio

            stacked_wav = stack_and_pad_audio(wavs[attribute])
            out[attribute] = WavCondition(
                stacked_wav,
                torch.cat(lengths[attribute]),
                sample_rate,
                paths[attribute],
                seek_times[attribute],
            )

        return out

    def prepare(self, inputs: tp.List[ConditionAttributes]) -> tp.Dict[str, tp.Any]:
        """Match attributes/wavs with existing conditioners in self, and call `prepare` for each one.
        This should be called before starting any real GPU work to avoid synchronization points.
        This will return a dict matching conditioner names to their arbitrary prepared representations.

        Args:
            inputs (list[ConditionAttributes]): List of ConditionAttributes objects containing
                text and wav conditions.
        """
        assert all([isinstance(x, ConditionAttributes) for x in inputs]), (
            "Got unexpected types input for conditioner! should be tp.List[ConditionAttributes]",
            f" but types were {set([type(x) for x in inputs])}",
        )

        output = {}
        text = self._collate_text(inputs)
        wavs = self._collate_wavs(inputs)

        assert set(text.keys() | wavs.keys()).issubset(set(self.conditioners.keys())), (
            f"Got an unexpected attribute! Expected {self.conditioners.keys()}, ",
            f"got {text.keys(), wavs.keys()}",
        )

        missing_inputs = set(self.conditioners.keys()) - (
            set(text.keys()) | set(wavs.keys())
        )
        if missing_inputs:
            raise RuntimeError(
                f"Some conditioners did not receive an input: {missing_inputs}"
            )
        for attribute, batch in chain(text.items(), wavs.items()):
            output[attribute] = self.conditioners[attribute].prepare(batch)
        return output

    def forward(self, prepared: tp.Dict[str, tp.Any]) -> tp.Dict[str, ConditionType]:
        """Compute pairs of `(embedding, mask)` using the configured conditioners and the prepared representations.
        The output is for example:
        {
            "genre": (torch.Tensor([B, 1, D_genre]), torch.Tensor([B, 1])),
            "description": (torch.Tensor([B, T_desc, D_desc]), torch.Tensor([B, T_desc])),
            ...
        }

        Args:
            prepared (dict): Dict of prepared representations as returned by `prepare()`.
        """
        output = {}
        for attribute, inputs in prepared.items():
            condition, mask = self.conditioners[attribute](inputs)
            output[attribute] = ConditionType(condition.to(self._fsdp_dtype), mask)
        return output


class ConditionFuser(StreamingModule):
    """Condition fuser handles the logic to combine the different conditions
    to the actual model input.

    Args:
        fuse2cond (tp.Dict[str, str]): A dictionary that says how to fuse
            each condition. For example:
            {
                "prepend": ["description"],
                "sum": ["genre", "bpm"],
                "cross": ["description"],
            }
        cross_attention_pos_emb (bool, optional): Use positional embeddings in cross attention.
        cross_attention_pos_emb_scale (int): Scale for positional embeddings in cross attention if used.
    """

    FUSING_METHODS = ["sum", "prepend", "cross"]

    def __init__(
        self,
        fuse2cond: tp.Dict[str, tp.List[str]],
        cross_attention_pos_emb: bool = False,
        cross_attention_pos_emb_scale: float = 1.0,
    ):
        super().__init__()
        assert all(
            [k in self.FUSING_METHODS for k in fuse2cond.keys()]
        ), f"Got invalid fuse method, allowed methods: {self.FUSING_METHODS}"
        self.cross_attention_pos_emb = cross_attention_pos_emb
        self.cross_attention_pos_emb_scale = cross_attention_pos_emb_scale
        self.fuse2cond: tp.Dict[str, tp.List[str]] = fuse2cond
        self.cond2fuse: tp.Dict[str, str] = {}
        for fuse_method, conditions in fuse2cond.items():
            for condition in conditions:
                self.cond2fuse[condition] = fuse_method

    def forward(
        self, input: torch.Tensor, conditions: tp.Dict[str, ConditionType]
    ) -> tp.Tuple[torch.Tensor, tp.Optional[torch.Tensor]]:
        """Fuse the conditions to the provided model input.

        Args:
            input (torch.Tensor): Transformer input.
            conditions (dict[str, ConditionType]): Dict of conditions.
        Returns:
            tuple[torch.Tensor, torch.Tensor]: The first tensor is the transformer input
                after the conditions have been fused. The second output tensor is the tensor
                used for cross-attention or None if no cross attention inputs exist.
        """
        B, T, _ = input.shape

        if "offsets" in self._streaming_state:
            first_step = False
            offsets = self._streaming_state["offsets"]
        else:
            first_step = True
            offsets = torch.zeros(input.shape[0], dtype=torch.long, device=input.device)

        assert set(conditions.keys()).issubset(set(self.cond2fuse.keys())), (
            f"given conditions contain unknown attributes for fuser, "
            f"expected {self.cond2fuse.keys()}, got {conditions.keys()}"
        )
        cross_attention_output = None
        for cond_type, (cond, cond_mask) in conditions.items():
            op = self.cond2fuse[cond_type]
            if op == "sum":
                input += cond
            elif op == "prepend":
                if first_step:
                    input = torch.cat([cond, input], dim=1)
            elif op == "cross":
                if cross_attention_output is not None:
                    cross_attention_output = torch.cat(
                        [cross_attention_output, cond], dim=1
                    )
                else:
                    cross_attention_output = cond
            else:
                raise ValueError(f"unknown op ({op})")

        if self.cross_attention_pos_emb and cross_attention_output is not None:
            positions = torch.arange(
                cross_attention_output.shape[1], device=cross_attention_output.device
            ).view(1, -1, 1)
            pos_emb = create_sin_embedding(
                positions, cross_attention_output.shape[-1]
            ).to(cross_attention_output.dtype)
            cross_attention_output = (
                cross_attention_output + self.cross_attention_pos_emb_scale * pos_emb
            )

        if self._is_streaming:
            self._streaming_state["offsets"] = offsets + T

        return input, cross_attention_output