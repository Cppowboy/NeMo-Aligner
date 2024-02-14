import argparse
import os
from collections import deque
from pathlib import Path

import clip  # pip install git+https://github.com/openai/CLIP.git
import matplotlib.pyplot as plt
import numpy as np
import requests
import torch
import torch.nn.functional as F
from packaging import version
from PIL import Image
from torch import nn
from torchvision.transforms import CenterCrop, Compose, Normalize, Resize, ToTensor
from tqdm import tqdm
from transformers import AutoModel, AutoProcessor

from nemo.collections.multimodal.data.clip.clip_dataset import get_preprocess_fns
from nemo.collections.multimodal.models.vision_language_foundation.clip.megatron_clip_models import (
    CLIPTextTransformer,
    CLIPVisionTransformer,
    MegatronCLIPModel,
)
from nemo.collections.multimodal.parts.utils import setup_trainer_and_model_for_inference
from nemo.collections.nlp.modules.common.megatron.module import Float16Module, MegatronModule

try:
    from apex.transformer.enums import AttnMaskType
    from apex.transformer.pipeline_parallel.utils import _reconfigure_microbatch_calculator, get_num_microbatches

    HAVE_APEX = True
except (ImportError, ModuleNotFoundError):
    HAVE_APEX = False

try:
    from megatron.core import parallel_state
    from megatron.core.pipeline_parallel.schedules import get_forward_backward_func

    HAVE_MEGATRON_CORE = True

except (ImportError, ModuleNotFoundError):

    HAVE_MEGATRON_CORE = False

try:
    from torchvision.transforms import InterpolationMode

    BICUBIC = InterpolationMode.BICUBIC
except ImportError:
    BICUBIC = Image.BICUBIC

torch.backends.cuda.matmul.allow_tf32 = True

OPENAI_DATASET_MEAN = (0.48145466, 0.4578275, 0.40821073)
OPENAI_DATASET_STD = (0.26862954, 0.26130258, 0.27577711)


class PickscoreRewardModel(MegatronModule):
    """CLIP-Based Model"""

    def __init__(self, model_cfg, model_parallel_config, padded_vocab_size, pre_process=True, post_process=True):
        super(PickscoreRewardModel, self).__init__()

        self.config = model_parallel_config
        self.pre_process = pre_process
        self.post_process = post_process
        self.vision_encoder = CLIPVisionTransformer(
            model_cfg.vision, model_parallel_config, pre_process=self.pre_process, post_process=self.post_process,
        )
        self.text_encoder = CLIPTextTransformer(
            model_cfg.text,
            model_parallel_config,
            padded_vocab_size,
            pre_process=self.pre_process,
            post_process=self.post_process,
        )

        self.logit_scale = torch.nn.Parameter(torch.ones([]) * np.log(1 / 0.07))

    def set_input_tensor(self, input_tensor):
        """See megatron.model.transformer.set_input_tensor()"""
        # TODO (yuya): fix this
        pass

    def get_reward(self, images, captions):

        text_features = self.text_encoder(captions)
        image_features = self.vision_encoder(images)

        rewards = (
            self.logit_scale.exp()
            * torch.matmul(F.normalize(image_features, dim=-1), F.normalize(text_features, dim=-1).t()).diag()
        )

        return rewards


class MegatronCLIPRewardModel(MegatronCLIPModel):
    def __init__(self, cfg, trainer):
        super().__init__(cfg, trainer)
        self.differentiable_preprocess = self.diff_preprocess()

    def diff_preprocess(self):

        return Compose(
            [
                Resize(224, interpolation=BICUBIC, antialias=True),
                CenterCrop(224),
                self.rescale,
                Normalize(OPENAI_DATASET_MEAN, OPENAI_DATASET_STD),
            ]
        )

    def rescale(self, image):
        return image * 0.00392156862745098

    def preprocess(self, images, captions):

        _, text_transform = get_preprocess_fns(self.cfg, tokenizer=self.tokenizer, is_train=False)

        images = (
            torch.stack([self.differentiable_preprocess(img.permute(2, 0, 1)) for img in images])
            .to(torch.cuda.current_device())
            .float()
        )

        captions_list = [text_transform(captions[i]) for i in range(images.shape[0])]

        captions = torch.stack(captions_list).cuda()

        return images, captions

    def get_reward(self, images, captions):
        images, captions = self.preprocess(images, captions)
        return self.model.get_reward(images, captions)

    def model_provider_func(self, pre_process, post_process):
        """Model depends on pipeline paralellism."""
        model = PickscoreRewardModel(
            model_cfg=self.cfg,
            model_parallel_config=self.model_parallel_config,
            padded_vocab_size=self.padded_vocab_size,
            pre_process=pre_process,
            post_process=post_process,
        )

        return model


def get_reward_model(cfg, mbs, gbs):
    def model_cfg_modifier(model_cfg):
        model_cfg.precision = cfg.trainer.precision
        model_cfg.vision.precision = cfg.trainer.precision
        model_cfg.text.precision = cfg.trainer.precision
        if cfg.trainer.precision != "bf16":
            model_cfg.megatron_amp_O2 = False
        model_cfg.sequence_parallel = False
        model_cfg.activations_checkpoint_granularity = None
        model_cfg.activations_checkpoint_method = None
        model_cfg.global_batch_size = gbs
        model_cfg.micro_batch_size = mbs

    _, model = setup_trainer_and_model_for_inference(
        model_provider=MegatronCLIPRewardModel, cfg=cfg, model_cfg_modifier=model_cfg_modifier,
    )
    return model