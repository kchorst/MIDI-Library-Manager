from __future__ import annotations

import argparse
from pathlib import Path

from .pipeline import LibraryScanService


def main() -> int:
    parser = argparse.ArgumentParser(description="MIDI Remixer staged library catalog scan")
    parser.add_argument("midi_directory", type=Path)
    parser.add_argument("--database", type=Path, default=Path("midi_library.sqlite3"))
    parser.add_argument("--report", type=Path, default=Path("library_scan_report.json"))
    parser.add_argument("--no-subfolders", action="store_true")
    args = parser.parse_args()

    def progress(p):
        current = f" — {p.current_file}" if p.current_file else ""
        print(f"[{p.phase}] {p.current}/{p.total}{current}")

    with LibraryScanService(args.database, on_progress=progress) as service:
        summary = service.quick_catalog(args.midi_directory, include_subfolders=not args.no_subfolders)
        service.export_report(summary, args.report)
    print(
        f"Catalogued {summary.catalogued}/{summary.discovered}; "
        f"identified {summary.identified}; probable {summary.probable_identity}; "
        f"duplicates/versions {summary.exact_duplicates + summary.likely_duplicates}; failed {summary.failed}."
    )
    print(f"Report: {args.report.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
