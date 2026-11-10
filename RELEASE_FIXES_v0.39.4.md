# MIDI Library Manager v0.39.4 – safety patch

Based on the provided v0.39.3 GitHub release.

1. Restore registers rollback before staged database installation, so an installation failure restores the original database. Existing SQLite WAL and SHM sidecars are also preserved and restored on failure. If rollback itself fails, displaced files are retained under `library/restore-recovery` instead of being silently discarded with temporary files.
2. Filesystem discovery records inaccessible roots, unreadable subdirectories, failed metadata reads, and resolution errors. An incomplete inventory skips destructive catalogue reconciliation and emits a `reconciliation_skipped` progress event. Previously indexed rows remain intact until a complete subsequent scan.
3. Backup verification now rejects manifests without SQLite databases, mismatched v2 included-file inventories, and missing/inconsistent declared database schema metadata.

## Verification

`python -m unittest discover -s tests -v` – 10 regression tests passed on Linux/Python 3.13. `python -m compileall` passed. Windows/Qt GUI acceptance tests were not performed in this environment.

This is a code safety patch; the internal Engine product version constant was not changed because it is shared with other split-suite components.
