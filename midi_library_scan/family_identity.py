from __future__ import annotations

"""Stable composition-family identity for 96k-scale discovery.

The family id is deliberately cheap and persistent.  It is not a claim that two
files are byte-identical; it is a durable bucket used to suppress likely alternate
arrangements during normal mash discovery and to find Version Fusion candidates
without fuzzy-comparing an anchor against the entire library on every search.
"""

import hashlib
from pathlib import Path

from .normalize import filename_family_key, identity_tokens, normalize_text


def _hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", errors="replace")).hexdigest()[:20]


def family_id_from_fields(
    *,
    filename: str,
    composition_id: str | None = None,
    artist: str | None = None,
    title: str | None = None,
) -> str:
    composition = str(composition_id or "").strip()
    if composition:
        return f"catalog:{composition}"

    normalized_title = normalize_text(title)
    normalized_artist = normalize_text(artist)
    if normalized_title:
        material = f"{normalized_artist}|{normalized_title}" if normalized_artist else normalized_title
        return f"title:{_hash(material)}"

    family = filename_family_key(Path(filename).name)
    if family:
        # File names are often ordered as either "Artist - Title" or "Title - Artist".
        # For uncatalogued files, a token-order-independent key groups those common
        # alternate arrangements without a 96k fuzzy comparison during discovery.
        tokens = sorted(identity_tokens(family))
        material = " ".join(tokens) if len(tokens) >= 2 else family
        return f"filename:{_hash(material)}"
    return f"filename:{_hash(str(filename).casefold())}"


def family_id_for_result(result) -> str:
    match = getattr(result, "catalog_match", None)
    return family_id_from_fields(
        filename=str(getattr(result, "filename", "") or ""),
        composition_id=getattr(match, "composition_id", None),
        artist=getattr(match, "artist", None),
        title=getattr(match, "title", None),
    )
