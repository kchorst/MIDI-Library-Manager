from __future__ import annotations

"""Small presentation helpers shared by Library Manager and MIDI Remixer.

The Library Engine owns the measurements.  GUI processes only turn those
measurements into compact human-readable text; they must never invent their own
scan clocks or ETAs because reconnecting a UI would then reset the estimate.
"""


def format_eta(seconds: object, *, include_remaining: bool = True) -> str:
    try:
        value = max(0.0, float(seconds))
    except (TypeError, ValueError):
        return ""
    suffix = " remaining" if include_remaining else ""
    if value < 8:
        return "finishing"
    rounded = int(value + 0.5)
    if rounded < 60:
        return f"~{rounded}s{suffix}"
    minutes = max(1, rounded // 60)
    if minutes < 60:
        return f"~{minutes}m{suffix}"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"~{hours}h {minutes:02d}m{suffix}"
    days, hours = divmod(hours, 24)
    return f"~{days}d {hours}h{suffix}"


def format_file_rate(value: object, *, decimals: int = 1) -> str:
    try:
        rate = float(value)
    except (TypeError, ValueError):
        return ""
    if rate <= 0:
        return ""
    return f"{rate:,.{decimals}f} files/s"
