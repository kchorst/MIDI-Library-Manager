from __future__ import annotations

from math import sqrt
from typing import Iterable

MAJOR_PROFILE = [6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88]
MINOR_PROFILE = [6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17]
NAMES_SHARP = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


def _correlation(a: list[float], b: list[float]) -> float:
    ma = sum(a) / len(a)
    mb = sum(b) / len(b)
    da = [v - ma for v in a]
    db = [v - mb for v in b]
    denom = sqrt(sum(v * v for v in da) * sum(v * v for v in db))
    return sum(x * y for x, y in zip(da, db)) / denom if denom else 0.0


def estimate_key(weighted_pitch_classes: Iterable[tuple[int, float]]) -> tuple[str | None, str | None, float]:
    histogram = [0.0] * 12
    for pitch, weight in weighted_pitch_classes:
        if 0 <= pitch <= 127 and weight > 0:
            histogram[pitch % 12] += float(weight)
    if sum(histogram) <= 0:
        return None, None, 0.0

    scores: list[tuple[float, int, str]] = []
    for root in range(12):
        major = MAJOR_PROFILE[-root:] + MAJOR_PROFILE[:-root] if root else MAJOR_PROFILE
        minor = MINOR_PROFILE[-root:] + MINOR_PROFILE[:-root] if root else MINOR_PROFILE
        scores.append((_correlation(histogram, major), root, "major"))
        scores.append((_correlation(histogram, minor), root, "minor"))
    scores.sort(reverse=True)
    best, second = scores[0], scores[1]
    margin = max(0.0, best[0] - second[0])
    confidence = max(0.0, min(1.0, 0.55 * max(0.0, best[0]) + 2.2 * margin))
    return NAMES_SHARP[best[1]], best[2], round(confidence, 3)


def parse_declared_key(value: str | None) -> tuple[str | None, str | None]:
    if not value:
        return None, None
    value = value.strip().replace("♭", "b").replace("♯", "#")
    is_minor = value.endswith("m")
    root = value[:-1] if is_minor else value
    enharmonic = {
        "Db": "C#", "Eb": "D#", "Gb": "F#", "Ab": "G#", "Bb": "A#",
        "Cb": "B", "Fb": "E", "E#": "F", "B#": "C",
    }
    root = enharmonic.get(root, root)
    if root not in NAMES_SHARP:
        return None, None
    return root, "minor" if is_minor else "major"
