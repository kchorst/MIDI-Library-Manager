from __future__ import annotations

"""Read-only, paged access to the persistent MIDI catalogue.

The Library Engine remains the sole writer.  Library Manager uses this module
only for inspection, including when the detached Engine is stopped or still
starting.  Queries are bounded so a 96K+ catalogue is never materialized into
the Qt UI in one operation.
"""

from dataclasses import dataclass
from contextlib import closing
from pathlib import Path
import sqlite3
from typing import Any

from midi_library_scan.lifecycle import eligibility_label, recommended_action as lifecycle_recommended_action


CATALOGUE_TABLE = "library_catalog_files_v11"
PAGE_SIZE_DEFAULT = 200
PAGE_SIZE_MAX = 1000




def suspicious_health_reasons(*, quick_status: str, duration_seconds: float | None, note_count: int, track_count: int) -> tuple[str, ...]:
    """Return advisory health flags for a row that Fast Scan did load."""
    if str(quick_status or "") == "failed":
        return ()
    reasons: list[str] = []
    duration = float(duration_seconds) if duration_seconds is not None else None
    if int(track_count or 0) <= 0:
        reasons.append("no MIDI tracks")
    if int(note_count or 0) <= 0:
        reasons.append("no note events")
    elif int(note_count or 0) <= 8:
        reasons.append(f"only {int(note_count):,} note events")
    if duration is not None:
        if duration <= 0.25:
            reasons.append("near-zero duration")
        elif duration < 12.0:
            reasons.append(f"very short ({duration:.1f}s)")
        elif duration > 1800.0:
            reasons.append(f"very long ({duration / 60.0:.1f} min)")
    return tuple(reasons)

def classify_deep_failure(error: str) -> str:
    """Classify saved deep-analysis errors conservatively for human review.

    The classifier never declares a source file safe to delete. It separates
    known-valid-but-unsupported and safety-limit cases from likely source-file
    corruption and internal analyzer defects so the user is not encouraged to
    delete a valid MIDI merely because the current analyzer cannot process it.
    """
    text = " ".join(str(error or "").split()).casefold()
    if not text:
        return "Needs review"
    if "type 2" in text and ("asyn" in text or "compute length" in text):
        return "Valid/unsupported MIDI Type 2"
    if "midiresourcelimiterror" in text or "safety limit" in text or "resource limit" in text:
        return "Resource/safety limit"
    if any(token in text for token in ("attributeerror", "assertionerror", "nameerror", "notimplementederror")):
        return "Analyzer/internal error"
    if any(token in text for token in ("unexpected end of file", "eoferror", "invalid midi", "invalid mthd", "running status")):
        return "Possibly corrupt MIDI"
    if any(token in text for token in ("permissionerror", "access is denied", "file not found", "filenotfounderror")):
        return "Source/access problem"
    return "Needs review"


def recommended_action_for_row(
    *, quick_status: str, deep_status: str, deep_error: str, health_reasons: tuple[str, ...]
) -> str:
    """Return a user-facing next action that matches what Library can actually do.

    MIDI Library does not repair source MIDI bytes. Recommendations therefore
    distinguish source-file inspection from Fast/Deep retry operations and never
    imply that a Deep-analysis limitation means the source MIDI should be deleted.
    """
    if str(quick_status or "") == "failed":
        return "Inspect/repair externally; retry Fast Scan after a file or Library update. Quarantine if you want to keep it outside the active Library."
    if str(deep_status or "") == "failed":
        category = classify_deep_failure(deep_error)
        if category == "Valid/unsupported MIDI Type 2":
            return "Keep the MIDI. Current Deep Analysis does not support Type 2; retry only after Library support changes."
        if category == "Resource/safety limit":
            return "Keep the MIDI. No source repair is implied; retry only after analyzer or safety-limit changes."
        if category == "Analyzer/internal error":
            return "Keep the MIDI. Retry after a Library/analyzer update; do not delete the source because of this error."
        if category == "Possibly corrupt MIDI":
            return "Inspect or repair the source in an external MIDI tool, then run Fast Scan again and retry Deep Analysis if needed."
        if category == "Source/access problem":
            return "Restore the file or Windows access first, then retry Fast Scan/Deep Analysis."
        return "Review/audition the MIDI. Retry Deep Analysis only after a relevant file, analyzer, or limit change."
    if health_reasons:
        return "Audition/review it. Keep it if valid; Quarantine if uncertain; Delete only if you intentionally want the source removed."
    return ""


def deep_retry_allowed_for_row(*, deep_status: str, deep_error: str) -> bool:
    """Whether current UI should offer a deliberate Deep retry for this row.

    Unsupported Type-2 structure and a resource/safety ceiling are deterministic
    under the current analyzer configuration. Repeating the identical operation
    would only recreate the same review item, so the UI must not advertise a
    routine retry for those categories. Other failures can become retryable after
    a source/access/analyzer change and therefore retain the deliberate retry.
    """
    if str(deep_status or "") != "failed":
        return False
    category = classify_deep_failure(deep_error)
    return category not in {"Valid/unsupported MIDI Type 2", "Resource/safety limit"}


@dataclass(frozen=True, slots=True)
class CataloguePage:
    rows: tuple[dict[str, Any], ...]
    total_matches: int
    offset: int
    limit: int


class CatalogueReader:
    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path)

    def exists(self) -> bool:
        return self.database_path.is_file()

    def _connect(self) -> sqlite3.Connection:
        if not self.database_path.is_file():
            raise FileNotFoundError(f"Saved MIDI catalogue not found: {self.database_path}")
        uri = self.database_path.resolve(strict=False).as_uri() + "?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=2.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA busy_timeout=1500")
        return connection

    def summary(self, *, include_failure_groups: bool = True) -> dict[str, Any]:
        if not self.exists():
            return {
                "available": False,
                "database_available": False,
                "total": 0,
                "catalogued": 0,
                "quick_failed": 0,
                "suspicious_loadable": 0,
                "pending": 0,
                "paused": 0,
                "analyzing": 0,
                "complete": 0,
                "failed": 0,
                "failed_retryable": 0,
                "failed_persistent": 0,
                "failure_groups": (),
                "mash_ready": 0, "fusion_candidate": 0, "usable_not_mash": 0,
                "needs_review": 0, "rejected": 0, "quarantined": 0, "unresolved": 0, "usable": 0,
            }
        try:
            with closing(self._connect()) as conn:
                columns = {str(item[1]) for item in conn.execute(f"PRAGMA table_info({CATALOGUE_TABLE})").fetchall()}
                lifecycle_supported = "eligibility_state" in columns and "lifecycle_state" in columns
                row = conn.execute(
                    f"SELECT COUNT(*) total, "
                    "SUM(CASE WHEN quick_status='failed' THEN 1 ELSE 0 END) quick_failed, "
                    "SUM(CASE WHEN quick_status<>'failed' AND (track_count<=0 OR note_count<=8 OR duration_seconds<=0.25 "
                    "OR (duration_seconds IS NOT NULL AND duration_seconds<12.0) "
                    "OR (duration_seconds IS NOT NULL AND duration_seconds>1800.0)) THEN 1 ELSE 0 END) suspicious_loadable "
                    f"FROM {CATALOGUE_TABLE}"
                ).fetchone()
                deep_rows = conn.execute(
                    f"SELECT COALESCE(deep_status,'pending') state,COUNT(*) count FROM {CATALOGUE_TABLE} "
                    "WHERE quick_status<>'failed' GROUP BY COALESCE(deep_status,'pending')"
                ).fetchall()
                lifecycle_rows = (
                    conn.execute(
                        f"SELECT COALESCE(eligibility_state,'unresolved') state,COUNT(*) count FROM {CATALOGUE_TABLE} "
                        "GROUP BY COALESCE(eligibility_state,'unresolved')"
                    ).fetchall()
                    if lifecycle_supported else ()
                )
                failure_attempts = conn.execute(
                    f"SELECT "
                    f"SUM(CASE WHEN deep_status='failed' AND deep_attempt_count<2 THEN 1 ELSE 0 END) retryable,"
                    f"SUM(CASE WHEN deep_status='failed' AND deep_attempt_count>=2 THEN 1 ELSE 0 END) persistent "
                    f"FROM {CATALOGUE_TABLE} WHERE quick_status<>'failed'"
                ).fetchone()
                failures = []
                if include_failure_groups:
                    failures = conn.execute(
                        f"SELECT COALESCE(NULLIF(deep_last_error,''),'Unknown detailed-analysis error') error,COUNT(*) count "
                        f"FROM {CATALOGUE_TABLE} WHERE quick_status<>'failed' AND deep_status='failed' "
                        "GROUP BY COALESCE(NULLIF(deep_last_error,''),'Unknown detailed-analysis error') "
                        "ORDER BY count DESC,error LIMIT 8"
                    ).fetchall()
        except (sqlite3.Error, OSError):
            return {
                "available": False,
                "database_available": True,
                "total": 0,
                "catalogued": 0,
                "quick_failed": 0,
                "suspicious_loadable": 0,
                "pending": 0,
                "paused": 0,
                "analyzing": 0,
                "complete": 0,
                "failed": 0,
                "failed_retryable": 0,
                "failed_persistent": 0,
                "failure_groups": (),
                "mash_ready": 0, "fusion_candidate": 0, "usable_not_mash": 0,
                "needs_review": 0, "rejected": 0, "quarantined": 0, "unresolved": 0, "usable": 0,
            }
        total = int(row["total"] or 0) if row else 0
        quick_failed = int(row["quick_failed"] or 0) if row else 0
        result: dict[str, Any] = {
            # From the user's perspective an empty schema is not yet a Library
            # catalogue. Keep database existence separate from browseability so
            # first-run UI does not advertise an empty Browser as useful state.
            "available": total > 0,
            "database_available": True,
            "total": total,
            "catalogued": max(0, total - quick_failed),
            "quick_failed": quick_failed,
            "suspicious_loadable": int(row["suspicious_loadable"] or 0) if row else 0,
            "pending": 0,
            "paused": 0,
            "analyzing": 0,
            "complete": 0,
            "failed": 0,
            "failed_retryable": int((failure_attempts["retryable"] if failure_attempts else 0) or 0),
            "failed_persistent": int((failure_attempts["persistent"] if failure_attempts else 0) or 0),
            "failure_groups": tuple((str(item["error"]), int(item["count"] or 0)) for item in failures),
        }
        lifecycle_counts = {str(item["state"]): int(item["count"] or 0) for item in lifecycle_rows}
        if not lifecycle_counts and total > 0:
            # Offline access to a pre-schema-15 Library remains browseable. The
            # Engine will migrate these rows when it next owns the database;
            # until then they are explicitly unresolved, never implicitly usable.
            lifecycle_counts["unresolved"] = total
        result.update({
            "mash_ready": lifecycle_counts.get("mash_ready", 0),
            "fusion_candidate": lifecycle_counts.get("fusion_candidate", 0),
            "usable_not_mash": lifecycle_counts.get("usable_not_mash", 0),
            "needs_review": lifecycle_counts.get("needs_review", 0),
            "rejected": lifecycle_counts.get("rejected", 0),
            "quarantined": lifecycle_counts.get("quarantined", 0),
            "unresolved": lifecycle_counts.get("unresolved", 0),
        })
        result["usable"] = result["mash_ready"] + result["fusion_candidate"] + result["usable_not_mash"]
        for item in deep_rows:
            state = str(item["state"] or "pending")
            if state in {"pending", "paused", "analyzing", "complete", "failed"}:
                result[state] = int(item["count"] or 0)
        return result

    @staticmethod
    def _filter_clause(search: str, status: str, *, lifecycle_supported: bool = True) -> tuple[str, list[object]]:
        clauses: list[str] = []
        params: list[object] = []
        text = str(search or "").strip()
        if text:
            pattern = f"%{text}%"
            clauses.append(
                "(filename LIKE ? COLLATE NOCASE OR path LIKE ? COLLATE NOCASE "
                "OR COALESCE(catalog_title,'') LIKE ? COLLATE NOCASE "
                "OR COALESCE(catalog_artist,'') LIKE ? COLLATE NOCASE)"
            )
            params.extend([pattern, pattern, pattern, pattern])
        state = str(status or "all").casefold()
        if state in {"mash_ready", "fusion_candidate", "usable_not_mash", "needs_review", "rejected", "quarantined", "unresolved"}:
            if lifecycle_supported:
                clauses.append("eligibility_state=?")
                params.append(state)
            elif state != "unresolved":
                clauses.append("1=0")
        elif state == "quick_failed":
            clauses.append("quick_status='failed'")
        elif state == "suspicious_loadable":
            clauses.append(
                "quick_status<>'failed' AND (track_count<=0 OR note_count<=8 OR duration_seconds<=0.25 "
                "OR (duration_seconds IS NOT NULL AND duration_seconds<12.0) "
                "OR (duration_seconds IS NOT NULL AND duration_seconds>1800.0))"
            )
        elif state == "health_review":
            clauses.append(
                "(quick_status='failed' OR track_count<=0 OR note_count<=8 OR duration_seconds<=0.25 "
                "OR (duration_seconds IS NOT NULL AND duration_seconds<12.0) "
                "OR (duration_seconds IS NOT NULL AND duration_seconds>1800.0))"
            )
        elif state in {"pending", "paused", "analyzing", "complete", "failed"}:
            clauses.append("quick_status<>'failed' AND deep_status=?")
            params.append(state)
        elif state == "failed_retryable":
            clauses.append("quick_status<>'failed' AND deep_status='failed' AND deep_attempt_count<2")
        elif state == "failed_persistent":
            clauses.append("quick_status<>'failed' AND deep_status='failed' AND deep_attempt_count>=2")
        elif state == "unfinished":
            clauses.append("quick_status<>'failed' AND deep_status<>'complete'")
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        return where, params

    def page(
        self,
        *,
        search: str = "",
        status: str = "all",
        sort_key: str = "title",
        descending: bool = False,
        offset: int = 0,
        limit: int = PAGE_SIZE_DEFAULT,
    ) -> CataloguePage:
        limit = max(1, min(PAGE_SIZE_MAX, int(limit)))
        offset = max(0, int(offset))
        # Every visible Browser header is sortable.  Keep the SQL order
        # contract aligned with the text users actually see instead of letting
        # a clickable header silently no-op or fall back to Title.
        suspicious_sql = (
            "quick_status<>'failed' AND (track_count<=0 OR note_count<=8 OR duration_seconds<=0.25 "
            "OR (duration_seconds IS NOT NULL AND duration_seconds<12.0) "
            "OR (duration_seconds IS NOT NULL AND duration_seconds>1800.0))"
        )
        type2_sql = (
            "LOWER(COALESCE(deep_last_error,'')) LIKE '%type 2%' AND "
            "(LOWER(COALESCE(deep_last_error,'')) LIKE '%asyn%' OR "
            "LOWER(COALESCE(deep_last_error,'')) LIKE '%compute length%')"
        )
        safety_sql = (
            "LOWER(COALESCE(deep_last_error,'')) LIKE '%midiresourcelimiterror%' OR "
            "LOWER(COALESCE(deep_last_error,'')) LIKE '%safety limit%' OR "
            "LOWER(COALESCE(deep_last_error,'')) LIKE '%resource limit%'"
        )
        internal_sql = (
            "LOWER(COALESCE(deep_last_error,'')) LIKE '%attributeerror%' OR "
            "LOWER(COALESCE(deep_last_error,'')) LIKE '%assertionerror%' OR "
            "LOWER(COALESCE(deep_last_error,'')) LIKE '%nameerror%' OR "
            "LOWER(COALESCE(deep_last_error,'')) LIKE '%notimplementederror%'"
        )
        corrupt_sql = (
            "LOWER(COALESCE(deep_last_error,'')) LIKE '%unexpected end of file%' OR "
            "LOWER(COALESCE(deep_last_error,'')) LIKE '%eoferror%' OR "
            "LOWER(COALESCE(deep_last_error,'')) LIKE '%invalid midi%' OR "
            "LOWER(COALESCE(deep_last_error,'')) LIKE '%invalid mthd%' OR "
            "LOWER(COALESCE(deep_last_error,'')) LIKE '%running status%'"
        )
        access_sql = (
            "LOWER(COALESCE(deep_last_error,'')) LIKE '%permissionerror%' OR "
            "LOWER(COALESCE(deep_last_error,'')) LIKE '%access is denied%' OR "
            "LOWER(COALESCE(deep_last_error,'')) LIKE '%file not found%' OR "
            "LOWER(COALESCE(deep_last_error,'')) LIKE '%filenotfounderror%'"
        )
        display_status_order = (
            "CASE "
            "WHEN quick_status='failed' THEN 0 "
            "WHEN deep_status='failed' AND deep_attempt_count<2 THEN 1 "
            "WHEN deep_status='failed' THEN 2 "
            f"WHEN {suspicious_sql} THEN 3 "
            "WHEN deep_status='analyzing' THEN 4 "
            "WHEN deep_status='paused' THEN 5 "
            "WHEN deep_status='pending' THEN 6 "
            "WHEN deep_status='complete' THEN 7 ELSE 8 END"
        )
        issue_order = (
            "CASE "
            "WHEN quick_status='failed' THEN 0 "
            f"WHEN deep_status='failed' AND ({type2_sql}) THEN 1 "
            f"WHEN deep_status='failed' AND ({safety_sql}) THEN 2 "
            f"WHEN deep_status='failed' AND ({internal_sql}) THEN 3 "
            f"WHEN deep_status='failed' AND ({corrupt_sql}) THEN 4 "
            f"WHEN deep_status='failed' AND ({access_sql}) THEN 5 "
            "WHEN deep_status='failed' THEN 6 "
            f"WHEN {suspicious_sql} THEN 7 ELSE 8 END"
        )
        recommended_order = (
            "CASE "
            "WHEN quick_status='failed' THEN 0 "
            f"WHEN deep_status='failed' AND ({type2_sql}) THEN 1 "
            f"WHEN deep_status='failed' AND ({safety_sql}) THEN 2 "
            f"WHEN deep_status='failed' AND ({internal_sql}) THEN 3 "
            f"WHEN deep_status='failed' AND ({corrupt_sql}) THEN 4 "
            f"WHEN deep_status='failed' AND ({access_sql}) THEN 5 "
            "WHEN deep_status='failed' THEN 6 "
            f"WHEN {suspicious_sql} THEN 7 ELSE 8 END"
        )
        order_columns = {
            "title": "COALESCE(NULLIF(catalog_title,''),filename) COLLATE NOCASE",
            "artist": "COALESCE(catalog_artist,'') COLLATE NOCASE",
            "filename": "filename COLLATE NOCASE",
            "path": "path COLLATE NOCASE",
            "duration": "COALESCE(duration_seconds,0)",
            "bpm": "COALESCE(bpm,0)",
            "key": "COALESCE(detected_key,'') COLLATE NOCASE",
            "meter": "COALESCE(meter,'') COLLATE NOCASE",
            "tracks": "track_count",
            "notes": "note_count",
            "display_status": display_status_order,
            "error": issue_order,
            "recommended_action": recommended_order,
            "updated": "COALESCE(last_validation_utc,deep_updated_utc,last_scanned_utc) COLLATE NOCASE",
            "eligibility": "eligibility_state COLLATE NOCASE",
            "rejection_reason": "COALESCE(rejection_code,rejection_detail,'') COLLATE NOCASE",
            "file_size": "size_bytes",
        }
        direction = "DESC" if bool(descending) else "ASC"
        with closing(self._connect()) as conn:
            columns = {str(item[1]) for item in conn.execute(f"PRAGMA table_info({CATALOGUE_TABLE})").fetchall()}
            lifecycle_supported = "eligibility_state" in columns and "lifecycle_state" in columns
            order = order_columns.get(str(sort_key or "title"), order_columns["title"])
            if not lifecycle_supported and str(sort_key or "title") in {"eligibility", "rejection_reason"}:
                order = order_columns["title"]
            if not lifecycle_supported and str(sort_key or "title") == "updated":
                order = "COALESCE(deep_updated_utc,last_scanned_utc) COLLATE NOCASE"
            where, params = self._filter_clause(search, status, lifecycle_supported=lifecycle_supported)
            total_row = conn.execute(f"SELECT COUNT(*) FROM {CATALOGUE_TABLE}{where}", tuple(params)).fetchone()
            lifecycle_select = (
                "lifecycle_state,eligibility_state,rejection_code,rejection_detail,lifecycle_validation_attempts,"
                "last_validation_utc,resolved_utc,original_source_path,quarantined_utc,"
                if lifecycle_supported else
                "'discovered' lifecycle_state,'unresolved' eligibility_state,NULL rejection_code,NULL rejection_detail,"
                "0 lifecycle_validation_attempts,last_scanned_utc last_validation_utc,NULL resolved_utc,"
                "NULL original_source_path,NULL quarantined_utc,"
            )
            rows = conn.execute(
                "SELECT path,filename,COALESCE(NULLIF(catalog_title,''),filename) display_title,"
                "COALESCE(catalog_artist,'') artist,duration_seconds,bpm,detected_key,mode,meter,"
                "track_count,note_count,arrangement_type,quick_status,deep_status,deep_attempt_count,error,deep_last_error,"
                "size_bytes," + lifecycle_select + "last_scanned_utc,deep_updated_utc "
                f"FROM {CATALOGUE_TABLE}{where} ORDER BY {order} {direction},path COLLATE NOCASE ASC LIMIT ? OFFSET ?",
                (*params, limit, offset),
            ).fetchall()
        payload: list[dict[str, Any]] = []
        for row in rows:
            path = str(row["path"] or "")
            quick_status = str(row["quick_status"] or "")
            deep_status = str(row["deep_status"] or "pending")
            duration = float(row["duration_seconds"]) if row["duration_seconds"] is not None else None
            health_reasons = suspicious_health_reasons(
                quick_status=quick_status, duration_seconds=duration,
                note_count=int(row["note_count"] or 0), track_count=int(row["track_count"] or 0),
            )
            deep_error = str(row["deep_last_error"] or "")
            payload.append({
                "path": path,
                "folder": str(Path(path).parent) if path else "",
                "filename": str(row["filename"] or ""),
                "title": str(row["display_title"] or ""),
                "artist": str(row["artist"] or ""),
                "duration_seconds": duration,
                "bpm": float(row["bpm"]) if row["bpm"] is not None else None,
                "key": str(row["detected_key"] or ""),
                "mode": str(row["mode"] or ""),
                "meter": str(row["meter"] or ""),
                "tracks": int(row["track_count"] or 0),
                "notes": int(row["note_count"] or 0),
                "arrangement": str(row["arrangement_type"] or ""),
                "quick_status": quick_status,
                "deep_status": deep_status,
                "suspicious_loadable": bool(health_reasons),
                "health_reasons": health_reasons,
                "deep_attempt_count": int(row["deep_attempt_count"] or 0),
                "error": str(row["deep_last_error"] or row["error"] or ""),
                "failure_category": (
                    "Unreadable by Fast Scan" if quick_status == "failed"
                    else classify_deep_failure(deep_error) if deep_status == "failed"
                    else "Unusual but loadable" if health_reasons
                    else ""
                ),
                "recommended_action": recommended_action_for_row(
                    quick_status=quick_status, deep_status=deep_status, deep_error=deep_error, health_reasons=health_reasons
                ),
                "deep_retry_allowed": deep_retry_allowed_for_row(deep_status=deep_status, deep_error=deep_error),
                "lifecycle_state": str(row["lifecycle_state"] or "discovered"),
                "eligibility_state": str(row["eligibility_state"] or "unresolved"),
                "eligibility_label": eligibility_label(str(row["eligibility_state"] or "unresolved")),
                "rejection_code": str(row["rejection_code"] or ""),
                "rejection_detail": str(row["rejection_detail"] or ""),
                "validation_attempts": int(row["lifecycle_validation_attempts"] or 0),
                "last_validation": str(row["last_validation_utc"] or row["deep_updated_utc"] or row["last_scanned_utc"] or ""),
                "resolved_utc": str(row["resolved_utc"] or ""),
                "original_source_path": str(row["original_source_path"] or ""),
                "quarantined_utc": str(row["quarantined_utc"] or ""),
                "file_size_bytes": int(row["size_bytes"] or 0),
                "updated": str(row["last_validation_utc"] or row["deep_updated_utc"] or row["last_scanned_utc"] or ""),
            })
            lifecycle_action = lifecycle_recommended_action(
                lifecycle_state=payload[-1]["lifecycle_state"],
                eligibility_state=payload[-1]["eligibility_state"],
                rejection_code=payload[-1]["rejection_code"],
            )
            # Needs Review is the one lifecycle state where the technical reason
            # materially changes the useful next action.  Do not overwrite a
            # concrete Type-2/safety-limit/analyzer recommendation with the generic
            # "Run Revalidate" lifecycle sentence.
            if payload[-1]["eligibility_state"] == "needs_review" and payload[-1]["recommended_action"]:
                pass
            else:
                payload[-1]["recommended_action"] = lifecycle_action
        return CataloguePage(tuple(payload), int(total_row[0] or 0) if total_row else 0, offset, limit)
