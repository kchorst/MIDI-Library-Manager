from __future__ import annotations

"""Persistent user preference helpers for recommendation suppression."""

import hashlib
import re
from itertools import combinations
from typing import Iterable

from core.remix_profile import MidiProfile
from midi_library_scan.database import LibraryDatabase

_SPACE_RE = re.compile(r"[^a-z0-9]+")


def _normalized_text(value: str | None) -> str:
    return _SPACE_RE.sub(" ", str(value or "").casefold()).strip()


def song_identity_key(profile: MidiProfile) -> str:
    """Return a stable composition-level key where evidence permits.

    Composition/catalog identity is preferred so alternate filenames and MIDI
    arrangements inherit an ignored-pair decision.  Musical fingerprints are the
    next-best cross-filename signal.  Content and path are last-resort fallbacks.
    """
    if profile.song_identity:
        return "song:" + _normalized_text(profile.song_identity)
    if profile.musical_fingerprint:
        return "music:" + str(profile.musical_fingerprint).strip().casefold()
    title = _normalized_text(profile.title)
    composer = _normalized_text(profile.composer)
    if title and title not in {"unknown", "untitled"}:
        material = f"{composer}|{title}" if composer and composer != "unknown" else title
        return "name:" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]
    if profile.content_fingerprint:
        return "content:" + str(profile.content_fingerprint).strip().casefold()
    return "path:" + hashlib.sha256(str(profile.path).casefold().encode("utf-8")).hexdigest()[:24]


def pair_identities(a: MidiProfile, b: MidiProfile) -> tuple[str, str]:
    first, second = song_identity_key(a), song_identity_key(b)
    return (first, second) if first < second else (second, first)


def pair_is_ignored(database: LibraryDatabase, a: MidiProfile, b: MidiProfile) -> bool:
    first, second = pair_identities(a, b)
    if first == second:
        return False
    return database.is_pair_ignored(first, second)


def profiles_contain_ignored_pair(database: LibraryDatabase, profiles: Iterable[MidiProfile]) -> bool:
    items = tuple(profiles)
    return any(pair_is_ignored(database, a, b) for a, b in combinations(items, 2))


def ignore_profile_pair(database: LibraryDatabase, a: MidiProfile, b: MidiProfile) -> None:
    first, second = pair_identities(a, b)
    if first == second:
        return
    database.ignore_pair(first, second, a.title or "Song A", b.title or "Song B")
