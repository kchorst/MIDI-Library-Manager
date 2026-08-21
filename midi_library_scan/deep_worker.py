from __future__ import annotations

"""Persistent detailed-analysis worker for large MIDI libraries.

v0.27 keeps one isolated child process alive for many MIDI files instead of
paying Python/import/database startup costs tens of thousands of times.  A bad
file is still isolated from the Qt process; the parent can kill/restart this
worker if a single request exceeds the safety budget.
"""

import argparse
import json
import os
from pathlib import Path
import sys
import time
from typing import TextIO

from core.library_index import LibraryIndex
from core.staged_library import enrich_deep_profile
from midi_library_scan.database import LibraryDatabase
from midi_library_scan.midi_quick import scan_midi




def _write_json(output_stream: TextIO, payload: dict[str, object]) -> None:
    """Write one code-page-independent JSON protocol line.

    Windows workers may inherit cp1252 even when the parent pipe is decoded as
    UTF-8. Escaping non-ASCII keeps arbitrary MIDI metadata/path text from
    crashing the worker or corrupting the line protocol.
    """
    print(json.dumps(payload, ensure_ascii=True), file=output_stream, flush=True)

def _quick_scan(source: str | Path) -> dict[str, object]:
    path = Path(source).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    return scan_midi(path).as_dict()


def _analyze_with_open_stores(
    source: str | Path,
    *,
    index: LibraryIndex,
    quick_db: LibraryDatabase,
    force: bool = False,
) -> dict[str, object]:
    path = Path(source).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    try:
        qa_hold = max(0.0, min(8.0, float(os.environ.get("MIDI_LIBRARY_QA_ANALYZER_WORKER_HOLD_SECONDS", "0") or 0)))
    except ValueError:
        qa_hold = 0.0
    if qa_hold > 0.0:
        time.sleep(qa_hold)
    quick_result = quick_db.load_result(path, validate_source=False)
    if quick_result is None:
        raise RuntimeError(
            f"Quick catalogue metadata is not ready for {path.name}; run incremental catalogue reconciliation first."
        )
    stat = path.stat()
    if int(quick_result.size_bytes) != int(stat.st_size) or int(quick_result.modified_ns) != int(stat.st_mtime_ns):
        raise RuntimeError(
            f"Quick catalogue metadata is stale for changed MIDI {path.name}; detailed analysis was deferred until reconciliation."
        )
    profiles, stats = index.classify(
        [path],
        include_generated=True,
        recursive=False,
        force=force,
        profile_transform=lambda profile: enrich_deep_profile(profile, quick_result),
    )
    if not profiles or stats.failed:
        detail = index.error_for(path) or "No detailed profile was produced."
        raise RuntimeError(f"Detailed analysis failed for {path.name}: {detail}")
    return {"path": str(path), "analyzed": stats.analyzed, "reused": stats.reused, "failed": stats.failed}


def analyze_one(
    source: str | Path,
    *,
    library_index_path: str | Path,
    quick_database_path: str | Path,
    force: bool = False,
) -> dict[str, object]:
    quick_db = LibraryDatabase(quick_database_path, recover_interrupted=False, read_only=True)
    index = LibraryIndex(library_index_path)
    try:
        return _analyze_with_open_stores(source, index=index, quick_db=quick_db, force=force)
    finally:
        index.close()
        quick_db.close()


def serve(
    *,
    library_index_path: str | Path,
    quick_database_path: str | Path,
    input_stream: TextIO = sys.stdin,
    output_stream: TextIO = sys.stdout,
) -> int:
    quick_db = LibraryDatabase(quick_database_path, recover_interrupted=False, read_only=True)
    index = LibraryIndex(library_index_path)
    try:
        for raw in input_stream:
            line = raw.strip()
            if not line:
                continue
            try:
                request = json.loads(line)
            except json.JSONDecodeError as exc:
                _write_json(output_stream, {"ok": False, "error": f"JSONDecodeError: {exc}"})
                continue
            if request.get("command") == "shutdown":
                _write_json(output_stream, {"ok": True, "shutdown": True})
                return 0
            request_id = str(request.get("id") or "")
            try:
                if str(request.get("command") or "").casefold() == "quick_scan":
                    payload = _quick_scan(request.get("path", ""))
                else:
                    payload = _analyze_with_open_stores(
                        request.get("path", ""), index=index, quick_db=quick_db, force=bool(request.get("force", False))
                    )
            except Exception as exc:
                response = {"id": request_id, "ok": False, "error": f"{type(exc).__name__}: {exc}"}
            else:
                response = {"id": request_id, "ok": True, "result": payload}
            _write_json(output_stream, response)
    finally:
        index.close()
        quick_db.close()
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Analyze MIDI files for MIDI Remixer")
    parser.add_argument("--path")
    parser.add_argument("--library-index", required=True)
    parser.add_argument("--quick-database", required=True)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--serve", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.serve:
        return serve(library_index_path=args.library_index, quick_database_path=args.quick_database)
    if not args.path:
        print("--path is required unless --serve is used", file=sys.stderr)
        return 2
    try:
        payload = analyze_one(
            args.path,
            library_index_path=args.library_index,
            quick_database_path=args.quick_database,
            force=bool(args.force),
        )
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(payload, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
