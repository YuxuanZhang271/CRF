"""Perception metadata shared by workers, HTTP clients and voice dispatch."""

import colorsys
from pathlib import Path

from .yolo import CATEGORIES, COLORS

MODEL_DIR = Path(__file__).resolve().parents[2] / "models" / "scope"
SURGICAL_MODEL_PATH = MODEL_DIR / "gastro28_yolo26m_seg.pt"
TEMPORAL_MODEL_PATH = MODEL_DIR / "YOLO26m_CausalMSTCN_fold0.pt"
TIPS_PATH = Path(__file__).with_name("stage_tips.json")
VISION_SHA256 = "4061864912078b3ac3d0e262389892aec6b31ae06b4a8f1e191754fea3c667cb"

# IDs match the paired checkpoint. Background (0) is never selectable.
SURGICAL_CATEGORIES = dict(enumerate((
    "Common hepatic artery", "Proper hepatic artery", "Gastroduodenal artery",
    "Left gastric artery", "Right gastric artery", "Left gastric vein",
    "Right gastric vein", "Pancreas", "Duodenal stump", "Liver", "Gallbladder",
    "Curved grasper", "Straight grasper", "Irrigation tube", "Harmonic scalpel",
    "Stapler", "Hem-o-lok", "Gauze", "Endoscopic scissor", "Needle holder",
    "Needle", "Suture", "Falciform ligament", "Stomach", "Hemoloc applier",
    "Blood pool", "Ligasure",
), start=1))
SURGICAL_COLORS = {
    cid: tuple(reversed(tuple(round(c * 255) for c in
        colorsys.hsv_to_rgb((cid * .61803398875) % 1, .6, .95))))
    for cid in SURGICAL_CATEGORIES
}
PHASE_NAMES = (
    "Duodenum transection", "Hepatic artery exposure", "Ligament incision",
    "Right gastric division", "Hepatic artery dissection",
    "Common hepatic dissection", "Coronary vein division",
)
# Supplied stage-duration statistics, in checkpoint stage order (seconds).
PHASE_DURATIONS_S = (40.8, 568.8, 101.4, 583.8, 490.2, 830.4, 216.6)


def categories_for(mode):
    return SURGICAL_CATEGORIES if mode == "full" else CATEGORIES


def colors_for(mode):
    return SURGICAL_COLORS if mode == "full" else COLORS


def category_metadata(mode):
    colors = colors_for(mode)
    return [{"id": cid, "label": name[0].upper() + name[1:],
             "color": "#%02x%02x%02x" % colors[cid][::-1]}
            for cid, name in categories_for(mode).items()]
