from __future__ import annotations

from dataclasses import dataclass
import csv
import gzip
import json
from pathlib import Path
from typing import Iterable, Sequence
import unicodedata

from core.app_paths import AppPaths

MAX_REFERENCE_FILE_BYTES = 100 * 1024 * 1024
MAX_REFERENCE_FILES = 100
MAX_REFERENCE_ROWS = 100_000


def normalize_title(value: str) -> str:
    """Return a conservative identity key for artist/title reference matching."""
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.replace("&", " and ")
    return "".join(ch.lower() for ch in text if ch.isalnum())


def _split_tags(value: object) -> tuple[str, ...]:
    if isinstance(value, (list, tuple, set)):
        return tuple(str(item).strip() for item in value if str(item).strip())
    text = str(value or "").strip()
    if not text:
        return ()
    separator = ";" if ";" in text else ","
    return tuple(part.strip() for part in text.split(separator) if part.strip())


def _safe_int(value: object, default: int = 0) -> int:
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class PairingReference:
    mashup_artist: str
    mashup_title: str
    source_titles: tuple[str, ...]
    source_artists: tuple[str, ...]
    relationship_tags: tuple[str, ...]
    source_url: str
    provider: str = "Documented mashup archive"
    evidence_type: str = "human-created mashup"
    confidence: int = 90

    @property
    def display(self) -> str:
        return f"Documented human pairing: {self.mashup_artist} — {self.mashup_title}"


@dataclass(frozen=True)
class JazzRelationship:
    relationship: str
    source_title: str
    related_title: str
    progression_family: str
    source_url: str
    provider: str = "Jazz relationship reference"
    confidence: int = 75


@dataclass(frozen=True)
class ExternalCandidateReference:
    provider: str
    anchor_title: str
    candidate_title: str
    anchor_artist: str = ""
    candidate_artist: str = ""
    evidence_type: str = "external candidate suggestion"
    relationship_tags: tuple[str, ...] = ()
    source_url: str = ""
    confidence: int = 45
    note: str = ""

    @property
    def display(self) -> str:
        return f"{self.provider}: {self.anchor_title} ↔ {self.candidate_title}"


@dataclass(frozen=True)
class SongPopularityReference:
    artist: str
    title: str
    rank: int = 0
    year: int = 0
    familiarity_score: int = 0
    provider: str = "Imported popularity/familiarity list"
    source_url: str = ""

    @property
    def label(self) -> str:
        details: list[str] = []
        if self.rank > 0:
            details.append(f"rank {self.rank}")
        if self.year > 0:
            details.append(str(self.year))
        suffix = f" ({', '.join(details)})" if details else ""
        return f"{self.artist} — {self.title}{suffix}".strip(" —")


@dataclass(frozen=True)
class PairEvidence:
    score: int = 0
    familiarity_score: int = 0
    labels: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()
    providers: tuple[str, ...] = ()
    source_urls: tuple[str, ...] = ()
    documented_reference: str | None = None
    pairing_category: str | None = None
    progression_family: str | None = None

    @property
    def has_evidence(self) -> bool:
        return bool(self.labels or self.reasons or self.familiarity_score)


class ReferenceCatalog:
    """Metadata-only reference evidence used as a bounded ranking tie-breaker.

    The actual MIDI files remain authoritative. Human mashup precedents, similar-song
    lists, key/BPM services, and popularity spreadsheets may help the engine discover
    or explain a candidate, but they never raise tempo, key, harmony, meter, role, or
    structural analysis scores.

    Supported inputs:
    - the bundled ``known_pairings.json``;
    - optional ``popularity_catalog.json(.gz)``;
    - optional JSON/CSV files inside ``data/reference_imports``.

    This loader performs no live scraping. It accepts metadata exported or curated
    through a permitted workflow and records each provider for transparent UI labels.
    """

    def __init__(
        self,
        data_path: str | Path,
        *,
        import_directories: Iterable[str | Path] | None = None,
    ) -> None:
        self.data_path = Path(data_path)
        self.data_root = self.data_path.parent if self.data_path.suffix else self.data_path
        if import_directories is None:
            app_root = self.data_root.parent if self.data_root.name == "data" else self.data_root
            import_directories = (AppPaths.for_app(app_root).reference_imports,)
        self.import_directories = tuple(Path(value) for value in import_directories)
        self.load_warnings: list[str] = []
        self._loaded_rows = 0
        self.pairings: list[PairingReference] = []
        self.jazz_relationships: list[JazzRelationship] = []
        self.external_candidates: list[ExternalCandidateReference] = []
        self.popularity: list[SongPopularityReference] = []
        self._load()

    # ---------- loading ----------
    def _load(self) -> None:
        if self.data_path.is_file():
            self._load_path(self.data_path)
        elif self.data_path.is_dir():
            bundled = self.data_path / "known_pairings.json"
            if bundled.is_file():
                self._load_path(bundled)

        # Optional bundled/runtime enrichments. Missing files are normal.
        for name in ("popularity_catalog.json.gz", "popularity_catalog.json", "reference_evidence.json"):
            path = self.data_root / name
            if path.is_file() and path.resolve() != self.data_path.resolve():
                self._load_path(path)

        imported_files = 0
        for imports in self.import_directories:
            if not imports.is_dir():
                continue
            for path in sorted(imports.iterdir()):
                if imported_files >= MAX_REFERENCE_FILES:
                    self.load_warnings.append(f"Reference import limit reached ({MAX_REFERENCE_FILES} files).")
                    break
                if path.name.casefold().startswith("sample"):
                    continue
                if path.suffix.casefold() in {".json", ".csv", ".gz"}:
                    imported_files += 1
                    self._load_path(path)

        self._deduplicate()

    def _load_path(self, path: Path) -> None:
        try:
            if path.stat().st_size > MAX_REFERENCE_FILE_BYTES:
                self.load_warnings.append(f"Skipped oversized reference file: {path.name}")
                return
            if path.suffix.casefold() == ".csv":
                with path.open("r", encoding="utf-8-sig", newline="") as fh:
                    self._load_rows(csv.DictReader(fh), source_name=path.stem)
                return
            opener = gzip.open if path.suffix.casefold() == ".gz" else open
            with opener(path, "rt", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, UnicodeError, json.JSONDecodeError, gzip.BadGzipFile, ValueError) as exc:
            self.load_warnings.append(f"Skipped {path.name}: {type(exc).__name__}: {exc}")
            return

        if isinstance(data, list):
            self._load_rows(data, source_name=path.stem)
            return
        if not isinstance(data, dict):
            return
        self._load_mapping(data, source_name=path.stem)

    def _load_mapping(self, data: dict, *, source_name: str) -> None:
        default_provider = str(data.get("provider") or data.get("source_name") or source_name)
        for item in data.get("pairings", []):
            if not isinstance(item, dict):
                continue
            sources = item.get("sources", [])
            titles = tuple(str(source.get("title", "")) for source in sources if isinstance(source, dict))
            artists = tuple(str(source.get("artist", "")) for source in sources if isinstance(source, dict))
            if len([title for title in titles if title.strip()]) < 2:
                continue
            self.pairings.append(PairingReference(
                mashup_artist=str(item.get("mashup_artist", "Unknown")),
                mashup_title=str(item.get("mashup_title", "Untitled")),
                source_titles=titles,
                source_artists=artists,
                relationship_tags=_split_tags(item.get("relationship_tags", [])),
                source_url=str(item.get("source_url", "")),
                provider=str(item.get("provider") or default_provider or "Documented mashup archive"),
                evidence_type=str(item.get("evidence_type") or "human-created mashup"),
                confidence=max(0, min(100, _safe_int(item.get("confidence"), 90))),
            ))
        for item in data.get("jazz_relationships", []):
            if not isinstance(item, dict):
                continue
            self.jazz_relationships.append(JazzRelationship(
                relationship=str(item.get("relationship", "relationship")),
                source_title=str(item.get("source_title", "")),
                related_title=str(item.get("related_title", "")),
                progression_family=str(item.get("progression_family", "")),
                source_url=str(item.get("source_url", "")),
                provider=str(item.get("provider") or default_provider or "Jazz relationship reference"),
                confidence=max(0, min(100, _safe_int(item.get("confidence"), 75))),
            ))
        self._load_external_candidates(data.get("external_candidates", []), default_provider)
        self._load_popularity(data.get("song_popularity", data.get("popularity", [])), default_provider)
        self._load_rows(data.get("records", []), source_name=default_provider)

    def _load_external_candidates(self, rows: object, default_provider: str) -> None:
        if not isinstance(rows, list):
            return
        for item in rows:
            if not isinstance(item, dict):
                continue
            anchor_title = str(item.get("anchor_title", "")).strip()
            candidate_title = str(item.get("candidate_title", "")).strip()
            if not anchor_title or not candidate_title:
                continue
            self.external_candidates.append(ExternalCandidateReference(
                provider=str(item.get("provider") or default_provider or "External reference"),
                anchor_title=anchor_title,
                candidate_title=candidate_title,
                anchor_artist=str(item.get("anchor_artist", "")),
                candidate_artist=str(item.get("candidate_artist", "")),
                evidence_type=str(item.get("evidence_type") or "external candidate suggestion"),
                relationship_tags=_split_tags(item.get("relationship_tags", [])),
                source_url=str(item.get("source_url", "")),
                confidence=max(0, min(100, _safe_int(item.get("confidence"), 45))),
                note=str(item.get("note", "")),
            ))

    def _load_popularity(self, rows: object, default_provider: str) -> None:
        if not isinstance(rows, list):
            return
        for item in rows:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title", "")).strip()
            if not title:
                continue
            rank = max(0, _safe_int(item.get("rank"), 0))
            score = _safe_int(item.get("familiarity_score"), 0)
            if not score and rank:
                # Bounded 1..100 familiarity only; never a musical score.
                score = max(10, min(100, int(round(101 - min(rank, 5000) / 50))))
            self.popularity.append(SongPopularityReference(
                artist=str(item.get("artist", "")),
                title=title,
                rank=rank,
                year=max(0, _safe_int(item.get("year"), 0)),
                familiarity_score=max(0, min(100, score)),
                provider=str(item.get("provider") or default_provider or "Imported popularity/familiarity list"),
                source_url=str(item.get("source_url", "")),
            ))

    def _load_rows(self, rows: Iterable[dict], *, source_name: str) -> None:
        for item in rows:
            if self._loaded_rows >= MAX_REFERENCE_ROWS:
                self.load_warnings.append(f"Reference row limit reached ({MAX_REFERENCE_ROWS:,}).")
                return
            self._loaded_rows += 1
            if not isinstance(item, dict):
                continue
            kind = str(item.get("record_type") or item.get("evidence_type") or item.get("type") or "").casefold()
            if kind in {"popularity", "song popularity", "familiarity", "ranking"} or (
                item.get("title") and any(item.get(key) not in (None, "") for key in ("rank", "year", "familiarity_score"))
            ):
                self._load_popularity([item], str(item.get("provider") or source_name))
                continue
            anchor = str(item.get("anchor_title") or item.get("title_a") or item.get("source_title") or "").strip()
            candidate = str(item.get("candidate_title") or item.get("title_b") or item.get("related_title") or "").strip()
            if anchor and candidate:
                self._load_external_candidates([{
                    **item,
                    "anchor_title": anchor,
                    "candidate_title": candidate,
                    "provider": item.get("provider") or source_name,
                }], source_name)

    def _deduplicate(self) -> None:
        def unique(items: Sequence, key):
            result = []
            seen = set()
            for item in items:
                marker = key(item)
                if marker in seen:
                    continue
                seen.add(marker)
                result.append(item)
            return result

        self.pairings = unique(self.pairings, lambda item: (
            normalize_title(item.mashup_artist), normalize_title(item.mashup_title),
            tuple(sorted(normalize_title(title) for title in item.source_titles)),
        ))
        self.jazz_relationships = unique(self.jazz_relationships, lambda item: (
            normalize_title(item.source_title), normalize_title(item.related_title), normalize_title(item.relationship)
        ))
        self.external_candidates = unique(self.external_candidates, lambda item: (
            normalize_title(item.provider), normalize_title(item.anchor_title), normalize_title(item.candidate_title)
        ))
        self.popularity = unique(self.popularity, lambda item: (normalize_title(item.artist), normalize_title(item.title)))

    # ---------- exact metadata lookup ----------
    @staticmethod
    def _song_matches(title: str, artist: str, ref_title: str, ref_artist: str = "") -> bool:
        if normalize_title(title) != normalize_title(ref_title):
            return False
        artist_key = normalize_title(artist)
        ref_artist_key = normalize_title(ref_artist)
        return not artist_key or not ref_artist_key or artist_key == ref_artist_key

    def documented_pairing(
        self, title_a: str, title_b: str, artist_a: str = "", artist_b: str = ""
    ) -> PairingReference | None:
        """Find a curated source pair, honoring artist metadata when it is available."""
        for item in self.pairings:
            sources = list(zip(item.source_titles, item.source_artists))
            a_matches = [
                index for index, (title, artist) in enumerate(sources)
                if self._song_matches(title_a, artist_a, title, artist)
            ]
            b_matches = [
                index for index, (title, artist) in enumerate(sources)
                if self._song_matches(title_b, artist_b, title, artist)
            ]
            if any(left != right for left in a_matches for right in b_matches):
                return item
        return None

    def jazz_relationship(self, title_a: str, title_b: str) -> JazzRelationship | None:
        pair = {normalize_title(title_a), normalize_title(title_b)}
        for item in self.jazz_relationships:
            titles = {normalize_title(item.source_title), normalize_title(item.related_title)}
            if pair == titles:
                return item
        return None

    def external_references(
        self, title_a: str, title_b: str, artist_a: str = "", artist_b: str = ""
    ) -> tuple[ExternalCandidateReference, ...]:
        result: list[ExternalCandidateReference] = []
        for item in self.external_candidates:
            forward = self._song_matches(title_a, artist_a, item.anchor_title, item.anchor_artist) and self._song_matches(
                title_b, artist_b, item.candidate_title, item.candidate_artist
            )
            reverse = self._song_matches(title_b, artist_b, item.anchor_title, item.anchor_artist) and self._song_matches(
                title_a, artist_a, item.candidate_title, item.candidate_artist
            )
            if forward or reverse:
                result.append(item)
        return tuple(result)

    def popularity_for(self, title: str, artist: str = "") -> SongPopularityReference | None:
        matches = [item for item in self.popularity if self._song_matches(title, artist, item.title, item.artist)]
        if not matches:
            return None
        return max(matches, key=lambda item: (item.familiarity_score, -item.rank if item.rank else 0))

    def evidence_for_pair(
        self, title_a: str, title_b: str, artist_a: str = "", artist_b: str = ""
    ) -> PairEvidence:
        labels: list[str] = []
        reasons: list[str] = []
        providers: list[str] = []
        urls: list[str] = []
        score = 0
        documented_reference: str | None = None
        category: str | None = None
        progression_family: str | None = None

        documented = self.documented_pairing(title_a, title_b, artist_a, artist_b)
        if documented:
            score = max(score, documented.confidence)
            documented_reference = documented.display
            category = "documented human pairing"
            labels.append("Documented human mashup precedent")
            reasons.append(
                f"Human-created mashup precedent: {documented.mashup_artist} — {documented.mashup_title}. "
                "This helps discovery, but the current MIDI arrangements still had to pass the engine's musical checks."
            )
            providers.append(documented.provider)
            if documented.source_url:
                urls.append(documented.source_url)

        jazz = self.jazz_relationship(title_a, title_b)
        if jazz:
            score = max(score, jazz.confidence)
            documented_reference = documented_reference or f"Jazz {jazz.relationship}: {jazz.source_title} / {jazz.related_title}"
            category = category or "documented jazz relationship"
            progression_family = jazz.progression_family
            labels.append(f"Jazz {jazz.relationship}")
            reasons.append(
                f"Documented jazz relationship: both compositions are associated with {jazz.progression_family}. "
                "The MIDI harmonic analysis remains authoritative."
            )
            providers.append(jazz.provider)
            if jazz.source_url:
                urls.append(jazz.source_url)

        for item in self.external_references(title_a, title_b, artist_a, artist_b):
            score = max(score, item.confidence)
            labels.append(item.evidence_type.replace("_", " ").title())
            qualifier = f" {item.note}" if item.note else ""
            reasons.append(
                f"Provisional discovery evidence from {item.provider}: this pair appeared in an imported "
                f"{item.evidence_type}.{qualifier} MIDI-derived key, tempo, meter, structure, and role checks still decide whether it is usable."
            )
            providers.append(item.provider)
            if item.source_url:
                urls.append(item.source_url)

        pop_a = self.popularity_for(title_a, artist_a)
        pop_b = self.popularity_for(title_b, artist_b)
        familiarity_values = [item.familiarity_score for item in (pop_a, pop_b) if item]
        familiarity = int(round(sum(familiarity_values) / len(familiarity_values))) if familiarity_values else 0
        if pop_a or pop_b:
            recognized = "; ".join(item.label for item in (pop_a, pop_b) if item)
            labels.append("Imported familiarity metadata")
            reasons.append(
                f"Familiarity metadata recognizes {recognized}. Familiarity is used only as a small presentation/ranking tie-breaker, never as musical compatibility."
            )
            for item in (pop_a, pop_b):
                if item:
                    providers.append(item.provider)
                    if item.source_url:
                        urls.append(item.source_url)

        return PairEvidence(
            score=max(0, min(100, score)),
            familiarity_score=max(0, min(100, familiarity)),
            labels=tuple(dict.fromkeys(labels)),
            reasons=tuple(dict.fromkeys(reasons)),
            providers=tuple(dict.fromkeys(provider for provider in providers if provider)),
            source_urls=tuple(dict.fromkeys(url for url in urls if url)),
            documented_reference=documented_reference,
            pairing_category=category,
            progression_family=progression_family,
        )

    def candidate_title_pairs(self) -> set[tuple[str, str]]:
        result: set[tuple[str, str]] = set()
        for item in self.pairings:
            titles = [normalize_title(title) for title in item.source_titles if normalize_title(title)]
            for index, left in enumerate(titles):
                for right in titles[index + 1:]:
                    result.add(tuple(sorted((left, right))))
        for item in self.external_candidates:
            left, right = normalize_title(item.anchor_title), normalize_title(item.candidate_title)
            if left and right:
                result.add(tuple(sorted((left, right))))
        for item in self.jazz_relationships:
            left, right = normalize_title(item.source_title), normalize_title(item.related_title)
            if left and right:
                result.add(tuple(sorted((left, right))))
        return result

    def tags_for_title(self, title: str) -> list[str]:
        needle = normalize_title(title)
        result: list[str] = []
        for item in self.pairings:
            if needle in {normalize_title(value) for value in item.source_titles}:
                result.extend(item.relationship_tags)
        for item in self.jazz_relationships:
            if needle in {normalize_title(item.source_title), normalize_title(item.related_title)}:
                result.extend([item.relationship, item.progression_family])
        for item in self.external_candidates:
            if needle in {normalize_title(item.anchor_title), normalize_title(item.candidate_title)}:
                result.extend(item.relationship_tags)
        return sorted(set(tag for tag in result if tag))

    def search(self, query: str) -> list[PairingReference]:
        needle = normalize_title(query)
        if not needle:
            return list(self.pairings)
        return [item for item in self.pairings if needle in normalize_title(" ".join((item.mashup_artist, item.mashup_title, *item.source_titles, *item.source_artists)))]

    def summary(self) -> dict[str, int]:
        return {
            "documented_pairings": len(self.pairings),
            "jazz_relationships": len(self.jazz_relationships),
            "external_candidates": len(self.external_candidates),
            "popularity_records": len(self.popularity),
            "providers": len({
                *(item.provider for item in self.pairings),
                *(item.provider for item in self.jazz_relationships),
                *(item.provider for item in self.external_candidates),
                *(item.provider for item in self.popularity),
            }),
        }
