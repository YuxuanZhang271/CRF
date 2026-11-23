"""Demo LLM choices. Metadata only: importing this never initializes CUDA."""
from pathlib import Path
import re

if __package__:
    from .device_policy import DEFAULT_GPU_UUID
else:
    from device_policy import DEFAULT_GPU_UUID

from .paths import model_path
from ..config import CLARIFICATION_MODELS, HYBRID_MODEL, PUBLISHED_MODEL
ROOT = Path(__file__).resolve().parent.parent
GPU_UUID = DEFAULT_GPU_UUID
CURRENT_MODEL = "clarification-current"
NEW_MODEL = "clarification-new"
LATEST_MODEL = "clarification-latest"
DEFAULT_MODEL = NEW_MODEL
LEGACY_CHECKPOINT = model_path("legacy")
CLARIFICATION_CHECKPOINT = model_path("clarification-back")
CURRENT_CLARIFICATION_CHECKPOINT = model_path("clarification-current")
SINGLE_MODEL = "clarification-single"
SINGLE_CLARIFICATION_CHECKPOINT = model_path("clarification-single")
NEW_CLARIFICATION_CHECKPOINT = model_path("clarification-new")
LATEST_CLARIFICATION_CHECKPOINT = model_path("clarification-latest")
H1_MODELS = frozenset(CLARIFICATION_MODELS)
SINGLE_MODELS = frozenset((SINGLE_MODEL, *H1_MODELS))
MODEL_SPECS = {
    HYBRID_MODEL: {
        "id": HYBRID_MODEL, "label": "Qwen 0.8B · 新命令表澄清（step1392）",
        "checkpoint": str(model_path("clarification-hybrid")), "clarification": True,
        "session_memory": False, "max_history_depth": 1,
        "removed_intents": ["rotate"],
        "description": "支持分开的画面缩放、镜体进退、喷水和喷雾，以及最近一条历史。",
    },
    CURRENT_MODEL: {
        "id": CURRENT_MODEL, "label": "Qwen 0.8B · 语义澄清与会话记忆（11/06，实验）",
        "checkpoint": str(CURRENT_CLARIFICATION_CHECKPOINT), "clarification": True,
        "description": "语义识别 back 类请求；支持自然回答、取消、改做其他动作及继续澄清。约定保留到会话结束。",
    },
    "clarification-back": {
        "id": "clarification-back", "label": "Qwen 0.8B · 仅 back 澄清（11/03）",
        "checkpoint": str(CLARIFICATION_CHECKPOINT), "clarification": True,
        "description": "仅单独 back 触发澄清；下一句语音确认含义，约定保留到会话结束。",
        "archived": True,
    },
    "legacy": {
        "id": "legacy", "label": "Qwen 0.8B · 原命令模型",
        "checkpoint": str(LEGACY_CHECKPOINT), "clarification": False,
        "description": "保留原 demo 的单 r16 命令解析模型。",
    },
    SINGLE_MODEL: {
        "id": SINGLE_MODEL, "label": "Qwen 0.8B · 单模型双动作澄清（11/08，实验）",
        "checkpoint": str(SINGLE_CLARIFICATION_CHECKPOINT), "clarification": True,
        "session_memory": False,
        "description": "同一个模型解析命令并提出两个动作；下一句回答或新命令直接解析，不保存含义约定。",
    },
    NEW_MODEL: {
        "id": NEW_MODEL, "label": "Qwen 0.8B · 新基线澄清（11/13，默认）",
        "checkpoint": str(NEW_CLARIFICATION_CHECKPOINT), "clarification": True,
        "session_memory": False, "max_history_depth": 1,
        "removed_intents": ["rotate"],
        "description": "使用新命令基线和最近一条历史；可回答澄清或提出新命令，不保存含义约定。",
    },
    LATEST_MODEL: {
        "id": LATEST_MODEL, "label": "Qwen 0.8B · 粤英强化澄清（11/14，step936）",
        "checkpoint": str(LATEST_CLARIFICATION_CHECKPOINT), "clarification": True,
        "session_memory": False, "max_history_depth": 1,
        "removed_intents": ["rotate"],
        "description": "整套 epoch1／step936；修补粤英 reset、track 等表达，使用新候选和最近一条历史。",
    },
}

# Preserve the older choices; the back-only package remains callable by CLI.
MODEL_SPECS = {key: MODEL_SPECS[key] for key in CLARIFICATION_MODELS}


def model_candidates(candidates, model):
    """Apply the selected model's candidate schema, without rewriting speech."""
    result = {slot: list(values) for slot, values in candidates.items()}
    if not result["P"] and model not in H1_MODELS:
        raise ValueError("scrub nurse 无方向配置请选 clarification-latest 或 clarification-new")
    if model in H1_MODELS:
        result["I"] = [value for value in result["I"] if value != "rotate"]
        if not result["I"]:
            raise ValueError("新模型已移除独立 rotate 候选；请使用 self rotate")
    return result


def model_history(history, model):
    """Limit current models to H1 and normalize legacy rotate history only for the clarification
    checkpoints.
    """
    result = dict(history)
    if model in H1_MODELS or model == PUBLISHED_MODEL:
        result = {"H1": result["H1"]} if result.get("H1") else {}
        if model in H1_MODELS and result:
            result["H1"] = re.sub(r"(^|;)I=rotate(?=;|$)", r"\1I=self rotate", result["H1"])
    return result


def model_specs(checkpoint=None):
    """Return independent copies of the model metadata so runtime selection can attach resolved paths."""
    specs = {key: dict(value) for key, value in MODEL_SPECS.items()}
    if checkpoint is not None:
        specs["legacy"]["checkpoint"] = str(Path(checkpoint).resolve())
        specs["legacy"]["label"] = "Qwen 0.8B · 指定命令模型"
    return specs


def clarification_input(utterance):
    """Ignore ASR sentence-ending punctuation only for the single word back.

    Original transcripts stay untouched. Phrases such as go back, back up or
    quoted discussion still take the ordinary parser path.
    """
    return "back" if re.fullmatch(r"back[.!?。！？]*", utterance.strip(), re.IGNORECASE) else utterance
