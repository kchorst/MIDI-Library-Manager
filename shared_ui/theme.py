from __future__ import annotations

"""Shared stylesheet resources used by both Library Manager and Remixer."""

from functools import lru_cache
from pathlib import Path

_STYLE_ROOT = Path(__file__).resolve().parents[1] / "assets" / "styles"


@lru_cache(maxsize=16)
def load_stylesheet(name: str) -> str:
    path = (_STYLE_ROOT / str(name)).resolve()
    if _STYLE_ROOT.resolve() not in path.parents:
        raise ValueError(f"Invalid stylesheet resource: {name}")
    return path.read_text(encoding="utf-8")
