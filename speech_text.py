"""Pull quoted / 'says:' dialogue out of a mixed-language scenario."""

from __future__ import annotations

import re

VOICES = {
    "ru": "ru-RU-SvetlanaNeural",
    "vi": "vi-VN-HoaiMyNeural",
    "en": "en-US-AriaNeural",
}
MALE_VOICES = {
    "ru": "ru-RU-DmitryNeural",
    "vi": "vi-VN-NamMinhNeural",
    "en": "en-US-GuyNeural",
}

_QUOTE_RES = (
    re.compile(r"«([^»]+)»"),
    re.compile(r"“([^”]+)”"),
    re.compile(r"„([^”]+)”"),
    re.compile(r"\"([^\"]+)\""),
    re.compile(r"'([^']{8,})'"),
)
_SPEAK_VERBS = (
    r"(?:говорит|скажет|говоря|произносит|озвуч(?:ка|ить|ивает)?|"
    r"says?|saying|said|speaks?|speaking|voiceover)"
)
_SAYS_RE = re.compile(
    rf"{_SPEAK_VERBS}\s*[:\-–]\s*(.+)",
    re.I | re.S,
)
_FROM_SPEAK_RE = re.compile(
    rf"{_SPEAK_VERBS}\s*[:\-–,]?.*$",
    re.I | re.S,
)
_MALE_RE = re.compile(
    r"(?:мужчин[аыеу]|парн(?:ень|я|ю)|мужск\w*|ведущи+й|"
    r"\bman\b|\bmale\b|\bguy\b|\bboy\b|\bhe\b|\bон\b)",
    re.I,
)
_FEMALE_RE = re.compile(
    r"(?:женщин[аыеу]|девушк[аиу]|ведущая|\bона\b|"
    r"\bwoman\b|\bfemale\b|\bgirl\b|\blady\b|\bshe\b)",
    re.I,
)
_CYRILLIC_RE = re.compile(r"[а-яё]", re.I)
_VI_RE = re.compile(
    r"[ăâêôơưáàảãạắằẳẵặấầẩẫậéèẻẽẹíìỉĩịóòỏõọốồổỗộớờởỡợúùủũụứừửữựýỳỷỹỵđ]",
    re.I,
)
_WRAP_QUOTES = "«»\"'“”„"


def detect_language(text: str) -> str:
    raw = text or ""
    if _VI_RE.search(raw):
        return "vi"
    if _CYRILLIC_RE.search(raw):
        return "ru"
    return "en"


def pick_voice(language: str, text: str = "") -> str:
    lang = language if language in VOICES else "en"
    raw = text or ""
    male = bool(_MALE_RE.search(raw))
    female = bool(_FEMALE_RE.search(raw))
    if male and not female:
        return MALE_VOICES.get(lang, MALE_VOICES["en"])
    return VOICES[lang]


def visual_prompt_without_speech(video_prompt: str, speech: str = "") -> str:
    visual = (video_prompt or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if speech:
        visual = visual.replace(speech, " ")
    visual = _FROM_SPEAK_RE.sub(" ", visual)
    visual = re.sub(r"[\s\"'«»“”„]+", " ", visual).strip(" :-")
    return visual if len(visual) >= 12 else ""


def extract_speech(scenario: str) -> str:
    raw = (scenario or "").replace("\r\n", "\n").replace("\r", "\n")
    quotes: list[str] = []
    for pattern in _QUOTE_RES:
        quotes.extend(m.strip() for m in pattern.findall(raw) if m.strip())
    if quotes:
        return " ".join(quotes)
    match = _SAYS_RE.search(raw)
    if match:
        return match.group(1).strip().strip(" .").strip(_WRAP_QUOTES)
    return ""
