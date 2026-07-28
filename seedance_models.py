"""Seedance 2.0 API: generation modes and model catalog."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

ModeId = Literal["t2v", "i2v", "r2v"]

MODE_ORDER: list[tuple[ModeId, str]] = [
    ("t2v", "Text to Video"),
    ("i2v", "Image to Video"),
    ("r2v", "Reference to Video"),
]


@dataclass(frozen=True)
class ModeSpec:
    id: ModeId
    title: str
    generation_type: str
    requires_image: bool = False
    allows_end_image: bool = False
    max_images: int = 0
    allows_video: bool = False
    max_videos: int = 0
    allows_audio: bool = False
    max_audios: int = 0


MODES: dict[ModeId, ModeSpec] = {
    "t2v": ModeSpec(
        id="t2v",
        title="Text to Video",
        generation_type="text-to-video",
    ),
    "i2v": ModeSpec(
        id="i2v",
        title="Image to Video",
        generation_type="image-to-video",
        requires_image=True,
        allows_end_image=True,
        max_images=2,
    ),
    "r2v": ModeSpec(
        id="r2v",
        title="Reference to Video",
        generation_type="reference-to-video",
        max_images=9,
        allows_video=True,
        max_videos=3,
        allows_audio=True,
        max_audios=3,
    ),
}


@dataclass(frozen=True)
class ModelSpec:
    id: str
    title: str
    api_model: str
    default_input: dict[str, Any] = field(default_factory=dict)
    timeout_sec: int = 900


MODELS: dict[str, ModelSpec] = {
    "sd20": ModelSpec(
        id="sd20",
        title="Seedance 2.0",
        api_model="seedance-2-0",
        default_input={
            "duration": 5,
            "aspect_ratio": "16:9",
            "resolution": "720p",
            "generate_audio": True,
            "watermark": False,
            "seed": -1,
        },
    ),
    "sd20_fast": ModelSpec(
        id="sd20_fast",
        title="Seedance 2.0 Fast",
        api_model="seedance-2-0-fast",
        default_input={
            "duration": 5,
            "aspect_ratio": "16:9",
            "resolution": "720p",
            "generate_audio": True,
            "watermark": False,
            "seed": -1,
        },
    ),
    "sd20_mini": ModelSpec(
        id="sd20_mini",
        title="Seedance 2.0 Mini",
        api_model="seedance-2-0-mini",
        default_input={
            "duration": 5,
            "aspect_ratio": "16:9",
            "resolution": "720p",
            "generate_audio": True,
            "watermark": False,
            "seed": -1,
        },
    ),
}

MODEL_ORDER = ["sd20", "sd20_fast", "sd20_mini"]
DEFAULT_MODEL_ID = "sd20"


def get_mode(mode_id: str) -> ModeSpec | None:
    return MODES.get(mode_id)  # type: ignore[arg-type]


def get_model(model_id: str) -> ModelSpec | None:
    return MODELS.get(model_id)


def mode_label(mode_id: ModeId) -> str:
    for mid, label in MODE_ORDER:
        if mid == mode_id:
            return label
    return mode_id


def format_mode_instructions(mode_id: ModeId) -> str:
    if mode_id == "t2v":
        return (
            "<b>Text to Video</b>\n\n"
            "Send a text prompt describing the video you want to create."
        )
    if mode_id == "i2v":
        return (
            "<b>Image to Video</b>\n\n"
            "Send a text prompt and 1 image (or 2 images for first and last frame).\n"
            "You can send them in any order. Press Done when ready."
        )
    return (
        "<b>Reference to Video</b>\n\n"
        "Send a text prompt and at least one reference image or video.\n"
        "You may also add more images (up to 9), videos (up to 3), and audio (up to 3).\n"
        "Press Done when ready."
    )


def format_modes_menu() -> str:
    lines = [
        "<b>Choose generation mode</b>",
        "",
        "1. Text to Video",
        "2. Image to Video",
        "3. Reference to Video",
    ]
    return "\n".join(lines)


def format_models_menu() -> str:
    lines = [
        "<b>Model</b>",
        "",
        "Choose a model — send the <b>number</b>:",
        "",
    ]
    for i, model_id in enumerate(MODEL_ORDER, start=1):
        m = MODELS[model_id]
        lines.append(f"{i}. {m.title}")
    return "\n".join(lines)


def parse_mode_choice(text: str) -> ModeId | None:
    raw = (text or "").strip()
    if raw.isdigit():
        idx = int(raw)
        if 1 <= idx <= len(MODE_ORDER):
            return MODE_ORDER[idx - 1][0]
    lower = raw.lower()
    aliases = {
        "text to video": "t2v",
        "t2v": "t2v",
        "text-to-video": "t2v",
        "image to video": "i2v",
        "i2v": "i2v",
        "image-to-video": "i2v",
        "reference to video": "r2v",
        "r2v": "r2v",
        "reference-to-video": "r2v",
    }
    return aliases.get(lower)  # type: ignore[return-value]


def parse_model_choice(text: str) -> ModelSpec | None:
    raw = (text or "").strip()
    if raw.isdigit():
        idx = int(raw)
        if 1 <= idx <= len(MODEL_ORDER):
            return MODELS[MODEL_ORDER[idx - 1]]
    lower = raw.lower()
    for m in MODELS.values():
        if m.title.lower() == lower or m.id == lower:
            return m
    return None


def build_input(
    mode: ModeSpec,
    model: ModelSpec,
    prompt: str,
    *,
    image_urls: list[str] | None = None,
    video_urls: list[str] | None = None,
    audio_urls: list[str] | None = None,
) -> dict[str, Any]:
    image_urls = list(image_urls or [])
    video_urls = list(video_urls or [])
    audio_urls = list(audio_urls or [])

    if mode.id == "i2v":
        if not image_urls:
            raise ValueError("Image to Video requires at least one image.")
        if len(image_urls) > 2:
            raise ValueError("Image to Video accepts at most 2 images.")

    if mode.id == "r2v":
        if not image_urls and not video_urls:
            raise ValueError("Reference to Video requires at least one image or video.")
        if audio_urls and not image_urls and not video_urls:
            raise ValueError("Audio reference requires at least one image or video.")

    payload: dict[str, Any] = {
        **model.default_input,
        "prompt": prompt,
        "generation_type": mode.generation_type,
    }

    if image_urls:
        payload["image_urls"] = image_urls
    if video_urls:
        payload["video_urls"] = video_urls
    if audio_urls:
        payload["audio_urls"] = audio_urls

    if mode.id == "i2v":
        payload["aspect_ratio"] = "adaptive"

    return payload
