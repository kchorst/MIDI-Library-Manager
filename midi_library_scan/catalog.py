from __future__ import annotations

import gzip
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

try:
    from rapidfuzz import fuzz
except ImportError:  # pragma: no cover
    from difflib import SequenceMatcher

    class _Fuzz:
        @staticmethod
        def ratio(a: str, b: str) -> float:
            return SequenceMatcher(None, a, b).ratio() * 100

        @staticmethod
        def token_set_ratio(a: str, b: str) -> float:
            return _Fuzz.ratio(" ".join(sorted(set(a.split()))), " ".join(sorted(set(b.split()))))

    fuzz = _Fuzz()

from .models import CatalogMatch
from .normalize import identity_tokens, normalize_filename, normalize_text


@dataclass(frozen=True, slots=True)
class CatalogRecord:
    composition_id: str
    artist: str
    title: str
    genres: str
    primary_genre: str
    normalized_artist: str
    normalized_title: str
    canonical_key: str

    @property
    def artist_title(self) -> str:
        return f"{self.normalized_artist} {self.normalized_title}".strip()

    @property
    def title_artist(self) -> str:
        return f"{self.normalized_title} {self.normalized_artist}".strip()


class SongCatalog:
    def __init__(self, records: Iterable[CatalogRecord]):
        self.records = list(records)
        self.by_key = {r.canonical_key: r for r in self.records}
        self._token_index: dict[str, set[int]] = {}
        for idx, record in enumerate(self.records):
            for token in identity_tokens(record.artist_title):
                self._token_index.setdefault(token, set()).add(idx)

    @classmethod
    def from_resource(cls, path: str | Path | None = None) -> "SongCatalog":
        if path is None:
            path = Path(__file__).with_name("resources") / "song_catalog.json.gz"
        path = Path(path)
        opener = gzip.open if path.suffix == ".gz" else open
        with opener(path, "rt", encoding="utf-8") as fh:
            rows = json.load(fh)
        return cls(CatalogRecord(**row) for row in rows)

    def match(self, filename: str, embedded_text: Iterable[str] = ()) -> CatalogMatch:
        file_norm = normalize_filename(filename)
        extra_norm = normalize_text(" ".join(embedded_text))
        search_text = f"{file_norm} {extra_norm}".strip()
        if not search_text:
            return CatalogMatch()

        # Exact canonical forms first. This safely resolves artist-title and title-artist filenames.
        for record in self.records:
            if record.artist_title and record.artist_title in search_text:
                return self._to_match(record, 1.0, "exact", ["artist and title found in filename/metadata"])
            if record.title_artist and record.title_artist in search_text:
                return self._to_match(record, 0.99, "exact", ["title and artist found in filename/metadata"])

        query_tokens = identity_tokens(search_text)
        candidate_ids: set[int] = set()
        for token in query_tokens:
            candidate_ids.update(self._token_index.get(token, ()))
        if not candidate_ids:
            return CatalogMatch()

        scored: list[tuple[float, CatalogRecord, list[str]]] = []
        for idx in candidate_ids:
            record = self.records[idx]
            title_score = fuzz.token_set_ratio(file_norm, record.normalized_title)
            combined_score = max(
                fuzz.token_set_ratio(search_text, record.artist_title),
                fuzz.token_set_ratio(search_text, record.title_artist),
            )
            artist_score = fuzz.token_set_ratio(search_text, record.normalized_artist)
            title_tokens = identity_tokens(record.normalized_title)
            title_coverage = len(title_tokens & query_tokens) / max(1, len(title_tokens))
            artist_tokens = identity_tokens(record.normalized_artist)
            artist_coverage = len(artist_tokens & query_tokens) / max(1, len(artist_tokens))

            # Require meaningful title evidence; same-artist-only is not enough.
            if title_coverage < 0.50 and title_score < 78:
                continue
            score = 0.50 * combined_score + 0.30 * title_score + 0.20 * artist_score
            score += min(6.0, title_coverage * 4.0 + artist_coverage * 2.0)
            evidence = [f"title coverage {title_coverage:.0%}"]
            if artist_coverage:
                evidence.append(f"artist coverage {artist_coverage:.0%}")
            if extra_norm:
                evidence.append("embedded MIDI text considered")
            scored.append((score, record, evidence))

        if not scored:
            return CatalogMatch()
        scored.sort(key=lambda item: item[0], reverse=True)
        best_score, best, evidence = scored[0]
        runner_up = scored[1][0] if len(scored) > 1 else 0.0
        margin = best_score - runner_up

        if best_score >= 92 and margin >= 4:
            status = "high"
        elif best_score >= 82 and margin >= 3:
            status = "probable"
        else:
            return CatalogMatch(
                confidence=round(min(best_score / 100.0, 0.77), 3),
                status="unmatched",
                evidence=["ambiguous or insufficient artist/title evidence"],
            )
        evidence.append(f"candidate margin {margin:.1f}")
        return self._to_match(best, best_score / 100.0, status, evidence)

    @staticmethod
    def _to_match(record: CatalogRecord, confidence: float, status: str, evidence: list[str]) -> CatalogMatch:
        return CatalogMatch(
            composition_id=record.composition_id,
            artist=record.artist,
            title=record.title,
            genres=record.genres,
            primary_genre=record.primary_genre,
            confidence=round(confidence, 3),
            status=status,
            evidence=evidence,
        )
