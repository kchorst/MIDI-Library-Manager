# MIDI Library Manager v0.39.3

MIDI Library Manager is the authoritative persistent catalogue, analysis, review, playback, and preparation application for the MIDI Performer product system. It is designed for large MIDI collections and preserves completed Fast Scan, Deep Analysis, lifecycle, composition-family, and Remix Eligibility work across restarts.

## Can I use it by itself?

**Yes. MIDI Library Manager is a complete standalone Library application.** You can install and use it without MIDI Remixer for catalogue management, scanning, detailed analysis, search/filter/review, playback, quarantine/restore, file maintenance, and Library backup/restore.

If you also install MIDI Remixer, Remixer attaches to this same authoritative Library. It does not create a competing catalogue.

## Release compatibility

| Item | Release value |
| --- | --- |
| MIDI Library Manager | **v0.39.3** |
| Matched MIDI Remixer | **v0.34.10** |
| Library Engine | **0.36.3** |
| Library Contract | **4** |
| Library schema | **15** |
| Supported OS | **Windows 10/11, 64-bit** |
| Supported Python | **3.11, 3.12, or 3.13, 64-bit** |
| License | **PolyForm Noncommercial 1.0.0** |

## Installation

For complete installation instructions, see **`INSTALL_GUIDE.md`**.

Short version:

1. Install a supported 64-bit Python version.
2. Extract this package to a writable folder.
3. Double-click **`Launch MIDI Library Manager.bat`**.
4. The launcher creates/reuses `%LOCALAPPDATA%\MIDI Remixer\PythonEnv` and installs the exact pinned packages if required.
5. Choose **Add Folder…** to create a Library, or **Restore Library Data…** to restore one.

Pinned runtime packages: **PySide6 6.8.3, mido 1.3.3, rapidfuzz 3.14.3, openpyxl 3.1.5**.

Scanning/cataloguing does not require audio software. Audition/playback requires **FluidSynth + a `.sf2`/`.sf3` SoundFont**.

## First run and returning users

A true first run has no usable saved Library. Add a source folder or restore a Library backup. Fast Scan discovers/reconciles MIDI files; Deep Analysis adds musical detail; recommendation readiness prepares persistent family/eligibility relationships used by MIDI Remixer.

Program files and per-user Library data are separate. Existing compatible Library data is reused after code updates under the same Windows profile. A temporarily unavailable source drive does not mean the saved catalogue has been deleted.

## Core workflow

- **Fast Scan** — discovers configured source folders and parses only new/changed MIDI files.
- **Deep Analysis** — detailed musical profiling with persisted progress.
- **Mash Recommendation Readiness** — persistent family/eligibility preparation.
- **Library Browser** — paged search/filter/review designed for very large catalogues.
- **Review / Quarantine / Restore** — controlled handling of unreadable, unusual, rejected, quarantined, and review-needed files.
- **Backup / Restore** — move/recover Library intelligence separately from the source MIDI files.

Guided and Assisted Remixer workflows use committed **Blessed + Mash Ready** material. Same-composition alternatives may become **Fusion Candidate** for Version Fusion. Unresolved, Rejected, Quarantined, unsupported, or review-needed material does not silently enter normal recommendation pools.

## Persistent data

Normal per-user data lives under:

`%APPDATA%\MIDI Remixer\`

Runtime databases, settings, backups, logs, caches, and user media are intentionally not stored in this repository.

## MIDI Remixer integration

MIDI Remixer v0.34.10 is the matched creative client. For the simplest two-product setup, install the Library and Remixer as sibling folders under one parent folder. Both products reuse the same private Python environment and authoritative Library data.

Library owns scanning, migration, lifecycle/family/eligibility truth, quarantine/recovery, and persistent preparation. Remixer owns creative recommendation workflows, sessions, generated Trials/Takes, audition, and export.

## Documentation

- **`INSTALL_GUIDE.md`** — standalone Library installation and optional Remixer pairing.
- **`USER_GUIDE.md`** — complete Library operation guide.

## Repository contents

This public package intentionally contains only runtime/application source, required data/resources, launch/install files, documentation, dependency manifests, `.gitignore`, and license. Qualification harnesses, tests, test results, logs, caches, intermediate build files, and local user data are excluded.

## License

MIDI Library Manager is provided under the **PolyForm Noncommercial License 1.0.0**. See **`LICENSE.md`** for the complete terms and required notice:

`Required Notice: Copyright © 2026 Kevin Horst.`

The license permits noncommercial purposes as defined in its terms. Commercial use is not granted by this license. Third-party dependencies, FluidSynth, SoundFonts, and user-provided MIDI content remain under their own licenses.
