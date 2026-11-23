"""Generic device policy; visibility is configured only in owned child processes."""
import os
import re

DEFAULT_DEVICE = "auto"
DEFAULT_GPU_UUID = ""
DEVICE = os.environ.get("STREAMINGCOMMAND_DEVICE", "cpu")
GPU_UUID = os.environ.get("STREAMINGCOMMAND_EXPECTED_GPU_UUID", "")
PHYSICAL_GPU = int(DEVICE.split(":")[1]) if DEVICE.startswith("cuda:") else None


def resolve_device(value="auto"):
    """Resolve auto to CPU or the first visible CUDA device and return any expected GPU UUID."""
    if value == "auto":
        import torch
        value = "cuda:0" if torch.cuda.is_available() else "cpu"
    if value == "cpu":
        return value, ""
    if value == "mps":
        raise ValueError("MPS has not been validated for this model; select cpu explicitly")
    if not value.startswith("cuda:") or not value[5:].isdigit():
        raise ValueError("device must be auto, cpu or cuda:N")
    return value, os.environ.get("STREAMINGCOMMAND_EXPECTED_GPU_UUID", "")


def pin_device():
    """Apply worker-local visibility without overriding a CUDA selector already set by the parent."""
    if DEVICE == "cpu":
        os.environ["CUDA_VISIBLE_DEVICES"] = ""
    else:
        os.environ.setdefault("CUDA_VISIBLE_DEVICES", DEVICE.split(":")[-1])
    return GPU_UUID


def worker_visibility(device, environment):
    """Select one CUDA ordinal within the caller's visible device list."""
    if device == "cpu":
        return ""
    index = int(device.split(":")[1])
    inherited = environment.get("CUDA_VISIBLE_DEVICES")
    if inherited is None:
        return str(index)
    visible = [item.strip() for item in inherited.split(",")]
    if index >= len(visible) or any(
        not re.fullmatch(r"(?:[0-9]+|(?:GPU|MIG)-[a-fA-F0-9/-]+)", item)
        for item in visible[:index + 1]
    ):
        raise ValueError(f"{device} is outside the caller's CUDA_VISIBLE_DEVICES")
    if len(set(visible[:index + 1])) != index + 1:
        raise ValueError("CUDA_VISIBLE_DEVICES contains repeated device identifiers")
    return visible[index]
