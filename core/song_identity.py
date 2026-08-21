from __future__ import annotations

"""Shared composition identity and arrangement-distance rules.

Recommendation workflows must not confuse alternate files of one composition
with genuinely different songs.  This module centralizes that policy so Guided
and Assisted modes use the same evidence and do not drift apart.
"""

from difflib import SequenceMatcher
import hashlib
from math import log
from pathlib import Path
import re
from typing import Callable, Sequence

from core.remix_profile import MidiProfile, canonical_song_identity


_METADATA_TOKENS = {
    "midi", "karaoke", "instrumental", "backing", "track", "demo", "remaster",
    "remastered", "version", "ver", "arrangement", "arr", "take", "mix", "copy",
    "final", "new", "alternate", "alt", "edit", "export",
}
_UNKNOWN_ARTISTS = {"", "unknown", "various", "anonymous", "untitled"}
_TRAILING_VARIANT_TOKENS = {
    "piano", "orchestral", "orchestra", "acoustic", "solo", "duet", "band", "choir",
    "strings", "string", "guitar", "vocal", "vocals", "live", "full", "score", "reduction",
}
_VARIANT_TOKEN_RE = re.compile(r"^(?:v|ver|version|rev|take|copy|mix|edit|export)\d{1,3}$", re.IGNORECASE)
_MIDI_FORMAT_TOKENS = {"gm", "gs", "xg", "smf", "type0", "type1", "format0", "format1"}


def _split_words(value: str) -> list[str]:
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", str(value or ""))
    tokens = re.sub(r"[^A-Za-z0-9]+", " ", text).casefold().split()
    result: list[str] = []
    variant_prefixes = {"v", "ver", "version", "rev", "take", "copy", "mix", "edit", "export"}
    for index, token in enumerate(tokens):
        if (
            not token
            or token in _METADATA_TOKENS
            or token in _MIDI_FORMAT_TOKENS
            or token == "bpm"
            or _VARIANT_TOKEN_RE.match(token)
        ):
            continue
        if token == "v" and index + 1 < len(tokens) and tokens[index + 1].isdigit():
            continue
        if token.isdigit():
            if 2 <= len(token) <= 3 and index + 1 < len(tokens) and tokens[index + 1] == "bpm":
                continue
            if index > 0 and tokens[index - 1] in variant_prefixes and len(token) <= 3:
                continue
        result.append(token)
    return result


def _joined(tokens: Sequence[str]) -> str:
    return "".join(token for token in tokens if token.isalnum())


def _identity_forms(profile: MidiProfile) -> tuple[str, ...]:
    """Return conservative title forms, including artist-prefix-stripped aliases."""
    raw_values = [profile.song_identity, profile.title, Path(profile.path).stem]
    forms: set[str] = set()
    for raw in raw_values:
        if not raw:
            continue
        canonical = canonical_song_identity(str(raw))
        if canonical:
            forms.add(canonical)
            if canonical.startswith("the") and len(canonical) > 8:
                forms.add(canonical[3:])
        tokens = _split_words(str(raw))
        if tokens and tokens[0] == "the":
            tokens = tokens[1:]
        joined = _joined(tokens)
        if joined:
            forms.add(joined)
        trimmed_tokens = list(tokens)
        while len(trimmed_tokens) >= 2 and trimmed_tokens[-1] in _TRAILING_VARIANT_TOKENS:
            trimmed_tokens.pop()
        trimmed = _joined(trimmed_tokens)
        if len(trimmed) >= 8:
            forms.add(trimmed)

        # If catalogued composer/artist text prefixes the filename, remove it.
        artist_tokens = _split_words(profile.composer)
        if artist_tokens and _joined(artist_tokens) not in _UNKNOWN_ARTISTS:
            if tokens[: len(artist_tokens)] == artist_tokens:
                stripped = _joined(tokens[len(artist_tokens):])
                if len(stripped) >= 8:
                    forms.add(stripped)

        # Some legacy libraries embed artist text in the title but have no
        # trustworthy composer metadata.  Candidate suffixes are supporting
        # evidence only; same_composition also requires musical agreement.
        suffix_tokens = trimmed_tokens if len(trimmed_tokens) >= 3 else tokens
        if len(suffix_tokens) >= 4:
            for start in range(1, min(3, len(suffix_tokens) - 2)):
                suffix = _joined(suffix_tokens[start:])
                if len(suffix) >= 10:
                    forms.add(suffix)
    return tuple(sorted(forms, key=lambda item: (len(item), item)))




def recommendation_family_key_from_fields(*, filename: str, title: str = "", artist: str = "") -> str:
    """Return a Remixer-side normalized composition-family hint.

    This key is intentionally *not* the Library Engine's persisted ``family_id``.
    Persisted family IDs are a versioned Library concern and existing 96K
    catalogues may have been created by an older identity algorithm. Guided and
    Version Fusion therefore use this read-only presentation key as an extra
    guard so tempo labels, export/version suffixes, and common arrangement words
    cannot masquerade as different compositions without forcing a library rescan.

    Bare numbers are preserved unless they are explicitly followed by ``BPM`` so
    real titles such as ``21 Guns`` remain distinct from ``Guns``.
    """

    def words(value: str) -> list[str]:
        raw = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", str(value or ""))
        tokens = re.sub(r"[^A-Za-z0-9]+", " ", raw).casefold().split()
        result: list[str] = []
        noise = _METADATA_TOKENS | _MIDI_FORMAT_TOKENS | _TRAILING_VARIANT_TOKENS | {
            "midi", "mid", "kar", "bpm", "rev", "duplicate", "v",
        }
        variant_prefixes = {"v", "ver", "version", "rev", "take", "copy", "mix", "edit", "export"}
        for index, token in enumerate(tokens):
            if token in noise or _VARIANT_TOKEN_RE.match(token):
                continue
            if token.isdigit():
                if 2 <= len(token) <= 3 and index + 1 < len(tokens) and tokens[index + 1] == "bpm":
                    continue
                if index > 0 and tokens[index - 1] in variant_prefixes and len(token) <= 3:
                    continue
            result.append(token)
        return result

    title_tokens = words(title)
    artist_tokens = words(artist)
    artist_material = " ".join(artist_tokens)
    if artist_material in _UNKNOWN_ARTISTS:
        artist_tokens = []
        artist_material = ""
    if title_tokens and artist_tokens and title_tokens[: len(artist_tokens)] == artist_tokens:
        title_tokens = title_tokens[len(artist_tokens):]
    if title_tokens:
        title_material = " ".join(title_tokens)
        return f"title:{artist_material}|{title_material}" if artist_material else f"title:{title_material}"

    filename_tokens = words(Path(filename).stem)
    if filename_tokens:
        # Filename-only metadata is less trustworthy. Token-order independence
        # handles the common ``Artist - Title`` / ``Title - Artist`` exports.
        return "filename:" + " ".join(sorted(filename_tokens))
    return f"path:{str(filename).casefold()}"


def recommendation_family_key(profile: MidiProfile) -> str:
    """Return the read-only normalized family hint used by recommendation UIs."""
    return recommendation_family_key_from_fields(
        filename=profile.path,
        title=getattr(profile, "title", "") or "",
        artist=getattr(profile, "composer", "") or "",
    )



def _known_artist_key(profile: MidiProfile) -> str:
    material = _joined(_split_words(getattr(profile, "composer", "") or ""))
    return "" if material in _UNKNOWN_ARTISTS else material


def _sequence_ratio(left: Sequence[str], right: Sequence[str]) -> float:
    if not left or not right:
        return 0.0
    return SequenceMatcher(None, tuple(left), tuple(right)).ratio()


def _activity_shape_ratio(a: MidiProfile, b: MidiProfile) -> float:
    """Return a conservative 0..1 similarity for normalized bar-activity shape."""
    left = [max(0.0, float(value)) for value in (a.activity_pattern or [])[:64]]
    right = [max(0.0, float(value)) for value in (b.activity_pattern or [])[:64]]
    if len(left) < 8 or len(right) < 8:
        return 0.0
    samples = min(32, len(left), len(right))

    def sampled(values: list[float]) -> list[float]:
        maximum = max(values) or 1.0
        return [
            values[min(len(values) - 1, int(index * len(values) / samples))] / maximum
            for index in range(samples)
        ]

    aa, bb = sampled(left), sampled(right)
    distance = sum(abs(x - y) for x, y in zip(aa, bb)) / samples
    return max(0.0, min(1.0, 1.0 - distance))



def _sequence_similarity(left: Sequence[str], right: Sequence[str], *, minimum_items: int = 1) -> float:
    """Return alignment-tolerant similarity for ordered bar-level musical tokens."""
    aa = tuple(str(item or "").strip() for item in left if str(item or "").strip())
    bb = tuple(str(item or "").strip() for item in right if str(item or "").strip())
    if len(aa) < minimum_items or len(bb) < minimum_items:
        return 0.0
    return SequenceMatcher(None, aa, bb, autojunk=False).ratio()


def _melody_bar_tokens(profile: MidiProfile) -> tuple[str, ...]:
    """Return a transposition-invariant, instrumentation-agnostic melody shape by bar.

    ``melody_pitch_class_weights`` is already stored relative to the detected tonic,
    so alternate arrangements in another key still produce comparable bar tokens.
    We intentionally keep only the strongest two pitch classes per bar: orchestration
    and doubling may alter note counts, but the composition-level melodic skeleton
    should remain recognizable.
    """
    tokens: list[str] = []
    for histogram in tuple(profile.melody_pitch_class_weights or ())[:96]:
        if not histogram:
            tokens.append(".")
            continue
        ranked = sorted(
            ((str(key), float(value)) for key, value in histogram.items()),
            key=lambda item: (-item[1], item[0]),
        )[:2]
        tokens.append("/".join(item[0] for item in ranked))
    while tokens and tokens[0] == ".":
        tokens.pop(0)
    while tokens and tokens[-1] == ".":
        tokens.pop()
    return tuple(tokens)


def _position_aligned_melody_ratio(a: MidiProfile, b: MidiProfile) -> float:
    """Return conservative same-bar melody agreement for persisted family identity.

    ``SequenceMatcher`` is intentionally tolerant of insertions and shifted phrases,
    which is useful for Remixer-side defensive exclusion but too permissive for
    *persisting* Library family truth: two repetitive songs can obtain a high
    sequence ratio merely by aligning different bars.  Deep-family reconciliation
    therefore also asks how often the relative melodic skeleton agrees at the same
    musical position.
    """
    left = _melody_bar_tokens(a)
    right = _melody_bar_tokens(b)
    if len(left) < 10 or len(right) < 10:
        return 0.0
    count = min(len(left), len(right), 96)
    if count < 10:
        return 0.0
    return sum(1 for index in range(count) if left[index] == right[index]) / float(count)


def _durable_structural_composition_match(a: MidiProfile, b: MidiProfile) -> bool:
    """High-confidence structural identity suitable for persisted Library families.

    The Remixer may conservatively suppress a questionable pair from Guided, but
    the Library must not permanently merge two compositions on harmony/activity
    alone.  When both deep profiles contain usable melody-by-bar evidence, strong
    disagreement is therefore a veto on a harmony-only family merge.
    """
    if a.meter and b.meter and a.meter != b.meter:
        return False

    chords_a = tuple(a.chord_fingerprint or ())[:96]
    chords_b = tuple(b.chord_fingerprint or ())[:96]
    if min(len(chords_a), len(chords_b)) < 12:
        return False

    bars_close = bool(
        a.bars and b.bars and _relative_difference(float(a.bars), float(b.bars)) <= 0.18
    )
    duration_close = bool(
        a.duration_seconds and b.duration_seconds
        and _relative_difference(float(a.duration_seconds), float(b.duration_seconds)) <= 0.20
    )
    if not (bars_close or duration_close):
        return False

    chord_ratio = _sequence_similarity(chords_a, chords_b, minimum_items=12)
    melody_a = _melody_bar_tokens(a)
    melody_b = _melody_bar_tokens(b)
    both_have_melody = len(melody_a) >= 10 and len(melody_b) >= 10
    positional_melody = _position_aligned_melody_ratio(a, b) if both_have_melody else 0.0
    aligned_melody = _sequence_similarity(melody_a, melody_b, minimum_items=10)
    activity_ratio = _activity_shape_ratio(a, b)

    # Strong long-form harmony plus same-position relative melody is durable
    # composition evidence even when orchestration, patching, tempo or density
    # changes substantially between versions.
    if chord_ratio >= 0.90 and positional_melody >= 0.62:
        return True
    if chord_ratio >= 0.82 and positional_melody >= 0.82 and aligned_melody >= 0.90:
        return True

    # If one/both profiles genuinely lack melody evidence, harmony+activity may
    # still establish identity, but only at an intentionally very high bar.
    if not both_have_melody and chord_ratio >= 0.985 and activity_ratio >= 0.96:
        return True
    return False


def _strong_structural_composition_match(a: MidiProfile, b: MidiProfile) -> bool:
    """High-confidence Mash exclusion for same songs whose metadata is useless.

    Real MIDI collections routinely contain aliases such as ``anobrick.mid`` and
    ``pf_brick.mid`` or versions whose melody-track extraction changes when the
    instrumentation changes.  Guided must not call those different songs merely
    because names/family IDs/melody hashes disagree.  This rule therefore compares
    *long-form composition structure*: relative harmony by bar, relative melody
    shape, normalized activity/form, and meter.  It is deliberately conservative
    and only runs on the small prepared recommendation set, never as an O(N²)
    whole-library operation.
    """
    if a.meter and b.meter and a.meter != b.meter:
        return False

    chords_a = tuple(a.chord_fingerprint or ())[:96]
    chords_b = tuple(b.chord_fingerprint or ())[:96]
    # Four-bar textbook progressions are not enough evidence; require a meaningful
    # song-length sequence before metadata-free structural identity is allowed.
    if min(len(chords_a), len(chords_b)) < 12:
        return False

    chord_ratio = _sequence_similarity(chords_a, chords_b, minimum_items=12)
    melody_a = _melody_bar_tokens(a)
    melody_b = _melody_bar_tokens(b)
    melody_ratio = _sequence_similarity(melody_a, melody_b, minimum_items=10)
    activity_ratio = _activity_shape_ratio(a, b)

    bars_close = bool(
        a.bars and b.bars and _relative_difference(float(a.bars), float(b.bars)) <= 0.18
    )
    duration_close = bool(
        a.duration_seconds and b.duration_seconds
        and _relative_difference(float(a.duration_seconds), float(b.duration_seconds)) <= 0.20
    )
    form_close = bars_close or duration_close
    if not form_close:
        return False

    progression_same = bool(
        a.progression_family and b.progression_family
        and a.progression_family != "Unclassified"
        and a.progression_family == b.progression_family
    )

    # Strong melody+harmony agreement is decisive even if total activity differs
    # because one arrangement is sparse and another is fully orchestrated.
    if chord_ratio >= 0.90 and melody_ratio >= 0.82:
        return True
    # A near-identical full harmonic timeline plus matching structural activity can
    # catch instrumental versions whose lead-track detector selected another voice.
    if chord_ratio >= 0.96 and activity_ratio >= 0.90:
        return True
    # A very strong relative melody shape plus substantial harmonic/form evidence.
    if melody_ratio >= 0.94 and chord_ratio >= 0.78 and activity_ratio >= 0.82:
        return True
    # Named progression families are supporting evidence, never sufficient alone.
    return progression_same and chord_ratio >= 0.92 and activity_ratio >= 0.93


def _same_artist_structural_duplicate(a: MidiProfile, b: MidiProfile) -> bool:
    """Catch high-confidence same-song aliases even when filenames have no words in common.

    This is deliberately a *Mash exclusion* rule, not a catalogue identity claim.
    Guided should prefer returning fewer ideas over presenting the same composition
    repeatedly.  A known matching artist plus near-identical musical structure is
    enough to suppress a pair from normal Mash recommendations.
    """
    artist_a, artist_b = _known_artist_key(a), _known_artist_key(b)
    if not artist_a or artist_a != artist_b:
        return False
    if a.meter and b.meter and a.meter != b.meter:
        return False
    if a.mode and b.mode and a.tonic_pc is not None and b.tonic_pc is not None:
        if a.tonic_pc != b.tonic_pc or a.mode != b.mode:
            return False

    bpm_close = bool(
        a.felt_bpm and b.felt_bpm
        and _relative_difference(float(a.felt_bpm), float(b.felt_bpm)) <= 0.025
    )
    duration_close = bool(
        a.duration_seconds and b.duration_seconds
        and _relative_difference(a.duration_seconds, b.duration_seconds) <= 0.035
    )
    bars_close = bool(a.bars and b.bars and _relative_difference(a.bars, b.bars) <= 0.035)
    notes_close = bool(
        a.note_count and b.note_count
        and _relative_difference(float(a.note_count), float(b.note_count)) <= 0.08
    )
    track_close = abs(int(a.track_count or 0) - int(b.track_count or 0)) <= 2
    chord_ratio = _sequence_ratio(tuple(a.chord_fingerprint or ())[:64], tuple(b.chord_fingerprint or ())[:64])
    activity_ratio = _activity_shape_ratio(a, b)
    progression_same = bool(
        a.progression_family and b.progression_family
        and a.progression_family != "Unclassified"
        and a.progression_family == b.progression_family
    )

    close_count = sum((bpm_close, duration_close, bars_close, notes_close, track_close))
    # Require both temporal/form agreement and deeper harmonic/activity evidence.
    # This avoids suppressing two unrelated songs merely because an artist often
    # writes at a similar tempo or in a similar key.
    deep_match = chord_ratio >= 0.88 or activity_ratio >= 0.95 or progression_same
    very_deep_match = chord_ratio >= 0.94 and activity_ratio >= 0.94
    return (close_count >= 4 and deep_match) or (close_count >= 3 and very_deep_match)


def mash_identity_collision_reason(a: MidiProfile, b: MidiProfile) -> str:
    """Return why two files must not be presented as ordinary Mash diversity.

    This predicate is intentionally stricter than durable Library family identity.
    It protects Guided/Assisted from stale or overly literal family IDs in a large
    existing catalogue.  False negatives (fewer recommendations) are preferable to
    showing alternate exports of one composition as if they were distinct songs.
    """
    if a.path == b.path:
        return "same_path"
    if a.content_fingerprint and b.content_fingerprint and a.content_fingerprint == b.content_fingerprint:
        return "same_content"
    if probable_same_composition(a, b):
        return "same_composition"
    if a.musical_fingerprint and b.musical_fingerprint and a.musical_fingerprint == b.musical_fingerprint:
        return "same_melody_fingerprint"
    if _strong_structural_composition_match(a, b):
        return "same_long_form_structure"
    key_a = recommendation_family_key(a)
    key_b = recommendation_family_key(b)
    if key_a == key_b and not key_a.startswith("path:"):
        material = key_a.split(":", 1)[-1]
        significant = re.sub(r"[^a-z0-9]+", "", material.casefold())
        if len(significant) >= 8:
            return "same_normalized_family"
    if _same_artist_structural_duplicate(a, b):
        return "same_artist_structure"
    return ""


def mash_pair_route(a: MidiProfile, b: MidiProfile) -> str:
    """Route a two-source idea to ordinary mash or same-song Fusion review."""
    return "fusion_review" if mash_identity_collision_reason(a, b) else "mash"


def mash_pair_is_too_similar(a: MidiProfile, b: MidiProfile) -> bool:
    return bool(mash_identity_collision_reason(a, b))


def mash_session_identity_keys(profile: MidiProfile) -> frozenset[str]:
    """Return conservative identity keys retained for one recommendation session.

    Multiple keys are intentional: old catalogue family IDs may be literal, while
    deep analysis can still prove identity through a melody fingerprint or normalized
    title. These keys are read-only Remixer hints and never rewrite Library state.
    """
    keys: set[str] = set()
    family = recommendation_family_key(profile)
    if family and not family.startswith("path:"):
        keys.add("family:" + family)
    song_identity = canonical_song_identity(getattr(profile, "song_identity", "") or "")
    if song_identity:
        keys.add("song:" + song_identity)
    melody = str(getattr(profile, "musical_fingerprint", "") or "").strip().casefold()
    if melody:
        keys.add("melody:" + melody)
    content = str(getattr(profile, "content_fingerprint", "") or "").strip().casefold()
    if content:
        keys.add("content:" + content)
    return frozenset(keys)


def mash_pair_conflicts_with_seen(
    a: MidiProfile,
    b: MidiProfile,
    seen_profiles: Sequence[MidiProfile],
) -> bool:
    """Return True when a Guided pair repeats any composition already shown.

    This is the session-level hard gate behind "More Different Mash Ideas".  It
    uses the same conservative identity collision rule as the visible pair gate,
    so filename aliases cannot consume later recommendation slots.
    """
    if mash_pair_is_too_similar(a, b):
        return True
    return any(
        mash_pair_is_too_similar(candidate, prior)
        for candidate in (a, b)
        for prior in seen_profiles
    )




# Durable Library-family classifier version.  This is intentionally independent
# of the quick-catalogue schema and deep-analysis schema: v0.39.2 can reconcile
# already-analyzed schema-15 libraries without re-reading source MIDI files.
IDENTITY_RULESET_VERSION = 2


def _stable_identity_hash(value: str) -> str:
    return hashlib.sha1(str(value).encode("utf-8", errors="replace")).hexdigest()[:20]


def authoritative_same_composition_reason(a: MidiProfile, b: MidiProfile) -> str:
    """Return a high-confidence reason two deep profiles belong to one composition.

    Unlike the Remixer's deliberately conservative presentation guard, this
    predicate is used to persist Library family truth.  It therefore requires
    either decisive content/catalog/melody identity or strong long-form musical
    agreement.  A normalized filename/title hint is never sufficient by itself.
    """
    if a.path == b.path:
        return "same_path"
    if a.content_fingerprint and b.content_fingerprint and a.content_fingerprint == b.content_fingerprint:
        return "same_content"
    if same_composition(a, b):
        return "same_composition"
    if a.musical_fingerprint and b.musical_fingerprint and a.musical_fingerprint == b.musical_fingerprint:
        return "same_melody_fingerprint"
    if _durable_structural_composition_match(a, b):
        return "same_long_form_structure"
    key_a = recommendation_family_key(a)
    key_b = recommendation_family_key(b)
    if key_a == key_b and not key_a.startswith("path:") and _musically_close(a, b):
        return "same_normalized_family"
    return ""


def authoritative_same_composition(a: MidiProfile, b: MidiProfile) -> bool:
    return bool(authoritative_same_composition_reason(a, b))


def identity_index_keys(profile: MidiProfile) -> tuple[str, ...]:
    """Return bounded candidate-bucket keys for 96K family reconciliation.

    Keys only produce *candidate comparisons*; ``authoritative_same_composition``
    remains the final decision.  This avoids an all-pairs 96K comparison while
    still finding opaque aliases whose filenames and melody-track hashes differ.
    """
    keys: set[str] = set()
    if profile.content_fingerprint:
        keys.add("content:" + str(profile.content_fingerprint).casefold())
    if profile.musical_fingerprint:
        keys.add("melody:" + str(profile.musical_fingerprint).casefold())
    family = recommendation_family_key(profile)
    if family and not family.startswith("path:"):
        material = re.sub(r"[^a-z0-9]+", "", family.casefold())
        if len(material) >= 8:
            keys.add("name:" + family.casefold())

    chords = tuple(str(item or "").strip() for item in (profile.chord_fingerprint or ())[:96])
    nonempty = tuple(item for item in chords if item)
    if len(nonempty) >= 12:
        sample_count = min(24, len(chords))
        sampled = tuple(chords[min(len(chords) - 1, int(i * len(chords) / sample_count))] for i in range(sample_count))
        meter = str(profile.meter or profile.meter_family or "").casefold()
        bars_bucket = int(round(float(profile.bars or 0.0) / 4.0))
        keys.add("structure:" + _stable_identity_hash(f"{meter}|{bars_bucket}|{'|'.join(sampled)}"))

        melody = _melody_bar_tokens(profile)
        if len(melody) >= 10:
            melody_count = min(24, len(melody))
            melody_sample = tuple(
                melody[min(len(melody) - 1, int(i * len(melody) / melody_count))]
                for i in range(melody_count)
            )
            keys.add("deep-melody:" + _stable_identity_hash(
                f"{meter}|{bars_bucket}|{'|'.join(melody_sample)}"
            ))

        activity = [max(0.0, float(value)) for value in (profile.activity_pattern or [])[:96]]
        if len(activity) >= 8:
            maximum = max(activity) or 1.0
            activity_count = min(24, len(activity))
            activity_sample = tuple(
                str(int(round(9.0 * activity[min(len(activity) - 1, int(i * len(activity) / activity_count))] / maximum)))
                for i in range(activity_count)
            )
            keys.add("harmony-activity:" + _stable_identity_hash(
                f"{meter}|{bars_bucket}|{'|'.join(sampled)}|{'|'.join(activity_sample)}"
            ))

    artist = _known_artist_key(profile)
    if artist and (profile.meter or profile.meter_family) and profile.bars:
        meter = str(profile.meter or profile.meter_family).casefold()
        # Adjacent four-bar buckets are emitted so small intro/outro edits remain
        # candidates without making every same-artist song an all-pairs group.
        bucket = int(round(float(profile.bars) / 4.0))
        for neighbor in (bucket - 1, bucket, bucket + 1):
            keys.add(f"artist-form:{artist}|{meter}|{neighbor}")
    return tuple(sorted(keys))

def _primary_identity(profile: MidiProfile) -> str:
    if profile.song_identity:
        return canonical_song_identity(profile.song_identity)
    return canonical_song_identity(profile.title or Path(profile.path).stem)


def probable_same_composition(a: MidiProfile, b: MidiProfile) -> bool:
    """Broader filename/title guard used to keep alternate files out of mash lists.

    This intentionally sits beside ``same_composition`` rather than replacing
    it.  The strict predicate remains authoritative for durable identity and
    Version Fusion grouping; this guard catches high-confidence filename
    variants that lack enough musical metadata to prove identity yet.  A short
    5-6 character overlap is *not* sufficient because common words would create
    too many false positives.
    """
    if same_composition(a, b):
        return True
    left = _primary_identity(a)
    right = _primary_identity(b)
    if not left or not right:
        return False
    shorter, longer = sorted((left, right), key=len)
    if len(shorter) < 8:
        return False

    # Different trustworthy artist/composer metadata is a strong reason not to
    # collapse similarly titled songs unless the title evidence is nearly exact.
    artist_a = _joined(_split_words(a.composer))
    artist_b = _joined(_split_words(b.composer))
    artist_conflict = (
        artist_a not in _UNKNOWN_ARTISTS
        and artist_b not in _UNKNOWN_ARTISTS
        and artist_a != artist_b
    )

    shared_forms = {
        item for item in set(_identity_forms(a)).intersection(_identity_forms(b))
        if len(item) >= 8
    }
    if shared_forms and not artist_conflict:
        return True

    matcher = SequenceMatcher(None, shorter, longer)
    ratio = matcher.ratio()
    longest = matcher.find_longest_match(0, len(shorter), 0, len(longer)).size
    tokens_a = set(_split_words(a.title or Path(a.path).stem))
    tokens_b = set(_split_words(b.title or Path(b.path).stem))
    overlap = len(tokens_a & tokens_b) / max(1, min(len(tokens_a), len(tokens_b)))

    if shorter in longer and len(shorter) >= 10:
        extra = len(longer) - len(shorter)
        if extra <= max(12, int(len(shorter) * 0.45)) and not artist_conflict:
            return True
    # High overall similarity plus a long contiguous title run catches files
    # such as ``Song Name.mid`` / ``Song_Name_v2_GM.mid`` without treating a
    # generic six-character substring as identity.
    required_ratio = 0.96 if artist_conflict else 0.88
    required_overlap = 0.90 if artist_conflict else 0.67
    return (
        ratio >= required_ratio
        and overlap >= required_overlap
        and longest >= max(8, int(len(shorter) * 0.55))
    )


def _relative_difference(left: float, right: float) -> float:
    high = max(abs(float(left)), abs(float(right)), 1.0)
    return min(1.0, abs(float(left) - float(right)) / high)


def _musically_close(a: MidiProfile, b: MidiProfile) -> bool:
    if a.musical_fingerprint and b.musical_fingerprint and a.musical_fingerprint == b.musical_fingerprint:
        return True
    evidence = 0
    if a.numerator == b.numerator and a.denominator == b.denominator:
        evidence += 1
    if a.felt_bpm and b.felt_bpm and _relative_difference(a.felt_bpm, b.felt_bpm) <= 0.07:
        evidence += 1
    if a.tonic_pc is not None and b.tonic_pc is not None and a.tonic_pc == b.tonic_pc and a.mode == b.mode:
        evidence += 1
    if a.bars and b.bars and _relative_difference(a.bars, b.bars) <= 0.18:
        evidence += 1
    if a.note_count and b.note_count and _relative_difference(a.note_count, b.note_count) <= 0.35:
        evidence += 1
    return evidence >= 3


def same_composition(a: MidiProfile, b: MidiProfile) -> bool:
    """Return True only when title/file evidence and musical evidence agree.

    Exact file/content/catalog identities are decisive.  Fuzzy title handling is
    intentionally coupled to musical agreement so similarly named but unrelated
    songs are not collapsed merely because they share common words.
    """
    if a.path == b.path:
        return True
    if a.content_fingerprint and b.content_fingerprint and a.content_fingerprint == b.content_fingerprint:
        return True
    if a.song_identity and b.song_identity and a.song_identity == b.song_identity:
        return True

    primary_a = _primary_identity(a)
    primary_b = _primary_identity(b)
    if primary_a and primary_b and primary_a == primary_b:
        return True

    forms_a = set(_identity_forms(a))
    forms_b = set(_identity_forms(b))
    shared = {item for item in forms_a.intersection(forms_b) if len(item) >= 8}
    if shared:
        if _musically_close(a, b):
            return True
        # A long shared title alias that is visibly contained in both filenames
        # remains strong evidence even when an arrangement changes key, tempo,
        # form, or melody extraction enough to weaken musical fingerprints.
        strongest = max(shared, key=len)
        if len(strongest) >= 14 and strongest in primary_a and strongest in primary_b:
            return True

    if not primary_a or not primary_b or not _musically_close(a, b):
        return False
    shorter, longer = sorted((primary_a, primary_b), key=len)
    if len(shorter) >= 10 and shorter in longer and len(longer) - len(shorter) <= max(24, int(len(shorter) * 0.85)):
        return True
    similarity = SequenceMatcher(None, primary_a, primary_b).ratio()
    return similarity >= 0.82


def composition_family_key(profile: MidiProfile) -> str:
    """Return a deterministic display/debug key; pairwise grouping is authoritative."""
    primary = _primary_identity(profile)
    if primary:
        return f"title:{primary}"
    if profile.content_fingerprint:
        return f"content:{profile.content_fingerprint}"
    if profile.musical_fingerprint:
        return f"music:{profile.musical_fingerprint}:{round(profile.bars)}:{profile.note_count // 32}"
    return f"path:{profile.path.casefold()}"


def composition_groups(profiles: Sequence[MidiProfile]) -> tuple[tuple[MidiProfile, ...], ...]:
    """Cluster profiles transitively without an all-library quadratic scan.

    Candidate indexes retrieve files sharing content, catalog identity, title
    aliases, or melody fingerprints.  The stricter same_composition predicate
    still makes the final decision.
    """
    items = tuple(profiles)
    if not items:
        return ()
    parent = list(range(len(items)))
    rank = [0] * len(items)

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        left_root, right_root = find(left), find(right)
        if left_root == right_root:
            return
        if rank[left_root] < rank[right_root]:
            left_root, right_root = right_root, left_root
        parent[right_root] = left_root
        if rank[left_root] == rank[right_root]:
            rank[left_root] += 1

    indexes: dict[str, list[int]] = {}
    for index, profile in enumerate(items):
        keys: set[str] = set()
        if profile.content_fingerprint:
            keys.add(f"content:{profile.content_fingerprint}")
        if profile.song_identity:
            keys.add(f"catalog:{profile.song_identity}")
        if profile.musical_fingerprint:
            keys.add(f"melody:{profile.musical_fingerprint}")
        keys.update(f"title:{form}" for form in _identity_forms(profile) if len(form) >= 10)
        candidates = {prior for key in keys for prior in indexes.get(key, ())}
        for prior in candidates:
            if same_composition(profile, items[prior]):
                union(index, prior)
        for key in keys:
            indexes.setdefault(key, []).append(index)

    grouped: dict[int, list[MidiProfile]] = {}
    order: list[int] = []
    for index, profile in enumerate(items):
        root = find(index)
        if root not in grouped:
            grouped[root] = []
            order.append(root)
        grouped[root].append(profile)
    return tuple(tuple(grouped[root]) for root in order)


def distinct_representatives(
    profiles: Sequence[MidiProfile],
    *,
    rank: Callable[[MidiProfile], object] | None = None,
) -> tuple[MidiProfile, ...]:
    """Choose one best profile from each composition family."""
    groups = composition_groups(profiles)
    if rank is None:
        return tuple(group[0] for group in groups)
    return tuple(max(group, key=rank) for group in groups)


def _programs(profile: MidiProfile) -> set[int]:
    return {
        int(program)
        for track in profile.note_tracks
        if "percussion" not in track.likely_role.casefold()
        for program in track.programs
    }


def _roles(profile: MidiProfile) -> set[str]:
    return {
        track.likely_role.casefold()
        for track in profile.note_tracks
        if track.note_count > 0 and track.likely_role
    }


def _jaccard_distance(left: set, right: set) -> float:
    union = left | right
    if not union:
        return 0.0
    return 1.0 - (len(left & right) / len(union))


def arrangement_distance(a: MidiProfile, b: MidiProfile) -> int:
    """Estimate audible arrangement/instrumentation distance on a 0-100 scale."""
    program_distance = _jaccard_distance(_programs(a), _programs(b))
    role_distance = _jaccard_distance(_roles(a), _roles(b))
    track_distance = min(1.0, abs(a.track_count - b.track_count) / max(a.track_count, b.track_count, 1))
    note_ratio = 0.0
    if a.note_count > 0 and b.note_count > 0:
        note_ratio = min(1.0, abs(log(a.note_count / b.note_count)) / log(4.0))
    arrangement_type = 0.0 if a.arrangement_type == b.arrangement_type else 1.0
    drums_a = any("percussion" in track.likely_role.casefold() for track in a.note_tracks)
    drums_b = any("percussion" in track.likely_role.casefold() for track in b.note_tracks)
    drum_difference = 1.0 if drums_a != drums_b else 0.0
    orchestration = abs(a.orchestration_readiness - b.orchestration_readiness) / 100.0
    length_distance = _relative_difference(a.bars or a.duration_seconds, b.bars or b.duration_seconds)
    score = (
        program_distance * 30.0
        + track_distance * 16.0
        + note_ratio * 16.0
        + arrangement_type * 14.0
        + role_distance * 10.0
        + drum_difference * 8.0
        + orchestration * 3.0
        + length_distance * 3.0
    )
    return max(0, min(100, int(round(score))))


def fusion_difference_kind(a: MidiProfile, b: MidiProfile) -> str:
    """Classify a same-song pair for Version Fusion without rewarding trivial exports.

    Tempo is intentionally absent from arrangement distance.  A global BPM edit,
    filename/version suffix, or a patch-only re-export therefore does not become a
    Fusion recommendation.  Fusion is reserved for versions that can contribute
    different musical roles, sections, density, or track structure.
    """
    if not mash_pair_is_too_similar(a, b):
        return "unrelated"
    if a.content_fingerprint and b.content_fingerprint and a.content_fingerprint == b.content_fingerprint:
        return "exact"

    roles_a, roles_b = _roles(a), _roles(b)
    programs_a, programs_b = _programs(a), _programs(b)
    role_distance = _jaccard_distance(roles_a, roles_b)
    program_distance = _jaccard_distance(programs_a, programs_b)
    track_delta = abs(int(a.track_count or 0) - int(b.track_count or 0))
    note_delta = _relative_difference(float(a.note_count or 0), float(b.note_count or 0))
    length_delta = _relative_difference(float(a.bars or a.duration_seconds or 0), float(b.bars or b.duration_seconds or 0))
    same_music = bool(
        a.musical_fingerprint and b.musical_fingerprint
        and a.musical_fingerprint == b.musical_fingerprint
    )
    same_roles = role_distance < 0.16
    same_structure = track_delta <= 1 and note_delta <= 0.10 and length_delta <= 0.08

    # Same notes/roles/program palette with only a BPM/export edit is redundant.
    if same_music and same_roles and same_structure and program_distance < 0.10:
        return "tempo_only"
    # A different instrument palette is a legitimate Version Fusion asset.  The
    # composition may be identical while one file supplies strings/piano and the
    # other supplies band/orchestral colors; that is exactly a useful Fusion case.
    if same_structure and program_distance >= 0.10:
        return "instrumentation"

    drums_a = any("percussion" in track.likely_role.casefold() for track in a.note_tracks)
    drums_b = any("percussion" in track.likely_role.casefold() for track in b.note_tracks)
    lead_a = any("melody" in role or "lead" in role or "solo" in role for role in roles_a)
    lead_b = any("melody" in role or "lead" in role or "solo" in role for role in roles_b)
    backing_a = any(any(word in role for word in ("accompan", "chord", "bass", "drum", "percussion", "foundation")) for role in roles_a)
    backing_b = any(any(word in role for word in ("accompan", "chord", "bass", "drum", "percussion", "foundation")) for role in roles_b)
    complementary = (lead_a != lead_b) or (backing_a != backing_b) or (drums_a != drums_b)
    if complementary and (role_distance >= 0.20 or track_delta >= 1 or note_delta >= 0.16):
        return "complementary"

    if arrangement_distance(a, b) >= 38 and (role_distance >= 0.20 or track_delta >= 2 or note_delta >= 0.18 or length_delta >= 0.15):
        return "arrangement"
    return "near_duplicate"


def materially_different_arrangement(a: MidiProfile, b: MidiProfile, *, minimum: int = 38) -> bool:
    """Allow Fusion only for genuinely different/complementary same-song arrangements."""
    kind = fusion_difference_kind(a, b)
    if kind not in {"arrangement", "complementary", "instrumentation"}:
        return False
    if kind == "instrumentation":
        # Program palette itself is the material difference; do not require the
        # higher structural-arrangement threshold intended for section/role edits.
        return True
    return arrangement_distance(a, b) >= int(minimum) or kind == "complementary"
