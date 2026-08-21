# MIDI Library Manager v0.39.3 — Installation Guide

## What this product is

**MIDI Library Manager can be installed and used by itself.** It is the complete Library application: it discovers MIDI files, builds and maintains the persistent catalogue, performs Fast Scan and Deep Analysis, prepares composition-family and Remix Eligibility data, provides the full Library Browser, supports review/quarantine/restore/backup workflows, and can audition MIDI when an audio stack is configured.

You do **not** need MIDI Remixer to use MIDI Library Manager.

If you later add MIDI Remixer v0.34.10, Remixer attaches to this authoritative Library rather than creating a second catalogue.

## Supported system

- Windows 10 or Windows 11, 64-bit
- 64-bit Python **3.11, 3.12, or 3.13**
- A normal writable folder for the application files
- Internet access during the first dependency installation, unless the compatible shared environment is already present

Python 3.14 is not the supported runtime for this release.

## Required Python dependencies

The launcher installs/reuses the exact versions in `requirements.lock`:

| Package | Version | Purpose |
| --- | ---: | --- |
| PySide6 | 6.8.3 | Qt desktop interface |
| mido | 1.3.3 | MIDI parsing/data handling |
| rapidfuzz | 3.14.3 | Fast identity/text matching |
| openpyxl | 3.1.5 | Shared spreadsheet/reference-data support |

The private environment is stored at:

`%LOCALAPPDATA%\MIDI Remixer\PythonEnv`

MIDI Library Manager and the matched MIDI Remixer reuse this same environment. You should not create a second environment for the second product.

## Install MIDI Library Manager by itself

1. Install a supported 64-bit Python version if one is not already installed.
2. Download/extract the MIDI Library Manager v0.39.3 package to a normal writable folder.
3. Double-click **`Launch MIDI Library Manager.bat`**.
4. If the shared private environment is missing or does not contain the exact pinned packages, the launcher runs **`Install Requirements.bat`** automatically.
5. On first launch, choose **Add Folder…** to create a Library from MIDI files, or **Restore Library Data…** to restore an existing Library backup.
6. Let Fast Scan catalogue the collection. Deep Analysis and recommendation-readiness preparation can continue after Fast Scan and preserve completed work across restarts.

You may run **`Install Requirements.bat`** manually before step 3 if you prefer to prepare dependencies first.

## Audio setup — optional for cataloguing, required for audition

Scanning, cataloguing, analysis, review, and Library maintenance do not require an audio renderer.

For MIDI audition/playback, configure:

- FluidSynth (`fluidsynth.exe` on Windows); and
- a General MIDI `.sf2` or `.sf3` SoundFont.

Use **Audio Setup…** in the Library Browser.

## Adding MIDI Remixer later

The matched creative client is **MIDI Remixer v0.34.10** using Library Contract 4 / schema 15.

For the simplest setup, keep the products as sibling folders under one parent directory, for example:

```text
MIDI Performer Suite\
├── MIDI-Library-Manager-v0.39.3\
└── MIDI-Remixer-v0.34.10\
```

Launch Library Manager first to create or restore the authoritative Library. Then launch Remixer. The two products reuse the same private Python environment and the same per-user Library data.

## Where user data lives

Application code and persistent Library data are separate. Normal per-user suite data is stored under:

`%APPDATA%\MIDI Remixer\`

This includes the Library databases/settings, backups, and runtime diagnostics. Removing or replacing the code folder does not by itself remove that per-user Library data.

## Updating

When updating the application code:

1. Keep the old code folder until the new release is confirmed working.
2. Extract the new release to its own folder rather than mixing files from two releases.
3. Launch the new version under the same Windows profile; it should reconnect to the existing compatible per-user Library.
4. Use Library backup/restore when moving to another Windows profile or computer.

## Troubleshooting installation

- **Launcher says compatible Python is missing:** install 64-bit Python 3.11, 3.12, or 3.13.
- **Dependency installation fails:** verify internet access and rerun `Install Requirements.bat`.
- **Application opens but no Library exists:** choose Add Folder… or Restore Library Data….
- **No playback audio:** configure FluidSynth and a SoundFont; this does not affect scanning/cataloguing.
- **Using Remixer too:** keep the matched v0.39.3 Library and v0.34.10 Remixer pair for this release line.

## License

MIDI Library Manager v0.39.3 is provided under the **PolyForm Noncommercial License 1.0.0**. See `LICENSE.md` for the complete terms and required notice.

The license permits the software for noncommercial purposes as defined by those terms. Commercial use is not granted by this license. Third-party dependencies, audio tools, SoundFonts, and user MIDI files remain under their own licenses.
