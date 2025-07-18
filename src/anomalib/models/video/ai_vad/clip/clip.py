# mypy: ignore-errors
# ruff: noqa

# Original Code
# https://github.com/openai/CLIP.
# SPDX-License-Identifier: MIT
#
# Modified
# Copyright (C) 2023-2024 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

import hashlib
import logging
import os
from typing import List, Union
from urllib.parse import urlparse

import requests
import torch
from PIL import Image
from packaging import version
from torchvision.transforms import CenterCrop, Compose, Normalize, Resize, ToTensor
from tqdm import tqdm

logger = logging.getLogger(__name__)
from .model import build_model

try:
    from torchvision.transforms import InterpolationMode

    BICUBIC = InterpolationMode.BICUBIC
except ImportError:
    BICUBIC = Image.BICUBIC


if version.parse(torch.__version__) < version.parse("1.7.1"):
    msg = "PyTorch version 1.7.1 or higher is recommended"
    logger.warn(msg)

__all__ = ["available_models", "load"]

_MODELS = {
    "RN50": "https://openaipublic.azureedge.net/clip/models/afeb0e10f9e5a86da6080e35cf09123aca3b358a0c3e3b6c78a7b63bc04b6762/RN50.pt",
    "RN101": "https://openaipublic.azureedge.net/clip/models/8fa8567bab74a42d41c5915025a8e4538c3bdbe8804a470a72f30b0d94fab599/RN101.pt",
    "RN50x4": "https://openaipublic.azureedge.net/clip/models/7e526bd135e493cef0776de27d5f42653e6b4c8bf9e0f653bb11773263205fdd/RN50x4.pt",
    "RN50x16": "https://openaipublic.azureedge.net/clip/models/52378b407f34354e150460fe41077663dd5b39c54cd0bfd2b27167a4a06ec9aa/RN50x16.pt",
    "RN50x64": "https://openaipublic.azureedge.net/clip/models/be1cfb55d75a9666199fb2206c106743da0f6468c9d327f3e0d0a543a9919d9c/RN50x64.pt",
    "ViT-B/32": "https://openaipublic.azureedge.net/clip/models/40d365715913c9da98579312b702a82c18be219cc2a73407c4526f58eba950af/ViT-B-32.pt",
    "ViT-B/16": "https://openaipublic.azureedge.net/clip/models/5806e77cd80f8b59890b7e101eabd078d9fb84e6937f9e85e4ecb61988df416f/ViT-B-16.pt",
    "ViT-L/14": "https://openaipublic.azureedge.net/clip/models/b8cca3fd41ae0c99ba7e8951adf17d267cdb84cd88be6f7c2e0eca1737a03836/ViT-L-14.pt",
    "ViT-L/14@336px": "https://openaipublic.azureedge.net/clip/models/3035c92b350959924f9f00213499208652fc7ea050643e8b385c2dac08641f02/ViT-L-14-336px.pt",
}


def _verify_checksum(file_path: str, url: str) -> bool:
    expected_sha256 = url.split("/")[-2]
    sha256_hash = hashlib.sha256()

    with open(file_path, "rb") as file:
        for chunk in iter(lambda: file.read(4096), b""):
            sha256_hash.update(chunk)

    file_hash = sha256_hash.hexdigest()

    return file_hash == expected_sha256


def _download(url: str, root: str):
    os.makedirs(root, exist_ok=True)
    filename = os.path.basename(urlparse(url).path)
    download_target = os.path.join(root, filename)

    if os.path.exists(download_target):
        if not os.path.isfile(download_target):
            raise FileExistsError(f"{download_target} exists and is not a regular file")
        if _verify_checksum(download_target, url):
            return download_target

        logger.warning("%s exists, but the checksum does not match; re-downloading the file", download_target)
        os.remove(download_target)

    response = requests.get(url, stream=True, timeout=10.0)  # Timeout is for bandit security linter
    response.raise_for_status()

    total_size = int(response.headers.get("Content-Length", 0))

    with (
        open(download_target, "wb") as file,
        tqdm(total=total_size, ncols=80, unit="iB", unit_scale=True, unit_divisor=1024) as loop,
    ):
        for chunk in response.iter_content(chunk_size=8192):
            if chunk:
                file.write(chunk)
                loop.update(len(chunk))

    if not _verify_checksum(download_target, url):
        raise RuntimeError("Model has been downloaded but the checksum does not match")

    return download_target


def _convert_image_to_rgb(image):
    return image.convert("RGB")


def _transform(n_px):
    return Compose([
        Resize(n_px, interpolation=BICUBIC),
        CenterCrop(n_px),
        _convert_image_to_rgb,
        ToTensor(),
        Normalize((0.48145466, 0.4578275, 0.40821073), (0.26862954, 0.26130258, 0.27577711)),
    ])


def available_models() -> List[str]:
    """Returns the names of available CLIP models"""
    return list(_MODELS.keys())


def load(
    name: str,
    device: Union[str, torch.device] = "cuda" if torch.cuda.is_available() else "cpu",
    jit: bool = False,
    download_root: str = None,
):
    """Load a CLIP model

    Args:
        name : str
            A model name listed by `clip.available_models()`, or the path to a model checkpoint containing the state_dict

        device : Union[str, torch.device]
            The device to put the loaded model

        jit : bool
            Whether to load the optimized JIT model or more hackable non-JIT model (default).

        download_root: str
            path to download the model files; by default, it uses "~/.cache/clip"

    Returns:
        model : torch.nn.Module
            The CLIP model

        preprocess : Callable[[PIL.Image], torch.Tensor]
            A torchvision transform that converts a PIL image into a tensor that the returned model can take as its input
    """
    if name in _MODELS:
        model_path = _download(_MODELS[name], download_root or os.path.expanduser("~/.cache/clip"))
    elif os.path.isfile(name):
        model_path = name
    else:
        raise RuntimeError(f"Model {name} not found; available models = {available_models()}")

    with open(model_path, "rb") as opened_file:
        try:
            # loading JIT archive
            model = torch.jit.load(opened_file, map_location=device if jit else "cpu").eval()  # nosec B614 # false positive
            state_dict = None
        except RuntimeError:
            # loading saved state dict
            if jit:
                msg = f"File {model_path} is not a JIT archive. Loading as a state dict instead"
                logger.warn(msg)
                jit = False
            # Weights_only is set to True
            # See mitigation details in https://github.com/open-edge-platform/anomalib/pull/2729
            # nosemgrep: trailofbits.python.pickles-in-pytorch.pickles-in-pytorch
            state_dict = torch.load(opened_file, map_location="cpu", weights_only=True)  # nosec B614

    if not jit:
        model = build_model(state_dict or model.state_dict()).to(device)
        if str(device) == "cpu":
            model.float()
        return model, _transform(model.visual.input_resolution)

    # patch the device names
    device_holder = torch.jit.trace(lambda: torch.ones([]).to(torch.device(device)), example_inputs=[])
    device_node = [n for n in device_holder.graph.findAllNodes("prim::Constant") if "Device" in repr(n)][-1]

    def patch_device(module):
        try:
            graphs = [module.graph] if hasattr(module, "graph") else []
        except RuntimeError:
            graphs = []

        if hasattr(module, "forward1"):
            graphs.append(module.forward1.graph)

        for graph in graphs:
            for node in graph.findAllNodes("prim::Constant"):
                if "value" in node.attributeNames() and str(node["value"]).startswith("cuda"):
                    node.copyAttributes(device_node)

    model.apply(patch_device)
    patch_device(model.encode_image)
    patch_device(model.encode_text)

    # patch dtype to float32 on CPU
    if str(device) == "cpu":
        float_holder = torch.jit.trace(lambda: torch.ones([]).float(), example_inputs=[])
        float_input = list(float_holder.graph.findNode("aten::to").inputs())[1]
        float_node = float_input.node()

        def patch_float(module):
            try:
                graphs = [module.graph] if hasattr(module, "graph") else []
            except RuntimeError:
                graphs = []

            if hasattr(module, "forward1"):
                graphs.append(module.forward1.graph)

            for graph in graphs:
                for node in graph.findAllNodes("aten::to"):
                    inputs = list(node.inputs())
                    for i in [1, 2]:  # dtype can be the second or third argument to aten::to()
                        if inputs[i].node()["value"] == 5:
                            inputs[i].node().copyAttributes(float_node)

        model.apply(patch_float)
        patch_float(model.encode_image)
        patch_float(model.encode_text)

        model.float()

    return model, _transform(model.input_resolution.item())
