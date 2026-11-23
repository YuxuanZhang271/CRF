"""Gastro28 online phase recognition with dual or shared visual inference."""

import copy
import gc
import hashlib

import cv2
import numpy as np
import torch
from torch import nn
from torch.nn import functional as F
from ultralytics import YOLO


# Adapted from endo_agent/temporal_inference.py; see MODEL_PROVENANCE.md.
from .catalog import PHASE_NAMES
FORMAT = "gastro28_online_phase_v1"


class CausalConv1d(nn.Module):
    def __init__(self, in_channels, out_channels, kernel_size, dilation):
        super().__init__()
        self.padding = (kernel_size - 1) * dilation
        self.conv = nn.Conv1d(in_channels, out_channels, kernel_size,
                              padding=self.padding, dilation=dilation)

    def forward(self, x):
        return self.conv(x)[:, :, :-self.padding]


class TeCNOBlock(nn.Module):
    def __init__(self, dilation, in_channels, out_channels):
        super().__init__()
        self.conv_dilated = CausalConv1d(in_channels, out_channels, 3, dilation)
        self.conv_1x1 = nn.Conv1d(out_channels, out_channels, 1)
        self.dropout = nn.Dropout()

    def forward(self, x):
        return x + self.dropout(self.conv_1x1(F.relu(self.conv_dilated(x))))


class TeCNOStage(nn.Module):
    def __init__(self, num_layers, num_f_maps, dim, num_classes):
        super().__init__()
        self.conv_1x1 = nn.Conv1d(dim, num_f_maps, 1)
        self.layers = nn.ModuleList([
            TeCNOBlock(2 ** i, num_f_maps, num_f_maps)
            for i in range(num_layers)
        ])
        self.conv_out = nn.Conv1d(num_f_maps, num_classes, 1)

    def forward(self, x):
        out = self.conv_1x1(x)
        for layer in self.layers:
            out = layer(out)
        return self.conv_out(out)


class TeCNO(nn.Module):
    def __init__(self, num_stages, num_layers, num_f_maps, dim, num_classes=7):
        super().__init__()
        self.stage1 = TeCNOStage(num_layers, num_f_maps, dim, num_classes)
        self.stages = nn.ModuleList([
            copy.deepcopy(TeCNOStage(num_layers, num_f_maps, num_classes,
                                     num_classes))
            for _ in range(num_stages - 1)
        ])

    def forward(self, x):
        out = self.stage1(x)
        outputs = [out.unsqueeze(0)]
        for stage in self.stages:
            out = stage(F.softmax(out, dim=1))
            outputs.append(out.unsqueeze(0))
        return torch.cat(outputs, dim=0)


class PhaseHead(nn.Module):
    def __init__(self, spec):
        super().__init__()
        channels = int(spec["phase_channels"])
        self.token_projection = nn.Sequential(
            nn.Linear(1080, channels), nn.LayerNorm(channels), nn.SiLU())
        self.temporal = TeCNO(int(spec["num_stages"]), int(spec["num_layers"]),
                              int(spec["num_f_maps"]), channels)
        self.receptive_field = 1 + int(spec["num_stages"]) * 2 * (
            2 ** int(spec["num_layers"]) - 1)

    def forward(self, projected):
        return self.temporal(projected.transpose(1, 2))[-1, :, :, -1]


def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class OnlinePhaseRecognizer:
    def __init__(self, checkpoint, vision_model, vision_checkpoint,
                 device="cuda", inference_mode="dual"):
        self.checkpoint = str(checkpoint)
        self.vision_checkpoint = str(vision_checkpoint)
        self.device = torch.device(device)
        spec = torch.load(checkpoint, map_location="cpu", weights_only=True)
        if not isinstance(spec, dict) or spec.get("format") != FORMAT:
            raise ValueError("Expected an exported Gastro28 online temporal .pt")
        if spec.get("temporal_model") not in ("tecno", "causal_mstcn"):
            raise ValueError("Unsupported online temporal model")
        if spec.get("feature_mode") != "global_mask" or spec.get("input_size") != 512:
            raise ValueError("Temporal checkpoint requires 512px global_mask YOLO tokens")
        expected = spec.get("seg_sha256")
        actual = file_sha256(vision_checkpoint)
        if not expected or actual.lower() != expected.lower():
            raise ValueError("Temporal checkpoint was trained with different YOLO weights")
        if vision_model is None and inference_mode != "dual":
            raise ValueError("Shared inference requires a primary segmentation model")
        if vision_model is not None and (vision_model.task != "segment" or vision_model.model.nc != 28):
            raise ValueError("Expected a 28-class YOLO segmentation checkpoint")
        self.head = PhaseHead(spec)
        self.head.load_state_dict(spec["state_dict"], strict=True)
        self.head.to(self.device).eval()
        self.yolo = None
        self.inference_mode = None
        self.history = None
        self.samples = 0
        self.context_window = self.head.receptive_field
        self.set_inference_mode(inference_mode)

    def set_context_window(self, frames):
        """Use the last N 1-Hz tokens; full keeps the trained receptive field."""
        if frames == "full":
            window = self.head.receptive_field
        elif type(frames) is int and frames in (16, 32, 64):
            window = frames
        else:
            raise ValueError("Context must be full, 16, 32, or 64 sampled frames")
        self.context_window = window
        self.reset()

    def reset(self):
        self.history = None
        self.samples = 0

    def set_inference_mode(self, mode):
        if mode not in ("dual", "shared"):
            raise ValueError("Inference mode must be dual or shared")
        if mode == self.inference_mode:
            return
        if mode == "dual":
            yolo = YOLO(self.vision_checkpoint)
            if yolo.task != "segment" or yolo.model.nc != 28:
                raise ValueError("Expected a 28-class YOLO segmentation checkpoint")
            yolo.model.fuse(verbose=False)
            yolo.model.eval()
            self.yolo = yolo
        elif self.yolo is not None:
            self.yolo = None
            gc.collect()
            if self.device.type == "cuda":
                torch.cuda.empty_cache()
        self.inference_mode = mode
        self.reset()

    @torch.inference_mode()
    def _dual_visual(self, frame_bgr):
        if frame_bgr is None or self.yolo is None:
            raise RuntimeError("Dual YOLO frame or model is missing")
        rgb = cv2.cvtColor(cv2.resize(frame_bgr, (512, 512),
                                      interpolation=cv2.INTER_LINEAR), cv2.COLOR_BGR2RGB)
        frame = torch.from_numpy(rgb.copy()).permute(2, 0, 1).unsqueeze(0)
        frame = frame.to(self.device, dtype=torch.float32).div_(255)
        predict_device = (self.device.index if self.device.index is not None else 0)
        if self.device.type != "cuda":
            predict_device = self.device.type
        options = dict(imgsz=512, conf=0.25, iou=0.7, max_det=300,
                       retina_masks=False, device=predict_device, verbose=False)
        # Current Ultralytics copies the model into its predictor. Initialize it
        # before hooking the actual inference model; hooking YOLO.model only
        # captures the first call on versions that clone hooks during setup.
        if self.yolo.predictor is None:
            self.yolo.predict(source=frame, **options)
        holder = {}

        def capture(_module, _inputs, output):
            holder["neck"] = output.detach()

        hook = self.yolo.predictor.model.model.model[22].register_forward_hook(capture)
        try:
            results = self.yolo.predict(source=frame, **options)
        finally:
            hook.remove()
        if "neck" not in holder or len(results) != 1:
            raise RuntimeError("Dual YOLO neck hook did not capture the current frame")
        return holder["neck"], results[0]

    @torch.inference_mode()
    def step(self, neck=None, result=None, frame_bgr=None):
        if self.inference_mode == "dual":
            neck, result = self._dual_visual(frame_bgr)
        if neck is None or result is None:
            raise RuntimeError("YOLO neck or segmentation result is missing")
        neck = neck.float()
        if neck.shape[0] != 1 or neck.shape[1] != 512:
            raise RuntimeError(f"Unexpected YOLO neck shape {tuple(neck.shape)}")
        global_features = neck.mean(dim=(2, 3))
        region_features = torch.zeros_like(global_features)
        class_area = neck.new_zeros((1, 28))
        class_conf = neck.new_zeros((1, 28))
        if result.masks is not None and len(result.boxes):
            masks = result.masks.data.float().to(neck.device)
            classes = result.boxes.cls.long().to(neck.device)
            confidence = result.boxes.conf.float().to(neck.device)
            valid = (classes >= 1) & (classes < 28)
            if valid.any():
                masks, classes, confidence = masks[valid], classes[valid], confidence[valid]
                areas = masks.mean(dim=(1, 2))
                class_area[0].scatter_add_(0, classes, areas * confidence)
                for cls, score in zip(classes.tolist(), confidence.tolist()):
                    class_conf[0, cls] = max(class_conf[0, cls], score)
                small = F.interpolate(masks.unsqueeze(1), size=neck.shape[-2:],
                                      mode="bilinear", align_corners=False).squeeze(1)
                regions = torch.einsum("nhw,chw->nc", small, neck[0])
                regions = regions / small.sum(dim=(1, 2)).unsqueeze(1).clamp_min(1e-6)
                weights = confidence * areas.clamp_min(1e-6)
                region_features[0] = (regions * weights.unsqueeze(1)).sum(dim=0) / weights.sum()
            class_area.clamp_(max=1)
        token = torch.cat((global_features, region_features, class_area,
                           class_conf), dim=1)
        projected = self.head.token_projection(token).unsqueeze(1)
        sequence = (torch.cat((self.history, projected), dim=1)
                    if self.history is not None else projected)
        sequence = sequence[:, -self.context_window:]
        logits = self.head(sequence)
        keep = self.context_window - 1
        self.history = sequence[:, -keep:].detach() if keep else None
        self.samples += 1
        probabilities = logits.softmax(dim=1)[0].cpu().numpy()
        order = np.argsort(probabilities)[::-1][:3]
        return [{"id": int(i) + 1, "name": PHASE_NAMES[i],
                 "prob": float(probabilities[i])} for i in order]
