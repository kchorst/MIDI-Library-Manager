from __future__ import annotations

"""Pure selection rules shared by tabbed UI surfaces."""

from typing import Iterable


def active_library_preview_path(
    tab_index: int,
    library_paths: Iterable[str],
    candidate_pair: tuple[str, str] | None,
) -> str | None:
    """Choose preview ownership from the visible Library Browser tab."""
    if int(tab_index) == 1:
        return candidate_pair[1] if candidate_pair else None
    for path in library_paths:
        if path:
            return str(path)
    return None
