from __future__ import annotations

import re
import unicodedata
from pathlib import Path

_NOISE_PATTERNS = [
    r"\b(?:midi|mid|karaoke|kar|gm|gs|xg|smf)\b",
    r"\b(?:full\s*band|piano\s*(?:only|version|arrangement)?|instrumental|backing\s*track)\b",
    r"\b(?:type\s*[012]|format\s*[012])\b",
    r"\b(?:ver(?:sion)?|take|mix|edit|rev)\s*[-_. ]*\d+[a-z]?\b",
    r"\b(?:copy|duplicate|final|new)\b",
    r"\b\d{2,3}\s*bpm\b",
]


def normalize_text(value: str | None) -> str:
    if not value:
        return ""
    value = unicodedata.normalize("NFKD", value)
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = value.casefold().replace("&", " and ")
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def normalize_filename(path_or_name: str | Path) -> str:
    stem = Path(path_or_name).stem
    value = stem.replace("_", " ").replace("-", " ")
    value = normalize_text(value)
    for pattern in _NOISE_PATTERNS:
        value = re.sub(pattern, " ", value, flags=re.IGNORECASE)
    value = re.sub(r"\b(?:v)?\d+\b$", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def identity_tokens(value: str) -> set[str]:
    stop = {
        "a", "an", "and", "the", "of", "to", "in", "on", "for", "with",
        "feat", "featuring", "vs", "version", "remix", "mix",
    }
    return {token for token in normalize_text(value).split() if len(token) > 1 and token not in stop}


def filename_family_key(path_or_name: str | Path) -> str:
    value = normalize_filename(path_or_name)
    value = re.sub(r"\b(?:live|demo|acoustic|orchestral|solo|duet)\b", " ", value)
    return re.sub(r"\s+", " ", value).strip()
