"""LTX / ComfyUI Resolution Selector presets."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AspectRatioSpec:
    id: str
    comfy_value: str
    short_label: str

    @property
    def menu_label(self) -> str:
        return self.short_label


@dataclass(frozen=True)
class MegapixelSpec:
    id: str
    value: float

    @property
    def label(self) -> str:
        return f"{self.value:g} MP"


# ComfyUI Resolution Selector aspect_ratio values
ASPECT_RATIO_ORDER: list[str] = [
    "1:1",
    "2:3",
    "3:2",
    "3:4",
    "4:3",
    "9:16",
    "16:9",
]

ASPECT_RATIOS: dict[str, AspectRatioSpec] = {
    "1:1": AspectRatioSpec("1:1", "1:1 (Square)", "1:1 Square"),
    "2:3": AspectRatioSpec("2:3", "2:3 (Portrait Photo)", "2:3 Portrait"),
    "3:2": AspectRatioSpec("3:2", "3:2 (Photo)", "3:2 Photo"),
    "3:4": AspectRatioSpec("3:4", "3:4 (Portrait Standard)", "3:4 Portrait"),
    "4:3": AspectRatioSpec("4:3", "4:3 (Standard)", "4:3 Standard"),
    "9:16": AspectRatioSpec("9:16", "9:16 (Portrait Widescreen)", "9:16 Portrait"),
    "16:9": AspectRatioSpec("16:9", "16:9 (Widescreen)", "16:9 Widescreen"),
}

# 16:9 reference output sizes (for menu hints only)
MEGAPIXEL_16_9_OUTPUT: dict[str, str] = {
    "0.2": "608×352",
    "0.3": "736×416",
    "0.4": "864×480",
    "0.5": "960×544",
    "0.6": "1056×608",
    "0.7": "1152×640",
    "0.8": "1216×672",
    "0.9": "1280×736",
    "1.0": "1376×768",
    "1.2": "1504×832",
    "1.5": "1664×928",
    "1.8": "1824×1024",
    "2.0": "1920×1088",
}

MEGAPIXEL_ORDER: list[str] = [
    "0.2", "0.3", "0.4", "0.5", "0.6", "0.7", "0.8", "0.9",
    "1.0", "1.2", "1.5", "1.8", "2.0",
]

MEGAPIXELS: dict[str, MegapixelSpec] = {
    rid: MegapixelSpec(rid, float(rid))
    for rid in MEGAPIXEL_ORDER
}

DEFAULT_ASPECT_RATIO_ID = "16:9"
DEFAULT_MEGAPIXEL_ID = "1.0"


def get_aspect_ratio(aspect_ratio_id: str) -> AspectRatioSpec | None:
    return ASPECT_RATIOS.get(aspect_ratio_id)


def get_megapixel(megapixel_id: str) -> MegapixelSpec | None:
    return MEGAPIXELS.get(megapixel_id)


def parse_aspect_ratio_choice(text: str) -> AspectRatioSpec | None:
    raw = (text or "").strip()
    if raw.isdigit():
        idx = int(raw)
        if 1 <= idx <= len(ASPECT_RATIO_ORDER):
            return ASPECT_RATIOS[ASPECT_RATIO_ORDER[idx - 1]]
    if raw in ASPECT_RATIOS:
        return ASPECT_RATIOS[raw]
    lower = raw.lower()
    for spec in ASPECT_RATIOS.values():
        if lower in {spec.id.lower(), spec.short_label.lower(), spec.comfy_value.lower()}:
            return spec
    return None


def parse_megapixel_choice(text: str) -> MegapixelSpec | None:
    raw = (text or "").strip().lower().replace("mp", "").strip()
    if raw.isdigit():
        idx = int(raw)
        if 1 <= idx <= len(MEGAPIXEL_ORDER):
            return MEGAPIXELS[MEGAPIXEL_ORDER[idx - 1]]
    if raw in MEGAPIXELS:
        return MEGAPIXELS[raw]
    for spec in MEGAPIXELS.values():
        if raw == spec.id or raw == f"{spec.value:g}":
            return spec
    return None


def format_aspect_ratios_menu() -> str:
    lines = ["<b>Choose aspect ratio</b>", ""]
    for i, rid in enumerate(ASPECT_RATIO_ORDER, start=1):
        spec = ASPECT_RATIOS[rid]
        lines.append(f"{i}. {spec.menu_label}")
    return "\n".join(lines)


def format_megapixels_menu(aspect_ratio_id: str) -> str:
    ar = get_aspect_ratio(aspect_ratio_id)
    title = ar.menu_label if ar else aspect_ratio_id
    lines = [f"<b>Choose megapixels</b> ({title})", ""]
    for i, mid in enumerate(MEGAPIXEL_ORDER, start=1):
        spec = MEGAPIXELS[mid]
        hint = ""
        if aspect_ratio_id == "16:9" and mid in MEGAPIXEL_16_9_OUTPUT:
            hint = f" → {MEGAPIXEL_16_9_OUTPUT[mid]}"
        lines.append(f"{i}. {spec.label}{hint}")
    return "\n".join(lines)
