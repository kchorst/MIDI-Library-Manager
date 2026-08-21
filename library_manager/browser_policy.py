from __future__ import annotations

"""Pure Browser policy helpers.

Keep geometry/source-state decisions independent of Qt and filesystem I/O so
selection changes stay instant and the Windows layout contract can be exercised
on every development platform.
"""

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True, slots=True)
class BrowserGeometryPlan:
    width: int
    height: int
    minimum_width: int
    minimum_height: int


def plan_browser_geometry(available_width: int, available_height: int) -> BrowserGeometryPlan:
    """Return a Browser size that is always inside the usable workspace.

    Windows reports logical available geometry after taskbar and display scaling
    are applied.  Never invent a minimum larger than that rectangle: doing so is
    exactly how the RC5/RC6 footer could end up below the screen at high DPI.
    """
    aw = max(1, int(available_width))
    ah = max(1, int(available_height))

    # Leave a small edge margin when the workspace permits it, while still
    # supporting compact 800x600 logical workspaces used by small/high-DPI
    # displays.  The catalogue-first layout is responsible for staying useful
    # at these bounds; geometry must not force the dialog beyond the desktop.
    width_margin = 24 if aw >= 900 else 8
    height_margin = 24 if ah >= 650 else 8
    usable_w = max(1, aw - width_margin)
    usable_h = max(1, ah - height_margin)

    target_w = min(1180, max(720, int(round(aw * 0.92))))
    target_h = min(760, max(520, int(round(ah * 0.90))))
    width = min(usable_w, target_w)
    height = min(usable_h, target_h)

    # Qt minimumSize is a hard geometry constraint.  Keep it inside the same
    # workspace rather than using a fixed 860x560 minimum that can make the
    # initial window physically impossible on scaled displays.
    minimum_width = min(width, 720)
    minimum_height = min(height, 520)
    return BrowserGeometryPlan(width, height, minimum_width, minimum_height)


def _normalised_root(value: str | os.PathLike[str]) -> str:
    try:
        return os.path.normcase(os.path.normpath(str(Path(value).expanduser().resolve(strict=False))))
    except (OSError, ValueError, TypeError):
        return ""


def source_roots_ready(configured_roots: Iterable[str], accessible_roots: Iterable[str]) -> bool:
    """True only when every configured source identity is currently accessible.

    Count equality is insufficient: source A can disappear while unrelated
    source C appears, producing the same count but an unsafe bulk-retry state.
    """
    configured = {_normalised_root(root) for root in configured_roots if str(root).strip()}
    accessible = {_normalised_root(root) for root in accessible_roots if str(root).strip()}
    configured.discard("")
    accessible.discard("")
    return bool(configured) and configured.issubset(accessible)


def saved_row_can_attempt_shell_action(*, eligibility: str) -> bool:
    """Whether Open/Show may be attempted without probing the filesystem.

    Even an unreadable Fast-scan failure should remain inspectable in the OS so
    the user can replace or diagnose it. Quarantine is the exception because
    the saved active-source path intentionally no longer names the live file.
    """
    return str(eligibility or "").casefold() != "quarantined"


def saved_row_can_attempt_audition(*, quick_status: str, eligibility: str) -> bool:
    """Whether a saved row may *attempt* Library audition without filesystem I/O.

    The actual Play worker validates the exact path. This keeps row selection
    instant on disconnected/network drives while refusing unreadable or
    quarantined rows before audio setup starts.
    """
    quick = str(quick_status or "").casefold()
    state = str(eligibility or "").casefold()
    return quick != "failed" and state != "quarantined"
