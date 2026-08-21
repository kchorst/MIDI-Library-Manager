from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
import hashlib
import math
import re
import statistics
from typing import Iterable

import mido

from core.resource_safety import DEFAULT_MIDI_LIMITS, ProcessingBudget, validate_decoded_midi, validate_file
from core.pair_assessment_cache import get_pair_assessment_cache


NOTE_NAMES = ("C", "C#", "D", "Eb", "E", "F", "F#", "G", "Ab", "A", "Bb", "B")
KEY_TO_PC = {
    "C": 0, "B#": 0,
    "C#": 1, "Db": 1,
    "D": 2,
    "D#": 3, "Eb": 3,
    "E": 4, "Fb": 4,
    "F": 5, "E#": 5,
    "F#": 6, "Gb": 6,
    "G": 7,
    "G#": 8, "Ab": 8,
    "A": 9,
    "A#": 10, "Bb": 10,
    "B": 11, "Cb": 11,
}

GM_PROGRAM_NAMES = (
    "Acoustic Grand Piano", "Bright Acoustic Piano", "Electric Grand Piano", "Honky-tonk Piano",
    "Electric Piano 1", "Electric Piano 2", "Harpsichord", "Clavinet",
    "Celesta", "Glockenspiel", "Music Box", "Vibraphone", "Marimba", "Xylophone", "Tubular Bells", "Dulcimer",
    "Drawbar Organ", "Percussive Organ", "Rock Organ", "Church Organ", "Reed Organ", "Accordion", "Harmonica", "Tango Accordion",
    "Acoustic Guitar (nylon)", "Acoustic Guitar (steel)", "Electric Guitar (jazz)", "Electric Guitar (clean)",
    "Electric Guitar (muted)", "Overdriven Guitar", "Distortion Guitar", "Guitar Harmonics",
    "Acoustic Bass", "Electric Bass (finger)", "Electric Bass (pick)", "Fretless Bass", "Slap Bass 1", "Slap Bass 2", "Synth Bass 1", "Synth Bass 2",
    "Violin", "Viola", "Cello", "Contrabass", "Tremolo Strings", "Pizzicato Strings", "Orchestral Harp", "Timpani",
    "String Ensemble 1", "String Ensemble 2", "Synth Strings 1", "Synth Strings 2", "Choir Aahs", "Voice Oohs", "Synth Voice", "Orchestra Hit",
    "Trumpet", "Trombone", "Tuba", "Muted Trumpet", "French Horn", "Brass Section", "Synth Brass 1", "Synth Brass 2",
    "Soprano Sax", "Alto Sax", "Tenor Sax", "Baritone Sax", "Oboe", "English Horn", "Bassoon", "Clarinet",
    "Piccolo", "Flute", "Recorder", "Pan Flute", "Blown Bottle", "Shakuhachi", "Whistle", "Ocarina",
    "Lead 1 (square)", "Lead 2 (sawtooth)", "Lead 3 (calliope)", "Lead 4 (chiff)", "Lead 5 (charang)", "Lead 6 (voice)", "Lead 7 (fifths)", "Lead 8 (bass + lead)",
    "Pad 1 (new age)", "Pad 2 (warm)", "Pad 3 (polysynth)", "Pad 4 (choir)", "Pad 5 (bowed)", "Pad 6 (metallic)", "Pad 7 (halo)", "Pad 8 (sweep)",
    "FX 1 (rain)", "FX 2 (soundtrack)", "FX 3 (crystal)", "FX 4 (atmosphere)", "FX 5 (brightness)", "FX 6 (goblins)", "FX 7 (echoes)", "FX 8 (sci-fi)",
    "Sitar", "Banjo", "Shamisen", "Koto", "Kalimba", "Bag Pipe", "Fiddle", "Shanai",
    "Tinkle Bell", "Agogo", "Steel Drums", "Woodblock", "Taiko Drum", "Melodic Tom", "Synth Drum", "Reverse Cymbal",
    "Guitar Fret Noise", "Breath Noise", "Seashore", "Bird Tweet", "Telephone Ring", "Helicopter", "Applause", "Gunshot",
)

# Metadata only: no MIDI content is distributed. These entries make the seven-file
# pilot immediately readable and demonstrate the later known-song-family catalog.
KNOWN_TITLES: dict[str, dict[str, str]] = {
    "thestarsandstripesforever": {"title": "The Stars and Stripes Forever", "composer": "John Philip Sousa", "style": "March"},
    "thelibertybell": {"title": "The Liberty Bell", "composer": "John Philip Sousa", "style": "March"},
    "thegladiator": {"title": "The Gladiator", "composer": "John Philip Sousa", "style": "March"},
    "winners": {"title": "The Easy Winners", "composer": "Scott Joplin", "style": "Ragtime"},
    "theeasywinners": {"title": "The Easy Winners", "composer": "Scott Joplin", "style": "Ragtime"},
    "maple": {"title": "Maple Leaf Rag", "composer": "Scott Joplin", "style": "Ragtime"},
    "mapleleafrag": {"title": "Maple Leaf Rag", "composer": "Scott Joplin", "style": "Ragtime"},
    "abreezefromalabama": {"title": "A Breeze from Alabama", "composer": "Scott Joplin", "style": "Ragtime"},
    "pineapplerag": {"title": "Pineapple Rag", "composer": "Scott Joplin", "style": "Ragtime"},
}


def _slug(value: str) -> str:
    return "".join(ch.lower() for ch in value if ch.isalnum())


def known_metadata(path: str | Path) -> dict[str, str]:
    stem = Path(path).stem
    return dict(KNOWN_TITLES.get(_slug(stem), {"title": stem.replace("_", " ").replace("-", " ").strip(), "composer": "Unknown", "style": "Unclassified"}))


def parse_key_signature(value: str | None) -> tuple[int | None, str]:
    if not value:
        return None, "major"
    text = str(value).strip()
    mode = "minor" if text.endswith("m") else "major"
    tonic = text[:-1] if mode == "minor" else text
    return KEY_TO_PC.get(tonic), mode


def key_name(pc: int | None, mode: str = "major") -> str:
    if pc is None:
        return "Unknown"
    root = NOTE_NAMES[int(pc) % 12]
    return f"{root} minor" if mode == "minor" else f"{root} major"


def signed_pitch_class_shift(source_pc: int | None, target_pc: int | None) -> int:
    if source_pc is None or target_pc is None:
        return 0
    raw = (int(target_pc) - int(source_pc)) % 12
    return raw - 12 if raw > 6 else raw


def normalize_felt_bpm(bpm: float | None) -> float | None:
    if bpm is None or bpm <= 0:
        return None
    value = float(bpm)
    while value > 150:
        value /= 2.0
    while value < 70:
        value *= 2.0
    return round(value, 3)


def felt_bpm_for_meter(bpm: float | None, numerator: int, denominator: int) -> float | None:
    """Estimate the written pulse unit before applying half/double-time fallback.

    MIDI tempo is always encoded in quarter notes. Cut time is normally felt in
    half notes, while compound meters such as 6/8 are normally felt in dotted
    quarters. Simple quarter-note meters keep the encoded quarter pulse, so a
    legitimate 60-BPM 2/4 file is not silently doubled to 120.
    """
    if bpm is None or bpm <= 0:
        return None
    value = float(bpm)
    if int(denominator) == 2:
        value /= 2.0
    elif int(denominator) in {8, 16} and int(numerator) >= 6 and int(numerator) % 3 == 0:
        # Dotted-quarter (or proportional dotted-eighth) pulse from quarter BPM.
        value *= 2.0 / 3.0
    while value > 220:
        value /= 2.0
    while value < 40:
        value *= 2.0
    return round(value, 3)


def meter_family(numerator: int, denominator: int) -> str:
    if numerator in {2, 4} and denominator in {2, 4}:
        return "simple duple"
    if numerator in {6, 12} and denominator in {8, 16}:
        return "compound duple"
    if numerator in {3}:
        return "triple"
    if numerator in {9}:
        return "compound triple"
    return "other"


def quarter_beats_per_bar(numerator: int, denominator: int) -> float:
    return float(numerator) * 4.0 / float(max(1, denominator))


def gm_program_name(program: int | None) -> str:
    if program is None:
        return "General MIDI default"
    value = max(0, min(127, int(program)))
    return GM_PROGRAM_NAMES[value]


@dataclass(frozen=True, slots=True)
class NoteTrackProfile:
    index: int
    name: str
    note_count: int
    min_note: int | None
    max_note: int | None
    mean_note: float | None
    median_note: float | None
    channels: list[int] = field(default_factory=list)
    programs: list[int] = field(default_factory=list)
    likely_role: str = "part"
    median_velocity: float | None = None
    velocity_low: int | None = None
    velocity_high: int | None = None
    max_polyphony: int = 0
    polyphonic_onset_ratio: float = 0.0
    monophonic_score: int = 0
    program_names: list[str] = field(default_factory=list)
    safe_for_solo_instrument: bool = False
    instrumentation_advice: str = ""
    role_confidence: int = 0
    role_reasons: list[str] = field(default_factory=list)

    def display(self) -> str:
        pitch = "no notes" if self.min_note is None else f"{self.min_note}–{self.max_note}"
        instrument = ", ".join(self.program_names) if self.program_names else "GM default"
        return (
            f"Track {self.index + 1}: {self.name or 'unnamed'} · {self.likely_role} · "
            f"{self.note_count} notes · range {pitch} · {instrument} · polyphony {self.max_polyphony}"
        )


@dataclass(frozen=True, slots=True)
class MidiProfile:
    path: str
    fingerprint: str
    title: str
    composer: str
    style: str
    midi_type: int
    ticks_per_beat: int
    track_count: int
    note_count: int
    duration_seconds: float
    total_ticks: int
    tempo_bpm: float | None
    felt_bpm: float | None
    tempo_change_count: int
    numerator: int
    denominator: int
    meter: str
    meter_family: str
    bars: float
    initial_key_signature: str | None
    key_changes: list[tuple[float, str]]
    tonic_pc: int | None
    mode: str
    note_tracks: list[NoteTrackProfile]
    melody_track_index: int | None
    accompaniment_track_index: int | None
    warnings: list[str] = field(default_factory=list)
    declared_tonic_pc: int | None = None
    declared_mode: str = "major"
    analyzed_tonic_pc: int | None = None
    analyzed_mode: str = "major"
    key_confidence: int = 0
    key_agreement: str = "unknown"
    section_keys: list[dict] = field(default_factory=list)
    chord_fingerprint: list[str] = field(default_factory=list)
    melody_pitch_class_weights: list[dict[str, float]] = field(default_factory=list)
    arrangement_type: str = "unclassified"
    orchestration_readiness: int = 0
    melody_extraction_confidence: int = 0
    dynamics_median: float | None = None
    dynamics_low: int | None = None
    dynamics_high: int | None = None
    progression_family: str = "Unclassified"
    reference_tags: list[str] = field(default_factory=list)
    content_fingerprint: str = ""
    song_identity: str = ""
    signature_assets: list[dict] = field(default_factory=list)
    main_entry_bar: float = 0.0
    melody_start_bar: float = 0.0
    accompaniment_start_bar: float = 0.0
    activity_pattern: list[float] = field(default_factory=list)
    musical_fingerprint: str = ""

    @property
    def key_display(self) -> str:
        if self.initial_key_signature:
            return self.initial_key_signature
        return key_name(self.tonic_pc, self.mode)

    @property
    def analyzed_key_display(self) -> str:
        return key_name(self.analyzed_tonic_pc, self.analyzed_mode)

    @property
    def meter_display(self) -> str:
        return f"{self.numerator}/{self.denominator}"

    @property
    def bar_ticks(self) -> float:
        return float(self.ticks_per_beat) * quarter_beats_per_bar(self.numerator, self.denominator)

    def compact_summary(self) -> str:
        tempo = "unknown tempo" if self.felt_bpm is None else f"{self.felt_bpm:g} felt BPM"
        return (
            f"{self.style} · {tempo} · {self.meter_display} · declared {self.key_display} · "
            f"analyzed {self.analyzed_key_display} ({self.key_confidence}%) · {self.arrangement_type}"
        )

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class PairAssessment:
    source_a: str
    source_b: str
    title_a: str
    title_b: str
    relationship: str
    preparation_score: int
    tempo_score: int
    meter_score: int
    length_score: int
    role_score: int
    key_shift_b_to_a: int
    target_felt_bpm: int
    observations: list[str]
    tempo_relationship: str = "direct"
    key_score: int = 0
    harmonic_score: int = 0
    orchestration_score: int = 0
    adaptation_required: str = "moderate"
    pairing_category: str = "structural relationship"
    documented_reference: str | None = None
    shared_progression_family: str | None = None
    melody_a_over_b_score: int = 0
    melody_b_over_a_score: int = 0
    entry_alignment_score: int = 0
    structural_alignment_score: int = 0
    # Reference metadata never changes MIDI-derived scores. It is retained for
    # transparent explanations and bounded ranking tie-breakers only.
    reference_score: int = 0
    familiarity_score: int = 0
    reference_evidence: tuple[str, ...] = ()
    reference_sources: tuple[str, ...] = ()
    reference_urls: tuple[str, ...] = ()

    def summary(self) -> str:
        return (
            f"Preparation {self.preparation_score}% · harmony {self.harmonic_score}% · "
            f"transpose B {self.key_shift_b_to_a:+d} · target {self.target_felt_bpm} BPM · {self.pairing_category}"
        )

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class _NoteInterval:
    track_index: int
    channel: int
    note: int
    velocity: int
    start_tick: int
    end_tick: int


def _fingerprint(path: Path) -> str:
    stat = path.stat()
    digest = hashlib.sha256()
    digest.update(str(path.resolve()).encode("utf-8", errors="replace"))
    digest.update(str(stat.st_size).encode())
    digest.update(str(stat.st_mtime_ns).encode())
    return digest.hexdigest()[:24]


def _content_fingerprint(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()[:24]


def canonical_song_identity(title: str, path: str | Path | None = None) -> str:
    """Normalize common filename/version noise for duplicate-song filtering.

    This is deliberately conservative: it groups obvious copies and alternate
    filenames, but does not claim two musically similar songs are the same work.
    """
    text = str(title or (Path(path).stem if path else "")).casefold()
    text = re.sub(r"\([^)]*(?:midi|karaoke|version|ver\.?|mix|arrangement|arr\.?|demo|take|instrumental)[^)]*\)", " ", text)
    text = re.sub(r"\[[^]]*(?:midi|karaoke|version|ver\.?|mix|arrangement|arr\.?|demo|take|instrumental)[^]]*\]", " ", text)
    text = re.sub(r"\b(?:midi|karaoke|instrumental|backing track|demo|remaster(?:ed)?|version|ver\.?|arrangement|arr\.?|take)\b", " ", text)
    text = re.sub(r"\b(?:v|ver|version|take|mix)[-_ ]?\d+\b", " ", text)
    text = re.sub(r"\b\d{2,4}kbps\b", " ", text)
    return "".join(ch for ch in text if ch.isalnum())


def primary_signature_asset(profile: "MidiProfile") -> dict | None:
    protected = [item for item in profile.signature_assets if bool(item.get("protect_default"))]
    choices = protected or list(profile.signature_assets)
    if not choices:
        return None
    return max(choices, key=lambda item: (int(item.get("confidence", 0)), int(item.get("length_bars", 0))))


def _key_correlations(weighted_notes: Iterable[tuple[int, float]]) -> list[tuple[float, int, str]]:
    histogram = [0.0] * 12
    for note, weight in weighted_notes:
        histogram[int(note) % 12] += max(0.001, float(weight))
    total = sum(histogram)
    if total <= 0:
        return []
    histogram = [v / total for v in histogram]
    major_profile = [6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88]
    minor_profile = [6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17]

    def correlation(profile: list[float], shift: int) -> float:
        rotated = profile[-shift:] + profile[:-shift] if shift else profile
        mean_h = sum(histogram) / 12.0
        mean_p = sum(rotated) / 12.0
        num = sum((histogram[i] - mean_h) * (rotated[i] - mean_p) for i in range(12))
        den_h = math.sqrt(sum((v - mean_h) ** 2 for v in histogram))
        den_p = math.sqrt(sum((v - mean_p) ** 2 for v in rotated))
        return num / (den_h * den_p) if den_h and den_p else 0.0

    candidates: list[tuple[float, int, str]] = []
    for pc in range(12):
        candidates.append((correlation(major_profile, pc), pc, "major"))
        candidates.append((correlation(minor_profile, pc), pc, "minor"))
    return sorted(candidates, reverse=True)


def _estimate_key_from_notes(weighted_notes: Iterable[tuple[int, float]]) -> tuple[int | None, str, int]:
    candidates = _key_correlations(weighted_notes)
    if not candidates:
        return None, "major", 0
    top = candidates[0]
    runner = candidates[1] if len(candidates) > 1 else (0.0, 0, "major")
    # Confidence is intentionally conservative: corpus MIDI reductions often modulate.
    absolute = max(0.0, min(1.0, (top[0] + 1.0) / 2.0))
    margin = max(0.0, top[0] - runner[0])
    confidence = int(round(max(5.0, min(99.0, absolute * 72.0 + margin * 90.0))))
    return top[1], top[2], confidence


def _interval_weights(intervals: list[_NoteInterval], start_tick: int, end_tick: int) -> list[tuple[int, float]]:
    result: list[tuple[int, float]] = []
    for item in intervals:
        overlap = max(0, min(item.end_tick, end_tick) - max(item.start_tick, start_tick))
        if overlap <= 0:
            continue
        result.append((item.note, overlap * max(0.2, item.velocity / 100.0)))
    return result


def _detect_chord_token(weights: list[tuple[int, float]], tonic_pc: int | None, mode: str) -> str:
    if not weights:
        return "N"
    histogram = [0.0] * 12
    for note, weight in weights:
        histogram[note % 12] += float(weight)
    total = sum(histogram)
    if total <= 0:
        return "N"
    templates = {
        "": (0, 4, 7),
        "m": (0, 3, 7),
        "dim": (0, 3, 6),
        "sus": (0, 5, 7),
    }
    best: tuple[float, int, str] = (-1.0, 0, "")
    for root in range(12):
        for quality, pcs in templates.items():
            in_score = sum(histogram[(root + pc) % 12] for pc in pcs)
            root_bonus = histogram[root] * 0.35
            outside = total - in_score
            score = in_score + root_bonus - outside * 0.10
            if score > best[0]:
                best = (score, root, quality)
    _, root, quality = best
    relative = root if tonic_pc is None else (root - tonic_pc) % 12
    degrees_major = ("I", "bII", "II", "bIII", "III", "IV", "#IV", "V", "bVI", "VI", "bVII", "VII")
    degrees_minor = ("i", "bII", "II", "III", "#III", "iv", "#iv", "v", "VI", "#VI", "VII", "#VII")
    degree = (degrees_minor if mode == "minor" else degrees_major)[relative]
    if quality == "m" and degree and degree[0].isupper():
        degree = degree.lower()
    elif quality == "" and degree and degree[0].islower():
        degree = degree.upper()
    elif quality == "dim":
        degree += "°"
    elif quality == "sus":
        degree += "sus"
    return degree


def _token_root(token: str) -> tuple[int | None, str]:
    if token == "N":
        return None, "N"
    clean = token.replace("°", "").replace("sus", "")
    quality = "dim" if "°" in token else "sus" if "sus" in token else "minor" if clean and clean[-1].islower() else "major"
    mapping = {
        "I": 0, "bII": 1, "II": 2, "bIII": 3, "III": 4, "IV": 5,
        "#IV": 6, "V": 7, "bVI": 8, "VI": 9, "bVII": 10, "VII": 11,
    }
    return mapping.get(clean.upper().replace("B", "b").replace("#IV", "#IV"), None), quality


def _harmonic_similarity(a_tokens: list[str], b_tokens: list[str]) -> int:
    a = [t for t in a_tokens if t != "N"]
    b = [t for t in b_tokens if t != "N"]
    if not a or not b:
        return 35
    samples = min(48, max(8, min(len(a), len(b))))
    score = 0.0
    for i in range(samples):
        ta = a[min(len(a) - 1, int(i * len(a) / samples))]
        tb = b[min(len(b) - 1, int(i * len(b) / samples))]
        if ta == tb:
            score += 1.0
            continue
        ra, qa = _token_root(ta)
        rb, qb = _token_root(tb)
        if ra is None or rb is None:
            score += 0.25
        elif ra == rb:
            score += 0.72 if qa != qb else 0.9
        elif (ra - rb) % 12 in {5, 7}:
            score += 0.42
        elif (ra - rb) % 12 in {3, 4, 8, 9}:
            score += 0.28
        else:
            score += 0.08
    return max(0, min(100, int(round(100.0 * score / samples))))


def _chord_pitch_classes(token: str) -> tuple[set[int], int | None]:
    root, quality = _token_root(token)
    if root is None:
        return set(), None
    intervals = {
        "major": (0, 4, 7),
        "minor": (0, 3, 7),
        "dim": (0, 3, 6),
        "sus": (0, 5, 7),
    }.get(quality, (0, 4, 7))
    return {(root + interval) % 12 for interval in intervals}, root


def _activity_similarity(a_values: list[float], b_values: list[float]) -> int:
    """Compare coarse per-bar activity shapes without treating it as artistic quality."""
    a = [max(0.0, float(v)) for v in a_values]
    b = [max(0.0, float(v)) for v in b_values]
    if not a or not b:
        return 45
    samples = min(32, max(8, min(len(a), len(b))))
    def sample(values: list[float]) -> list[float]:
        return [values[min(len(values) - 1, int(i * len(values) / samples))] for i in range(samples)]
    aa, bb = sample(a), sample(b)
    max_a, max_b = max(aa) or 1.0, max(bb) or 1.0
    aa = [v / max_a for v in aa]
    bb = [v / max_b for v in bb]
    distance = sum(abs(x - y) for x, y in zip(aa, bb)) / samples
    return max(0, min(100, int(round(100.0 * (1.0 - distance)))))


def windowed_pair_scores(
    a: MidiProfile,
    b: MidiProfile,
    *,
    start_bar_a: int = 0,
    start_bar_b: int = 0,
    bars: int = 32,
) -> tuple[int, int, int, int]:
    """Return harmony, A-over-B, B-over-A, and activity scores for a chosen window."""
    count = max(8, int(bars))
    a0, b0 = max(0, int(start_bar_a)), max(0, int(start_bar_b))
    a_chords = a.chord_fingerprint[a0:a0 + count]
    b_chords = b.chord_fingerprint[b0:b0 + count]
    a_melody = a.melody_pitch_class_weights[a0:a0 + count]
    b_melody = b.melody_pitch_class_weights[b0:b0 + count]
    a_activity = a.activity_pattern[a0:a0 + count]
    b_activity = b.activity_pattern[b0:b0 + count]
    return (
        _harmonic_similarity(a_chords, b_chords),
        _melody_over_harmony_score(a_melody, b_chords),
        _melody_over_harmony_score(b_melody, a_chords),
        _activity_similarity(a_activity, b_activity),
    )


def _melody_over_harmony_score(melody_bars: list[dict[str, float]], harmony_tokens: list[str]) -> int:
    """Estimate how comfortably a normalized melody sits on another source's harmony.

    This is an editing/preparation indicator, not an artistic judgment. Chord tones
    score highest; common extensions retain useful credit; persistent semitone
    collisions lower the score. Both profiles are expressed relative to their tonic,
    so this remains meaningful after opening-key transposition.
    """
    if not melody_bars or not harmony_tokens:
        return 35
    samples = min(64, max(8, min(len(melody_bars), len(harmony_tokens))))
    earned = 0.0
    weight_total = 0.0
    for index in range(samples):
        melody = melody_bars[min(len(melody_bars) - 1, int(index * len(melody_bars) / samples))]
        token = harmony_tokens[min(len(harmony_tokens) - 1, int(index * len(harmony_tokens) / samples))]
        chord_pcs, root = _chord_pitch_classes(token)
        if not melody or root is None:
            continue
        for raw_pc, raw_weight in melody.items():
            try:
                pc = int(raw_pc) % 12
                weight = max(0.0, float(raw_weight))
            except (TypeError, ValueError):
                continue
            if pc in chord_pcs:
                value = 1.0
            else:
                distance = min((pc - chord_pc) % 12 for chord_pc in chord_pcs)
                circular = min(distance, 12 - distance)
                relative = (pc - root) % 12
                if relative in {2, 5, 9, 10, 11}:
                    value = 0.46  # possible extension, but not automatically comfortable
                elif circular == 1:
                    value = 0.08  # likely collision unless clearly prepared/resolved
                elif circular == 2:
                    value = 0.28
                else:
                    value = 0.18
            earned += value * weight
            weight_total += weight
    if weight_total <= 0:
        return 35
    return max(0, min(100, int(round(100.0 * earned / weight_total))))


def _detect_progression_family(tokens: list[str], bars: float, key_changes: list[tuple[float, str]]) -> str:
    clean = [token for token in tokens if token != "N"]
    if not clean:
        return "Unclassified"
    roots = [_token_root(token)[0] for token in clean]
    roots = [root for root in roots if root is not None]
    if len(roots) >= 12:
        expected = [0, 0, 0, 0, 5, 5, 0, 0, 7, 5, 0, 7]
        window = roots[:12]
        matches = sum(1 for a, b in zip(window, expected) if a == b)
        if matches >= 8:
            return "12-bar blues family"
    joined = ",".join(str(root) for root in roots)
    if any(pattern in joined for pattern in ("2,7,0", "2,7,0,0")):
        return "ii–V–I family"
    if any(pattern in joined for pattern in ("0,9,2,7", "9,2,7,0")) and bars >= 24:
        return "turnaround / rhythm-changes family"
    unique = len(set(roots[:32]))
    if unique <= 3 and len(roots) >= 8:
        return "three-chord cycle"
    if unique <= 5 and len(roots) >= 8:
        return "four-chord / compact cycle"
    if len(key_changes) >= 2 and bars >= 64:
        return "modulating strain form"
    return "functional mixed progression"


def _track_polyphony(intervals: list[_NoteInterval], track_index: int) -> tuple[int, float]:
    track_intervals = [item for item in intervals if item.track_index == track_index]
    if not track_intervals:
        return 0, 0.0
    events: list[tuple[int, int]] = []
    onset_counts: dict[int, int] = {}
    for item in track_intervals:
        events.append((item.start_tick, 1))
        events.append((item.end_tick, -1))
        onset_counts[item.start_tick] = onset_counts.get(item.start_tick, 0) + 1
    # Ends before starts at the same tick prevents false overlap at exact handoffs.
    active = 0
    maximum = 0
    for _tick, delta in sorted(events, key=lambda value: (value[0], value[1])):
        active += delta
        maximum = max(maximum, active)
    poly_onsets = sum(1 for value in onset_counts.values() if value > 1)
    ratio = poly_onsets / max(1, len(onset_counts))
    return maximum, ratio


def _bar_track_activity(
    intervals: list[_NoteInterval],
    track_indices: list[int],
    bar_ticks: int,
    bar_count: int,
) -> tuple[list[set[int]], dict[int, list[int]]]:
    active_sets: list[set[int]] = [set() for _ in range(max(1, bar_count))]
    onset_counts: dict[int, list[int]] = {index: [0] * max(1, bar_count) for index in track_indices}
    for item in intervals:
        if item.track_index not in onset_counts:
            continue
        onset_bar = max(0, min(bar_count - 1, int(item.start_tick // max(1, bar_ticks))))
        onset_counts[item.track_index][onset_bar] += 1
        first = max(0, int(item.start_tick // max(1, bar_ticks)))
        last = min(bar_count - 1, int(max(item.start_tick, item.end_tick - 1) // max(1, bar_ticks)))
        for bar in range(first, last + 1):
            active_sets[bar].add(item.track_index)
    return active_sets, onset_counts


def _bar_motif_signature(
    intervals: list[_NoteInterval],
    track_indices: set[int],
    bar: int,
    bar_ticks: int,
) -> set[tuple[int, int, int]]:
    start = bar * bar_ticks
    end = start + bar_ticks
    result: set[tuple[int, int, int]] = set()
    for item in intervals:
        if item.track_index not in track_indices or not (start <= item.start_tick < end):
            continue
        position = int(round(((item.start_tick - start) / max(1, bar_ticks)) * 16))
        pitch = item.note if item.channel == 9 else item.note % 12
        result.add((1 if item.channel == 9 else 0, pitch, max(0, min(15, position))))
    return result


def _jaccard(a: set, b: set) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / max(1, len(a | b))


def _detect_signature_assets(
    intervals: list[_NoteInterval],
    note_profiles: list[NoteTrackProfile],
    *,
    bar_ticks: int,
    bars: float,
    melody_track_index: int | None,
    accompaniment_track_index: int | None,
    melody_start_bar: float,
) -> tuple[list[dict], float]:
    """Detect a protected opening passage without pretending to judge fame.

    The detector looks for an isolated/repeated opening texture followed by a
    clear increase in instrumentation or the main melody. It returns
    "signature candidate" metadata; the user remains free to confirm or reject it.
    """
    bar_count = max(1, int(math.ceil(bars)))
    track_indices = [profile.index for profile in note_profiles if profile.note_count > 0]
    if not track_indices or bar_count < 2:
        return [], 0.0
    active_sets, onset_counts = _bar_track_activity(intervals, track_indices, bar_ticks, bar_count)
    first_nonempty = next((i for i, values in enumerate(active_sets[: min(16, bar_count)]) if values), None)
    if first_nonempty is None:
        return [], 0.0
    initial_set = set(active_sets[first_nonempty])
    initial_count = len(initial_set)
    max_opening_count = max((len(values) for values in active_sets[: min(24, bar_count)]), default=initial_count)
    change_bar: int | None = None
    for bar in range(first_nonempty + 1, min(16, bar_count)):
        current = active_sets[bar]
        next_set = active_sets[bar + 1] if bar + 1 < bar_count else current
        sustained = bool(current) and bool(next_set)
        new_roles = len(current - initial_set) >= 1
        fuller = len(current) >= max(2, initial_count + 1, int(math.ceil(max_opening_count * 0.60)))
        melody_arrives = melody_track_index is not None and melody_track_index in current and melody_track_index not in initial_set
        foundation_arrives = accompaniment_track_index is not None and accompaniment_track_index in current and accompaniment_track_index not in initial_set
        if sustained and (melody_arrives or (fuller and new_roles) or (foundation_arrives and new_roles)):
            change_bar = bar
            break
    melody_entry = int(round(max(0.0, melody_start_bar)))
    candidates = [value for value in (change_bar, melody_entry if melody_entry > first_nonempty else None) if value is not None]
    intro_end = min(candidates) if candidates else None
    if intro_end is None or intro_end <= first_nonempty or intro_end > 12:
        return [], float(melody_entry or first_nonempty)
    intro_tracks = set().union(*active_sets[first_nonempty:intro_end]) if intro_end > first_nonempty else initial_set
    if not intro_tracks:
        return [], float(intro_end)
    profiles_by_index = {item.index: item for item in note_profiles}
    percussion_tracks = {index for index in intro_tracks if any(ch == 9 for ch in profiles_by_index.get(index, NoteTrackProfile(index, '', 0, None, None, None, None)).channels)}
    pitched_tracks = intro_tracks - percussion_tracks
    total_onsets = sum(sum(onset_counts[index][first_nonempty:intro_end]) for index in intro_tracks)
    length = intro_end - first_nonempty
    bar_signatures = [_bar_motif_signature(intervals, intro_tracks, bar, bar_ticks) for bar in range(first_nonempty, intro_end)]
    repeat_similarity = 0.0
    if len(bar_signatures) >= 2:
        repeat_similarity = sum(_jaccard(bar_signatures[i], bar_signatures[i + 1]) for i in range(len(bar_signatures) - 1)) / (len(bar_signatures) - 1)
    return_similarity = 0.0
    if bar_signatures:
        seed = bar_signatures[0]
        later = [_bar_motif_signature(intervals, intro_tracks, bar, bar_ticks) for bar in range(intro_end, min(bar_count, intro_end + 48))]
        return_similarity = max((_jaccard(seed, item) for item in later), default=0.0)
    if intro_tracks == percussion_tracks:
        kind = "signature groove candidate"
        label = "Opening drum / percussion groove"
    elif len(pitched_tracks) <= 2 and len(intro_tracks) <= 3:
        kind = "signature riff candidate"
        label = "Opening riff or hook"
    else:
        kind = "composed intro candidate"
        label = "Composed opening passage"
    count_in = kind == "signature groove candidate" and length <= 1 and total_onsets <= 8 and repeat_similarity < 0.35
    confidence = 42
    confidence += 14 if 2 <= length <= 8 else 4
    confidence += 12 if initial_count <= 3 else 2
    confidence += int(round(repeat_similarity * 18))
    confidence += int(round(return_similarity * 14))
    confidence += 10 if melody_entry >= intro_end else 0
    confidence += 8 if change_bar is not None else 0
    if count_in:
        confidence -= 35
        kind = "count-in candidate"
        label = "Possible count-in"
    confidence = max(5, min(98, confidence))
    reasons = [
        f"The opening uses {len(intro_tracks)} track(s) before the main texture changes at bar {intro_end + 1}.",
        f"The passage lasts {length} bar(s) and is separated from the likely main melody entry.",
    ]
    if repeat_similarity >= 0.45:
        reasons.append("The opening contains a repeated rhythmic or melodic pattern.")
    if return_similarity >= 0.50:
        reasons.append("A related motif appears again later in the MIDI.")
    if percussion_tracks and not pitched_tracks:
        reasons.append("The opening is percussion-only, so it can be preserved without key transposition.")
    asset = {
        "asset_id": "opening-signature-1",
        "kind": kind,
        "label": label,
        "start_bar": int(first_nonempty),
        "end_bar": int(intro_end),
        "length_bars": int(length),
        "track_indices": sorted(intro_tracks),
        "percussion_track_indices": sorted(percussion_tracks),
        "pitched_track_indices": sorted(pitched_tracks),
        "confidence": int(confidence),
        "protect_default": bool(confidence >= 65 and not count_in),
        "companion_entry_bar": int(intro_end),
        "reasons": reasons,
    }
    return [asset], float(intro_end)




_MELODY_NAME_HINTS = ("melody", "lead", "solo", "theme", "vocal", "voice", "soprano", "trumpet", "sax", "upper", "right hand", "right-hand", "rh")
_ACCOMP_NAME_HINTS = ("accomp", "chord", "rhythm", "piano", "guitar", "pad", "strings", "lower", "left hand", "left-hand", "lh")
_BASS_NAME_HINTS = ("bass", "contrabass", "low strings", "tuba")
_HOOK_NAME_HINTS = ("hook", "riff", "motif", "fill")


def _program_family(program: int) -> str:
    value = max(0, min(127, int(program)))
    if 32 <= value <= 39:
        return "bass"
    # Choir Aahs, Voice Oohs, and Synth Voice often carry a sung or lead line.
    # Check them before the wider strings/ensemble family.
    if 52 <= value <= 54:
        return "voice"
    if 40 <= value <= 55:
        return "strings-ensemble"
    if 56 <= value <= 63:
        return "brass"
    if 64 <= value <= 71:
        return "reed"
    if 72 <= value <= 79:
        return "pipe"
    if 80 <= value <= 87:
        return "synth-lead"
    if 88 <= value <= 95:
        return "synth-pad"
    if 0 <= value <= 7:
        return "piano"
    if 24 <= value <= 31:
        return "guitar"
    if 8 <= value <= 15:
        return "chromatic"
    return "other"


def _musical_fingerprint(intervals: list["_NoteInterval"], melody_track_index: int | None, ticks_per_beat: int) -> str:
    """Create a tempo-, key-, filename-, and instrument-independent melody contour hash.

    It is used only to group likely alternate files of the same song. Rendering
    never uses this top-onset contour; full MIDI tracks remain intact.
    """
    if melody_track_index is None:
        return ""
    by_onset: dict[int, _NoteInterval] = {}
    for item in intervals:
        if item.track_index != melody_track_index or item.channel == 9:
            continue
        prior = by_onset.get(item.start_tick)
        if prior is None or item.note > prior.note:
            by_onset[item.start_tick] = item
    notes = [by_onset[tick] for tick in sorted(by_onset)][:384]
    if len(notes) < 16:
        return ""
    unit = max(1.0, float(ticks_per_beat) / 24.0)
    previous_tick = notes[0].start_tick
    previous_note = notes[0].note
    tokens: list[str] = []
    for item in notes:
        onset_delta = int(round((item.start_tick - previous_tick) / unit))
        duration = int(round((item.end_tick - item.start_tick) / unit))
        pitch_delta = int(item.note - previous_note)
        tokens.append(f"{max(0, min(255, onset_delta))}:{max(-48, min(48, pitch_delta))}:{max(1, min(255, duration))}")
        previous_tick = item.start_tick
        previous_note = item.note
    return hashlib.sha256("|".join(tokens).encode("ascii", errors="ignore")).hexdigest()[:24]


def _type2_duration_seconds(midi: mido.MidiFile) -> float:
    """Return a practical duration for asynchronous SMF Type-2 sequences.

    Mido correctly refuses ``MidiFile.length`` for Type 2 because the tracks are
    independent sequences rather than one shared timeline. For Library analysis
    we still need a bounded duration metric, so each sequence is timed against
    its own tempo changes and the longest sequence is reported. This preserves
    valid Type-2 files instead of treating a library/analyzer limitation as file
    corruption.
    """
    ticks_per_beat = max(1, int(midi.ticks_per_beat or 480))
    longest = 0.0
    for track in midi.tracks:
        tempo = 500000
        seconds = 0.0
        for msg in track:
            delta = max(0, int(getattr(msg, "time", 0) or 0))
            if delta:
                seconds += float(mido.tick2second(delta, ticks_per_beat, tempo))
            if msg.type == "set_tempo":
                tempo = int(msg.tempo)
        longest = max(longest, seconds)
    return longest


def analyze_midi(path: str | Path) -> MidiProfile:
    """Build a detailed musical profile within explicit resource budgets."""
    source = Path(path)
    validate_file(source, DEFAULT_MIDI_LIMITS)
    midi = mido.MidiFile(source)
    validate_decoded_midi(midi, DEFAULT_MIDI_LIMITS)
    budget = ProcessingBudget("deep", DEFAULT_MIDI_LIMITS)
    metadata = known_metadata(source)
    tempos: list[tuple[int, int]] = []
    times: list[tuple[int, int, int]] = []
    keys: list[tuple[int, str]] = []
    total_ticks = 0
    note_tracks_raw: list[dict] = []
    intervals: list[_NoteInterval] = []

    for index, track in enumerate(midi.tracks):
        abs_tick = 0
        name = ""
        notes: list[int] = []
        velocities: list[int] = []
        channels: set[int] = set()
        programs: set[int] = set()
        active: dict[tuple[int, int], tuple[int, int]] = {}
        for msg in track:
            abs_tick += int(msg.time)
            total_ticks = max(total_ticks, abs_tick)
            budget.add_event(note=msg.type in {"note_on", "note_off"}, total_ticks=total_ticks)
            if msg.type == "track_name":
                name = str(msg.name)
            elif msg.type == "set_tempo":
                tempos.append((abs_tick, int(msg.tempo)))
            elif msg.type == "time_signature":
                times.append((abs_tick, int(msg.numerator), int(msg.denominator)))
            elif msg.type == "key_signature":
                keys.append((abs_tick, str(msg.key)))
            elif msg.type == "program_change":
                programs.add(int(msg.program)); channels.add(int(msg.channel))
            elif msg.type == "note_on":
                channel = int(msg.channel); note = int(msg.note); velocity = int(msg.velocity)
                channels.add(channel)
                key = (channel, note)
                if velocity > 0:
                    notes.append(note)
                    velocities.append(velocity)
                    prior = active.pop(key, None)
                    if prior is not None:
                        intervals.append(_NoteInterval(index, channel, note, prior[1], prior[0], max(prior[0] + 1, abs_tick)))
                    active[key] = (abs_tick, velocity)
                else:
                    start = active.pop(key, None)
                    if start is not None:
                        intervals.append(_NoteInterval(index, channel, note, start[1], start[0], max(start[0] + 1, abs_tick)))
            elif msg.type == "note_off":
                channel = int(msg.channel); note = int(msg.note); channels.add(channel)
                start = active.pop((channel, note), None)
                if start is not None:
                    intervals.append(_NoteInterval(index, channel, note, start[1], start[0], max(start[0] + 1, abs_tick)))
        for (channel, note), (start_tick, velocity) in active.items():
            intervals.append(_NoteInterval(index, channel, note, velocity, start_tick, max(start_tick + 1, total_ticks)))
        note_tracks_raw.append({
            "index": index,
            "name": name,
            "notes": notes,
            "velocities": velocities,
            "channels": sorted(channels),
            "programs": sorted(programs),
        })

    tempos.sort(key=lambda item: item[0])
    times.sort(key=lambda item: item[0])
    keys.sort(key=lambda item: item[0])
    initial_tempo = tempos[0][1] if tempos else mido.bpm2tempo(120)
    bpm = float(mido.tempo2bpm(initial_tempo))
    numerator, denominator = (times[0][1], times[0][2]) if times else (4, 4)
    duration_seconds = _type2_duration_seconds(midi) if int(midi.type) == 2 else float(midi.length)
    budget.validate_duration(duration_seconds)
    first_key = keys[0][1] if keys else None
    declared_pc, declared_mode = parse_key_signature(first_key)

    weighted_notes = [(item.note, (item.end_tick - item.start_tick) * max(0.2, item.velocity / 100.0)) for item in intervals]
    analyzed_pc, analyzed_mode, key_confidence = _estimate_key_from_notes(weighted_notes)
    tonic_pc = declared_pc if declared_pc is not None else analyzed_pc
    mode = declared_mode if declared_pc is not None else analyzed_mode
    if declared_pc is None:
        key_agreement = "no declared key"
    elif declared_pc == analyzed_pc and declared_mode == analyzed_mode:
        key_agreement = "agrees"
    elif declared_pc == analyzed_pc:
        key_agreement = "same tonic, mode differs"
    elif analyzed_pc is not None and (declared_pc - analyzed_pc) % 12 in {3, 9}:
        key_agreement = "relative-key relationship"
    else:
        key_agreement = "differs"

    playable = [item for item in note_tracks_raw if item["notes"] and 9 not in item["channels"]]

    # Track roles are scored from names, General MIDI programs, register, note
    # density, and polyphony. Highest pitch alone is never treated as proof of
    # melody, and the lower part is not assumed to be the complete backing.
    track_metrics: dict[int, dict] = {}
    pitch_means = [statistics.mean(item["notes"]) for item in playable] or [60.0]
    pitch_low, pitch_high = min(pitch_means), max(pitch_means)
    pitch_span = max(1.0, pitch_high - pitch_low)
    for item in note_tracks_raw:
        notes = item["notes"]
        max_polyphony, poly_ratio = _track_polyphony(intervals, item["index"])
        monophonic_score = int(round(max(0.0, min(100.0, 100.0 - poly_ratio * 140.0 - max(0, max_polyphony - 1) * 7.0)))) if notes else 0
        mean_note = statistics.mean(notes) if notes else 0.0
        register = (mean_note - pitch_low) / pitch_span if notes else 0.0
        name = str(item["name"] or "").casefold()
        families = {_program_family(program) for program in item["programs"]}
        is_drum = bool(notes and 9 in item["channels"])
        melody_score = monophonic_score * 0.42 + register * 22.0 + min(16.0, len(notes) / 80.0)
        accompaniment_score = (100 - monophonic_score) * 0.22 + min(24.0, len(notes) / 45.0) + (1.0 - register) * 8.0
        bass_score = (1.0 - register) * 35.0 + min(15.0, len(notes) / 80.0)
        reasons: list[str] = []
        if any(word in name for word in _MELODY_NAME_HINTS):
            melody_score += 55; reasons.append("track name suggests a lead or melody")
        if any(word in name for word in _ACCOMP_NAME_HINTS):
            accompaniment_score += 45; melody_score -= 25; reasons.append("track name suggests chords or accompaniment")
        if any(word in name for word in _BASS_NAME_HINTS):
            bass_score += 60; melody_score -= 50; reasons.append("track name suggests bass")
        if any(word in name for word in _HOOK_NAME_HINTS):
            melody_score += 20; reasons.append("track name suggests a hook or riff")
        if families.intersection({"brass", "reed", "pipe", "synth-lead", "voice"}):
            melody_score += 24; reasons.append("instrument family commonly carries a lead")
        if "bass" in families:
            bass_score += 70; melody_score -= 55; reasons.append("General MIDI program is a bass instrument")
        if families.intersection({"piano", "guitar", "strings-ensemble", "synth-pad"}):
            accompaniment_score += 22; reasons.append("instrument family commonly supports harmony")
        if "synth-pad" in families:
            melody_score -= 28
        if max_polyphony >= 4 or poly_ratio >= 0.35:
            accompaniment_score += 28; melody_score -= 22; reasons.append("polyphony suggests chordal or ensemble material")
        if is_drum:
            melody_score = accompaniment_score = bass_score = -999.0
        track_metrics[item["index"]] = {
            "max_polyphony": max_polyphony, "poly_ratio": poly_ratio, "monophonic_score": monophonic_score,
            "melody_score": melody_score, "accompaniment_score": accompaniment_score, "bass_score": bass_score,
            "families": families, "reasons": reasons, "mean_note": mean_note, "register": register,
        }

    melody_candidates = [item for item in playable if track_metrics[item["index"]]["melody_score"] > -900]
    melody_index = max(melody_candidates, key=lambda item: track_metrics[item["index"]]["melody_score"])["index"] if melody_candidates else None
    accompaniment_candidates = [item for item in playable if item["index"] != melody_index]
    accompaniment_index = max(
        accompaniment_candidates,
        key=lambda item: track_metrics[item["index"]]["accompaniment_score"] + min(25.0, track_metrics[item["index"]]["bass_score"] * 0.25),
    )["index"] if accompaniment_candidates else melody_index

    note_profiles: list[NoteTrackProfile] = []
    for item in note_tracks_raw:
        notes = item["notes"]
        velocities = item["velocities"]
        metrics = track_metrics[item["index"]]
        max_polyphony = int(metrics["max_polyphony"]); poly_ratio = float(metrics["poly_ratio"]); monophonic_score = int(metrics["monophonic_score"])
        families = set(metrics["families"]); name = str(item["name"] or "").casefold()
        role = "control"
        confidence = 0
        role_reasons = list(metrics["reasons"])
        if notes:
            if 9 in item["channels"]:
                role = "drums / percussion"; confidence = 100; role_reasons = ["General MIDI percussion channel 10"]
            elif item["index"] == melody_index and item["index"] == accompaniment_index:
                role = "combined melody and accompaniment"; confidence = max(20, monophonic_score // 2)
            elif item["index"] == melody_index:
                role = "lead melody"; confidence = max(35, min(98, int(round(metrics["melody_score"]))))
            elif "bass" in families or any(word in name for word in _BASS_NAME_HINTS) or metrics["bass_score"] >= 70:
                role = "bass"; confidence = max(50, min(98, int(round(metrics["bass_score"]))))
            elif item["index"] == accompaniment_index:
                role = "chords / accompaniment"; confidence = max(35, min(98, int(round(metrics["accompaniment_score"]))))
            elif any(word in name for word in _HOOK_NAME_HINTS):
                role = "hook / riff"; confidence = 70
            elif families.intersection({"strings-ensemble", "synth-pad"}):
                role = "supporting harmony"; confidence = 65
            else:
                role = "supporting part"; confidence = 50
        safe_solo = bool(notes and max_polyphony <= 1 and poly_ratio <= 0.03 and role == "lead melody")
        if safe_solo:
            advice = "Suitable for a solo-instrument patch without voice extraction."
        elif notes and role == "lead melody" and max_polyphony <= 3 and poly_ratio < 0.25:
            advice = "Mostly melodic; keep the complete track by default and extract a top voice only for a deliberate solo-instrument reassignment."
        elif notes and role == "lead melody":
            advice = "Polyphonic lead candidate: keep its complete musical part unless the user explicitly requests voice extraction."
        elif notes:
            advice = "Keep this complete supporting track unless the user deliberately excludes or remaps it."
        else:
            advice = "No pitched note material."
        programs = item["programs"]
        program_names = [gm_program_name(value) for value in programs]
        note_profiles.append(NoteTrackProfile(
            index=item["index"], name=item["name"], note_count=len(notes),
            min_note=min(notes) if notes else None, max_note=max(notes) if notes else None,
            mean_note=round(statistics.mean(notes), 2) if notes else None, median_note=round(statistics.median(notes), 2) if notes else None,
            channels=item["channels"], programs=programs, likely_role=role,
            median_velocity=round(statistics.median(velocities), 1) if velocities else None,
            velocity_low=min(velocities) if velocities else None, velocity_high=max(velocities) if velocities else None,
            max_polyphony=max_polyphony, polyphonic_onset_ratio=round(poly_ratio, 3), monophonic_score=monophonic_score,
            program_names=program_names, safe_for_solo_instrument=safe_solo, instrumentation_advice=advice,
            role_confidence=confidence, role_reasons=role_reasons[:4],
        ))

    qbeats = total_ticks / float(max(1, midi.ticks_per_beat))
    qbeats_bar = quarter_beats_per_bar(numerator, denominator)
    bars = qbeats / qbeats_bar if qbeats_bar else 0.0
    budget.validate_bars(bars)
    bar_ticks = max(1, int(round(midi.ticks_per_beat * qbeats_bar)))
    key_changes = [(round(tick / max(1.0, bar_ticks), 2), key) for tick, key in keys]

    section_keys: list[dict] = []
    section_size = 8
    for start_bar in range(0, max(1, int(math.ceil(bars))), section_size):
        start_tick = start_bar * bar_ticks
        end_tick = min(total_ticks, (start_bar + section_size) * bar_ticks)
        pc, section_mode, confidence = _estimate_key_from_notes(_interval_weights(intervals, start_tick, end_tick))
        section_keys.append({
            "start_bar": start_bar + 1,
            "end_bar": min(int(math.ceil(bars)), start_bar + section_size),
            "key": key_name(pc, section_mode),
            "tonic_pc": pc,
            "mode": section_mode,
            "confidence": confidence,
        })

    chord_fingerprint: list[str] = []
    melody_pitch_class_weights: list[dict[str, float]] = []
    melody_onsets: dict[int, _NoteInterval] = {}
    if melody_index is not None:
        for item in intervals:
            if item.track_index != melody_index:
                continue
            previous = melody_onsets.get(item.start_tick)
            if previous is None or item.note > previous.note:
                melody_onsets[item.start_tick] = item
    top_voice_intervals = list(melody_onsets.values())
    for bar in range(max(1, int(math.ceil(bars)))):
        start_tick = bar * bar_ticks
        end_tick = min(total_ticks, (bar + 1) * bar_ticks)
        chord_fingerprint.append(_detect_chord_token(_interval_weights(intervals, start_tick, end_tick), tonic_pc, mode))
        melody_histogram: dict[str, float] = {}
        if melody_index is not None:
            for item in top_voice_intervals:
                overlap = max(0, min(item.end_tick, end_tick) - max(item.start_tick, start_tick))
                if overlap <= 0:
                    continue
                relative_pc = item.note % 12 if tonic_pc is None else (item.note - tonic_pc) % 12
                key = str(relative_pc)
                melody_histogram[key] = melody_histogram.get(key, 0.0) + overlap * max(0.2, item.velocity / 100.0)
        melody_pitch_class_weights.append({key: round(value, 3) for key, value in melody_histogram.items()})

    playable_profiles = [profile for profile in note_profiles if profile.note_count and "percussion" not in profile.likely_role]
    distinct_programs = {program for profile in playable_profiles for program in profile.programs}
    piano_like = not distinct_programs or all(0 <= value <= 7 for value in distinct_programs)
    if len(playable_profiles) <= 1:
        arrangement_type = "single-track performance"
    elif len(playable_profiles) == 2 and piano_like:
        arrangement_type = "piano reduction"
    elif len(playable_profiles) >= 3 and len(distinct_programs) >= 2:
        arrangement_type = "multitrack arrangement"
    elif piano_like:
        arrangement_type = "keyboard reduction"
    else:
        arrangement_type = "compact ensemble arrangement"

    melody_profile = next((item for item in note_profiles if item.index == melody_index), None)
    melody_confidence = melody_profile.monophonic_score if melody_profile else 0
    base_ready = 75 if arrangement_type == "multitrack arrangement" else 58 if arrangement_type == "compact ensemble arrangement" else 38 if "reduction" in arrangement_type else 30
    orchestration_readiness = max(0, min(100, int(round(base_ready * 0.55 + melody_confidence * 0.45))))
    # A reduction may have clearly separated upper/lower hands yet still hide
    # inner voices inside one track. Keep it below multitrack confidence so the
    # UI never overstates how safely individual instruments can be reassigned.
    if "reduction" in arrangement_type:
        orchestration_readiness = min(orchestration_readiness, 58)

    all_velocities = [item.velocity for item in intervals]
    dynamics_median = round(statistics.median(all_velocities), 1) if all_velocities else None
    dynamics_low = min(all_velocities) if all_velocities else None
    dynamics_high = max(all_velocities) if all_velocities else None
    progression_family = _detect_progression_family(chord_fingerprint, bars, key_changes)

    # Coarse musical-entry and activity information is used by Guided Remix to
    # avoid placing one song's intro against another song's first theme.
    melody_intervals = [item for item in intervals if item.track_index == melody_index] if melody_index is not None else []
    accompaniment_intervals = [item for item in intervals if item.track_index == accompaniment_index] if accompaniment_index is not None else []
    melody_start_bar = (min((item.start_tick for item in melody_intervals), default=0) / bar_ticks) if bar_ticks else 0.0
    accompaniment_start_bar = (min((item.start_tick for item in accompaniment_intervals), default=0) / bar_ticks) if bar_ticks else 0.0
    activity_pattern: list[float] = []
    for bar in range(max(1, int(math.ceil(bars)))):
        start_tick = bar * bar_ticks
        end_tick = min(total_ticks, (bar + 1) * bar_ticks)
        onset_count = sum(1 for item in intervals if start_tick <= item.start_tick < end_tick)
        sounding = sum(max(0, min(item.end_tick, end_tick) - max(item.start_tick, start_tick)) for item in intervals)
        activity_pattern.append(round(onset_count + sounding / max(1.0, bar_ticks), 3))

    signature_assets, main_entry_bar = _detect_signature_assets(
        intervals, note_profiles, bar_ticks=bar_ticks, bars=bars,
        melody_track_index=melody_index, accompaniment_track_index=accompaniment_index,
        melody_start_bar=melody_start_bar,
    )

    warnings: list[str] = []
    if len(playable_profiles) < 2:
        warnings.append("Fewer than two playable pitched tracks; role swapping will be limited.")
    if len(tempos) > 1:
        warnings.append(f"Contains {len(tempos)} tempo events; target-tempo normalization currently uses one shared pulse.")
    if len(keys) > 1:
        warnings.append("Contains key changes; evaluate section keys before assuming one global transposition will fit every strain.")
    if key_agreement in {"differs", "same tonic, mode differs"}:
        warnings.append(f"Declared key {first_key or 'none'} and note-distribution estimate {key_name(analyzed_pc, analyzed_mode)} do not fully agree.")
    if arrangement_type in {"piano reduction", "keyboard reduction"}:
        warnings.append("Piano/keyboard reduction: the upper part may contain melody plus inner voices. Do not assign it directly to a solo wind or brass patch without checking polyphony.")
    if melody_profile and not melody_profile.safe_for_solo_instrument:
        warnings.append("Likely melody track is polyphonic; top-voice extraction is recommended for solo-instrument reassignment.")
    musical_fingerprint = _musical_fingerprint(intervals, melody_index, int(midi.ticks_per_beat))

    protected_signature = next((item for item in signature_assets if item.get("protect_default")), None)
    if protected_signature:
        warnings.append(
            f"Likely signature opening detected in bars {int(protected_signature['start_bar']) + 1}-{int(protected_signature['end_bar'])}; preserve it separately from the main alignment point."
        )

    return MidiProfile(
        path=str(source.resolve()),
        fingerprint=_fingerprint(source),
        title=metadata["title"],
        composer=metadata["composer"],
        style=metadata["style"],
        midi_type=int(midi.type),
        ticks_per_beat=int(midi.ticks_per_beat),
        track_count=len(midi.tracks),
        note_count=sum(len(item["notes"]) for item in note_tracks_raw),
        duration_seconds=duration_seconds,
        total_ticks=int(total_ticks),
        tempo_bpm=round(bpm, 3),
        felt_bpm=felt_bpm_for_meter(bpm, numerator, denominator),
        tempo_change_count=len(tempos),
        numerator=numerator,
        denominator=denominator,
        meter=f"{numerator}/{denominator}",
        meter_family=meter_family(numerator, denominator),
        bars=round(bars, 3),
        initial_key_signature=first_key,
        key_changes=key_changes,
        tonic_pc=tonic_pc,
        mode=mode,
        note_tracks=note_profiles,
        melody_track_index=melody_index,
        accompaniment_track_index=accompaniment_index,
        warnings=warnings,
        declared_tonic_pc=declared_pc,
        declared_mode=declared_mode,
        analyzed_tonic_pc=analyzed_pc,
        analyzed_mode=analyzed_mode,
        key_confidence=key_confidence,
        key_agreement=key_agreement,
        section_keys=section_keys,
        chord_fingerprint=chord_fingerprint,
        melody_pitch_class_weights=melody_pitch_class_weights,
        arrangement_type=arrangement_type,
        orchestration_readiness=orchestration_readiness,
        melody_extraction_confidence=melody_confidence,
        dynamics_median=dynamics_median,
        dynamics_low=dynamics_low,
        dynamics_high=dynamics_high,
        progression_family=progression_family,
        reference_tags=[],
        content_fingerprint=_content_fingerprint(source),
        song_identity=canonical_song_identity(metadata["title"], source),
        signature_assets=signature_assets,
        main_entry_bar=round(float(main_entry_bar), 3),
        melody_start_bar=round(float(melody_start_bar), 3),
        accompaniment_start_bar=round(float(accompaniment_start_bar), 3),
        activity_pattern=activity_pattern,
        musical_fingerprint=musical_fingerprint,
    )


def _assess_pair_uncached(a: MidiProfile, b: MidiProfile) -> PairAssessment:
    felt_a = a.felt_bpm or 120.0
    felt_b = b.felt_bpm or 120.0
    tempo_options = ((felt_b, "direct"), (felt_b / 2.0, "candidate half-time"), (felt_b * 2.0, "candidate double-time"))
    aligned_b, tempo_relationship = min(tempo_options, key=lambda item: abs(felt_a - item[0]))
    diff = abs(felt_a - aligned_b)
    relationship_penalty = 0 if tempo_relationship == "direct" else 6
    tempo_score = max(0, int(round(100 - diff * 2.0 - relationship_penalty)))
    if a.meter == b.meter:
        meter_score = 100
    elif a.meter_family == b.meter_family:
        meter_score = 86
    elif {a.meter_family, b.meter_family} == {"simple duple", "compound duple"}:
        meter_score = 66
    else:
        meter_score = 40
    ratio = min(a.bars, b.bars) / max(1.0, max(a.bars, b.bars))
    length_score = int(round(52 + 48 * ratio))
    role_score = 100 if all(v is not None for v in (a.melody_track_index, a.accompaniment_track_index, b.melody_track_index, b.accompaniment_track_index)) else 55
    orchestration_score = int(round((a.orchestration_readiness + b.orchestration_readiness) / 2.0))

    shift = signed_pitch_class_shift(b.tonic_pc, a.tonic_pc)
    if a.mode == b.mode:
        key_score = max(45, 100 - abs(shift) * 7)
    elif a.tonic_pc == b.tonic_pc:
        key_score = 62
    elif a.tonic_pc is not None and b.tonic_pc is not None and (a.tonic_pc - b.tonic_pc) % 12 in {3, 9}:
        key_score = 78
    else:
        key_score = max(30, 82 - abs(shift) * 6)
    if a.key_agreement == "differs" or b.key_agreement == "differs":
        key_score = max(20, key_score - 12)

    harmonic_score = _harmonic_similarity(a.chord_fingerprint, b.chord_fingerprint)
    melody_a_over_b = _melody_over_harmony_score(a.melody_pitch_class_weights, b.chord_fingerprint)
    melody_b_over_a = _melody_over_harmony_score(b.melody_pitch_class_weights, a.chord_fingerprint)
    if a.progression_family == b.progression_family and a.progression_family != "Unclassified":
        harmonic_score = min(100, harmonic_score + 8)
    cross_fit = int(round((melody_a_over_b + melody_b_over_a) / 2.0))
    entry_delta = abs(float(a.melody_start_bar) - float(b.melody_start_bar))
    entry_alignment_score = max(0, min(100, int(round(100 - entry_delta * 18))))
    structural_alignment_score = _activity_similarity(a.activity_pattern[:48], b.activity_pattern[:48])

    # Preparation estimates how much normalization/editing is likely. It is not an artistic-quality score.
    preparation = int(round(
        tempo_score * 0.13 + meter_score * 0.10 + length_score * 0.06 + role_score * 0.09 +
        key_score * 0.13 + harmonic_score * 0.17 + cross_fit * 0.14 + orchestration_score * 0.06 +
        entry_alignment_score * 0.06 + structural_alignment_score * 0.06
    ))
    relationship = "same style" if a.style == b.style and a.style != "Unclassified" else "cross-style"
    target = int(round((felt_a + aligned_b) / 2.0))

    if harmonic_score >= 72 and cross_fit >= 70 and meter_score >= 70:
        pairing_category = "harmonically related"
        adaptation = "light"
    elif harmonic_score >= 52 and cross_fit >= 55 and preparation >= 62:
        pairing_category = "transformable relationship"
        adaptation = "moderate"
    elif meter_score >= 70 or length_score >= 85:
        pairing_category = "structural bridge"
        adaptation = "substantial"
    else:
        pairing_category = "experimental pairing"
        adaptation = "substantial"

    observations = [
        f"Felt-pulse relationship: {tempo_relationship}; aligned difference {diff:.1f} BPM. Both sources can be written to the shared target pulse.",
        f"Meters: {a.meter_display} and {b.meter_display} ({a.meter_family} / {b.meter_family}).",
        f"Opening-key alignment moves source B {shift:+d} semitone(s) toward source A, but section modulations still require checking.",
        f"Declared/analyzed keys: A {a.key_display} / {a.analyzed_key_display}; B {b.key_display} / {b.analyzed_key_display}.",
        f"Bar-level harmonic-function similarity is approximately {harmonic_score}% after key normalization.",
        f"Cross-fit estimate: A melody over B harmony {melody_a_over_b}%; B melody over A harmony {melody_b_over_a}%.",
        f"Approximate lengths: {a.bars:.1f} and {b.bars:.1f} bars.",
        f"Melody entry bars: {a.melody_start_bar:.1f} and {b.melody_start_bar:.1f}; entry alignment {entry_alignment_score}%.",
        f"Coarse activity/structure alignment: {structural_alignment_score}%.",
        f"Orchestration readiness: A {a.orchestration_readiness}%, B {b.orchestration_readiness}%.",
    ]
    if relationship == "same style":
        observations.append("The sources share a style label; recognition, counterpoint, role exchange, and form can still make the combination surprising.")
    else:
        observations.append("The sources carry different style labels; rhythm, phrase length, and functional harmony provide possible bridges.")
    if a.arrangement_type in {"piano reduction", "keyboard reduction"} or b.arrangement_type in {"piano reduction", "keyboard reduction"}:
        observations.append("At least one source is a keyboard reduction. Use top-voice extraction before assigning its upper part to a solo instrument.")

    shared_family = a.progression_family if a.progression_family == b.progression_family else None
    return PairAssessment(
        source_a=a.path,
        source_b=b.path,
        title_a=a.title,
        title_b=b.title,
        relationship=relationship,
        preparation_score=max(0, min(100, preparation)),
        tempo_score=max(0, min(100, tempo_score)),
        meter_score=max(0, min(100, meter_score)),
        length_score=max(0, min(100, length_score)),
        role_score=max(0, min(100, role_score)),
        key_shift_b_to_a=shift,
        target_felt_bpm=max(30, min(300, target)),
        observations=observations,
        tempo_relationship=tempo_relationship,
        key_score=max(0, min(100, key_score)),
        harmonic_score=max(0, min(100, harmonic_score)),
        orchestration_score=max(0, min(100, orchestration_score)),
        adaptation_required=adaptation,
        pairing_category=pairing_category,
        documented_reference=None,
        shared_progression_family=shared_family,
        melody_a_over_b_score=max(0, min(100, melody_a_over_b)),
        melody_b_over_a_score=max(0, min(100, melody_b_over_a)),
        entry_alignment_score=max(0, min(100, entry_alignment_score)),
        structural_alignment_score=max(0, min(100, structural_alignment_score)),
    )


def assess_pair(a: MidiProfile, b: MidiProfile) -> PairAssessment:
    """Assess a pair, reusing versioned derived results when possible."""
    cache = get_pair_assessment_cache()
    payload = cache.get(a, b)
    if payload is not None:
        for key in ("reference_evidence", "reference_sources", "reference_urls"):
            if isinstance(payload.get(key), list):
                payload[key] = tuple(payload[key])
        try:
            return PairAssessment(**payload)
        except (TypeError, ValueError):
            # A corrupt or older row is ignored and replaced below.
            payload = None
    assessment = _assess_pair_uncached(a, b)
    cache.put(a, b, assessment.to_dict())
    return assessment


def is_generated_remix(path: str | Path) -> bool:
    """Return True for MIDI files written by this application."""
    try:
        midi = mido.MidiFile(path, clip=True)
        for track in midi.tracks[:2]:
            for message in track[:32]:
                if message.type in {"text", "track_name"}:
                    text = str(getattr(message, "text", "") or getattr(message, "name", ""))
                    if "generated by midi remixer" in text.lower() or "midi remixer conductor" in text.lower():
                        return True
    except (OSError, EOFError, ValueError, TypeError, KeyError):
        return False
    return False
