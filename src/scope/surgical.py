"""Gastro28 segmentation; temporal inference has its own independent YOLO."""

from .catalog import SURGICAL_CATEGORIES, VISION_SHA256
from .yolo import Detection, Detector


class SurgicalDetector(Detector):
    def __init__(self, model_path):
        # Validate before loading; another 28-class model is not interchangeable.
        from .temporal import file_sha256

        if file_sha256(model_path) != VISION_SHA256:
            raise ValueError("Gastro28 segmentation weights do not match the paired model manifest.")
        super().__init__(model_path)
        if self.model.task != "segment" or self.model.model.nc != 28:
            raise ValueError("Expected the paired 28-class Gastro28 segmentation model.")

    def detect(self, frame, category):
        # Always infer all classes. Selection only filters drawing/target lookup.
        result = self.model.predict(frame, imgsz=512, conf=.25, iou=.7,
                                    max_det=300, retina_masks=False, verbose=False)[0]
        if result.boxes is None:
            return []
        boxes = result.boxes.cpu()
        polygons = result.masks.xy if result.masks is not None else []
        return [Detection(int(cid), float(conf), tuple(map(int, bounds)),
                          tuple(tuple(map(int, point)) for point in polygons[i])
                          if i < len(polygons) else ())
                for i, (cid, conf, bounds) in enumerate(zip(
                    boxes.cls.tolist(), boxes.conf.tolist(), boxes.xyxy.tolist()))
                if int(cid) in SURGICAL_CATEGORIES]


def load_phase(vision_checkpoint, temporal_checkpoint):
    import torch
    from .temporal import OnlinePhaseRecognizer

    return OnlinePhaseRecognizer(temporal_checkpoint, None, vision_checkpoint,
                                 device="cuda" if torch.cuda.is_available() else "cpu",
                                 inference_mode="dual")
