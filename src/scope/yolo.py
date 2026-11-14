"""Detect and annotate the SCOPE categories using the bundled YOLO model."""

from dataclasses import dataclass
from pathlib import Path

import cv2


MODEL_PATH = Path(__file__).with_name("yolov8n.pt")
CATEGORIES = {0: "person", 39: "bottle", 41: "cup", 46: "banana", 56: "chair"}
COLORS = {
    0: (242, 189, 77),
    39: (143, 212, 74),
    41: (240, 156, 192),
    46: (81, 212, 252),
    56: (92, 155, 250),
}
LOCK_RADIUS = 12


@dataclass(frozen=True)
class Detection:
    class_id: int
    confidence: float
    bounds: tuple[int, int, int, int]

    @property
    def center(self):
        x1, y1, x2, y2 = self.bounds
        return (x1 + x2) // 2, (y1 + y2) // 2


@dataclass(frozen=True)
class Observation:
    frame: int
    captured_at: float
    revision: int
    category: int | None
    width: int
    height: int
    valid: bool
    centered: bool
    dx: float
    dy: float


def target_error(target, shape):
    height, width = shape[:2]
    if target is None:
        return 0, 0, False
    x, y = target.center
    dx, dy = x - width // 2, y - height // 2
    return dx, dy, abs(dx) <= LOCK_RADIUS and abs(dy) <= LOCK_RADIUS


class Detector:
    def __init__(self, model_path=MODEL_PATH):
        from ultralytics import YOLO

        model_path = Path(model_path)
        if not model_path.is_file():
            raise FileNotFoundError(f"YOLO model not found: {model_path}")
        self.model = YOLO(str(model_path))

    def detect(self, frame, category):
        classes = list(CATEGORIES) if category is None else [category]
        result = self.model.predict(frame, imgsz=640, conf=0.25, classes=classes, verbose=False)[0]
        if result.boxes is None or not len(result.boxes):
            return []
        boxes = result.boxes.cpu()
        return [
            Detection(int(class_id), confidence, tuple(map(int, bounds)))
            for class_id, confidence, bounds in zip(
                boxes.cls.tolist(), boxes.conf.tolist(), boxes.xyxy.tolist()
            )
            if int(class_id) in classes
        ]


def draw_detections(frame, detections, category):
    """Draw all visible detections and return the strongest selected target."""
    font = cv2.FONT_HERSHEY_SIMPLEX
    for detection in detections:
        x1, y1, x2, y2 = detection.bounds
        color = COLORS[detection.class_id]
        label = f"{CATEGORIES[detection.class_id]} {detection.confidence:.2f}"
        (width, height), baseline = cv2.getTextSize(label, font, 0.6, 2)
        top = max(0, y1 - height - baseline - 6)
        cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)
        cv2.rectangle(frame, (x1, top), (x1 + width + 8, top + height + baseline + 6), color, -1)
        cv2.putText(frame, label, (x1 + 4, top + height + 3), font, 0.6, (0, 0, 0), 2, cv2.LINE_AA)

    if category is None:
        return None
    height, width = frame.shape[:2]
    target = max((item for item in detections if item.class_id == category
                  and 0 <= item.center[0] < width and 0 <= item.center[1] < height),
                 key=lambda item: item.confidence, default=None)
    center = width // 2, height // 2
    x, y = center
    color = (95, 95, 255)
    if target is not None:
        dx, dy, centered = target_error(target, frame.shape)
        if centered:
            color = (92, 214, 99)
        cv2.line(frame, center, target.center, color, 2)
        cv2.circle(frame, target.center, 5, color, -1)
        cv2.putText(frame, f"dx={dx:+d}  dy={dy:+d}", (x + 16, max(16, y - 16)),
                    font, 0.6, color, 2, cv2.LINE_AA)
    cv2.rectangle(frame, (x - LOCK_RADIUS, y - LOCK_RADIUS),
                  (x + LOCK_RADIUS, y + LOCK_RADIUS), color, 2)
    return target
