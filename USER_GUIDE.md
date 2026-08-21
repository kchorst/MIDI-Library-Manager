# MIDI Library Manager v0.39.3 — User Guide

## 1. What MIDI Library Manager is for

MIDI Library Manager builds a persistent, reusable understanding of a MIDI collection. The goal is that a large collection becomes useful quickly through Fast Scan, becomes more musically detailed through Deep Analysis, and can later supply prepared recommendation data to MIDI Remixer without rescanning the same unchanged files.

**Authoritative product flow:** MIDI Library Manager / Library Engine owns the authoritative persistent MIDI Library. Point MIDI Library at the MIDI collection. MIDI Library Manager / Library Engine scans, analyzes, curates, and persists that collection. MIDI Remixer attaches to this curated Library and uses eligible committed results; it does **not** independently rebuild a second Library. Library owns the Library database schema/migration and lifecycle/eligibility truth. For Guided/Assisted, only persisted **Blessed + Mash Ready** records may enter recommendation pools; Version Fusion uses the persisted Fusion/version-family information.

The Manager window is the user interface. A detached Library Engine owns the writable databases and background workers. Completed work is committed incrementally so closing/restarting the application does not intentionally throw away finished batches.

---

## Installation and requirements

**Standalone status:** MIDI Library Manager is fully usable without MIDI Remixer. For detailed standalone installation and optional two-product setup, see **`INSTALL_GUIDE.md`**.

**Supported release target:** Windows 10/11, 64-bit.

1. Install 64-bit **Python 3.11, 3.12, or 3.13** if a compatible Python is not already installed.
2. Extract or clone the program to a writable folder.
3. Run **`Launch MIDI Library Manager.bat`**. The launcher reuses or prepares `%LOCALAPPDATA%\MIDI Remixer\PythonEnv`.
4. If you want to prepare dependencies separately, run **`Install Requirements.bat`** first.

Pinned Python dependencies are **PySide6 6.8.3**, **mido 1.3.3**, **rapidfuzz 3.14.3**, and **openpyxl 3.1.5**. The installer uses `requirements.lock`.

Fast/Deep scanning does not require an audio stack. Library audition additionally requires **FluidSynth** and a General MIDI **`.sf2`/`.sf3` SoundFont**, configured through **Audio Setup…** in the Browser.

---

## 2. First run: no saved Library yet

This first-run path is designed for large existing collections, including approximately **96,000 MIDI files**; it is not a toy small-library workflow.

A new user's first experience must not assume the prebuilt/drafted development Library exists.

### What you should see

After the Engine becomes ready and before a source folder is chosen:

- HOME state: **First run · add source or restore backup**
- Saved catalogue: **none yet**
- Fast Scan: waiting for a source folder
- Deep Analysis: **Waiting for Fast Scan**
- Mash Recommendation Readiness: **Waiting for catalogued MIDI**

The application may create empty SQLite database files as part of Engine initialization. Those empty files are implementation storage, not a usable saved Library, so Browse/Statistics/review actions should not be presented as though a catalogue already exists.

### Choose the correct first-run path

#### Start fresh

1. Choose **Add Folder…**.
2. Select the highest useful folder that contains the MIDI collection you want Library to manage. Subfolders are scanned recursively.
3. Fast Scan begins automatically after the source is added.
4. Leave **Performance** at **Maximum throughput** for fastest safe sustained work unless you intentionally prefer lower impact. Changing the performance mode can require an Engine restart before the worker count changes.
5. Leave **Auto-resume Deep** enabled if you want a new Engine lifetime to continue Deep Analysis automatically after its foreground Fast check.

#### Restore an existing Library backup

If this is the first launch on a replacement/new PC or Windows profile and you already have a MIDI Library backup, choose **Restore Library Data…** instead of rebuilding from zero. Restore recovers the saved Library databases and Engine settings, including the source-root configuration. Source MIDI files are not contained in the backup.

If those restored source paths do not exist on this PC (for example a different drive letter or folder), the saved catalogue remains browseable as an offline snapshot. Reconnect the original source path or deliberately remove/re-add the appropriate source folder before whole-Library reconciliation resumes. The Library must not interpret the unavailable restored path as deleted MIDI files.

### Important distinction: Fast Scan vs. the Deep preference

**Auto-resume Deep** does not mean “disable all Library work when unchecked.” On a new Engine lifetime, the Engine can still perform its incremental Fast source-change check. If this preference is off, Deep Analysis is not automatically resumed after that startup Fast work. Changing the checkbox affects future Engine starts; it does not silently pause or resume Deep Analysis in the Engine session that is already running. Use the Deep Analysis **Pause / Start / Resume** controls for current-session work.

### What happens next

Fast Scan proceeds in stages:

1. discover MIDI files;
2. compare filesystem changes with persistent state;
3. parse new/changed MIDI files into the Fast catalogue;
4. commit results in batches so useful rows appear before the entire collection is finished.

After Fast Scan releases foreground priority, Deep Analysis can continue. Recommendation readiness is then prepared from committed analysis. There is no meaningful fixed percentage denominator for relationship preparation, so HOME shows a calm state such as **Building recommendation matches…**, **Paused — Fast Scan has priority**, or **Ready · N families** rather than an animated percentage bar.

---

## 3. Returning user: a Library already exists

On later launches the Manager reconnects to the per-user databases. Saved aggregate counts can take a moment to load, especially for a very large Library. During that interval the Manager uses **loading…** wording rather than displaying temporary zero counts that could look like data loss.

With configured source folders, the Engine runs an incremental Fast change check. Unchanged files are reused. New, changed, or removed source files are reconciled with the saved catalogue.

If a configured source folder is temporarily unavailable (for example an offline drive), the saved catalogue is not automatically discarded. HOME identifies the configured source as unavailable and keeps committed Library data browseable. A normal whole-Library Fast Scan waits until **every configured source root is accessible**. This prevents a temporarily disconnected drive from being interpreted as thousands of deleted MIDI files.

### Updating the program does not normally reset the Library

Program files and user data are separate. Extracting a newer MIDI Library code folder under the same Windows user profile should attach to the existing per-user Library data.

A new Windows profile or another computer has a different application-data root. To transfer the Library intelligence, use **Backup Library Data…** on the old installation and **Restore Library Data…** on the new one.

---

## 4. HOME screen

### Header

**Browse Library…** opens the saved paged catalogue once Library rows exist.

**Statistics…** opens numeric catalogue/analysis/readiness totals once saved Library rows exist.

**Open MIDI Remixer** appears only when a compatible Remixer launcher is installed beside the Library component. It launches that sibling application; it is not part of Library scanning.

If the Engine has been stopped or becomes unavailable, the normally hidden Engine action can become **Start Library Engine** or **Retry Engine Start**. Those labels mean exactly that: start/reconnect the detached Library Engine without changing source MIDI files.

The right side shows the current Engine/processing state and saved-catalogue status.

### Fast Scan

Fast Scan is the single normal incremental Library scan.

**Start** is enabled only when every configured source folder is currently accessible and no Fast operation is active. Whole-Library reconciliation is intentionally blocked when even one configured root is offline, because a partial filesystem snapshot must not be mistaken for deletion from the missing source.

**Pause / Resume** pauses and resumes the active Fast operation at safe boundaries. While the Engine is reaching that boundary, the UI shows **Pausing…** rather than pretending the operation is already paused. A file already inside a supervised analyzer may need to be interrupted or reach its controlled boundary before the paused state is acknowledged.

**Cancel** stops future work in the current Fast pass. While cancellation is reaching its safe boundary, HOME and the Browser show **Cancelling…** consistently and prevent duplicate cancel requests. Already committed batches remain saved. A later incremental Fast Scan reconciles the remainder.

During initial preparation, the progress indicator is indeterminate until the Engine knows a real population. The UI must not invent a `0 / 1` total.

### Deep Analysis

Deep Analysis performs thorough musical profiling on MIDI files that Fast Scan successfully catalogued. Global Start/Resume and bulk Deep retry wait until every configured source is accessible. In the Browser, selected-row Deep operations are available only when the selected source file belongs to an accessible configured Library root. Saved completed analysis remains browseable while a source is offline.

The state-aware Deep button can mean:

- **Start / Resume** — unfinished normal Deep work exists;
- **Retry N Failed…** — first-attempt failures are eligible for one controlled bulk retry;
- **Review N Issues…** — the normal Deep pass is finished and review items remain;
- **Complete** — no unfinished/retry/review action is currently required.

On a true first run before any Fast catalogue exists, Deep Analysis should say **Waiting for Fast Scan**, not **Complete**.

**Pause** stops current-session Deep work at a safe resumable boundary; while that transition is in progress the UI shows **Pausing…**. **Cancel Deep Analysis** is a separate control for abandoning the current Deep pass without losing completed work; while the safe cancellation boundary is being reached the UI shows **Cancelling…** consistently: active supervised analyzers are preempted, queued in-memory work is discarded, and incomplete files remain persisted for **Start / Resume** later. **Completed files remain saved.** Cancel does not reject, quarantine, or delete the unfinished MIDI files.

### Mash Recommendation Readiness

This prepares persistent match/family information for later Guided/Assisted MIDI Remixer use.

Typical states are:

- **Paused — Fast Scan has priority**
- **Paused — Unreadable File Retry has priority**
- **Building recommendation matches in the background…**
- **Ready · N Guided-ready families**

No animated/banded percentage is used because there is no trustworthy stable denominator while the relationship population changes.

### Library & Controls

#### Performance

- **Maximum throughput** — fastest safe sustained setting
- **Balanced** — reduced background impact
- **Low impact** — minimum worker pressure

A changed worker mode can require stopping/restarting the Engine before the new worker count takes effect.

#### Auto-resume Deep

This is a **new-Engine-start preference**, not a current-session Pause button. When enabled, a newly started Engine may automatically continue Deep Analysis after its foreground Fast check. When disabled, Fast source-change checking can still occur, but the new Engine lifetime does not automatically resume Deep Analysis. Changing this checkbox does not interrupt or start Deep work that is already running now; use the Deep Analysis controls for current-session work.

#### Source folders

**Add Folder…** adds a recursive source root and starts the appropriate incremental Fast update.

**Remove…** removes the selected root from active Library configuration only after confirmation. It never deletes source MIDI files. If other sources remain, Fast reconciliation runs when all remaining configured roots are accessible and then removes saved rows that are no longer under any configured source. If you remove the **last** source, MIDI Library deliberately preserves the saved catalogue and completed analysis as an offline snapshot; add a source folder later to resume filesystem reconciliation. Do not rebuild merely because no active source is configured.

**Add Folder…** and **Remove…** are disabled while Fast Scan or an unreadable-file retry is active. Finish or cancel the foreground Fast operation before changing source topology.

#### Review & maintenance

**Unreadable Files (N)…** opens only files Fast Scan could not parse.

**Unusual but Loadable (N)…** opens successfully parsed files that look abnormal enough to review.

**Open Quarantine…** opens the preserved quarantine directory.

**Backup Library Data…** creates a verified backup of persistent Library data and Engine settings. Source MIDI files are not included.

**Restore Library Data…** verifies a backup, creates a pre-restore safety backup, proves the writer Engine is stopped, restores persistent Library state, and restarts the Engine. It remains available as a recovery action when the Engine cannot start. If the current Library cannot itself be verified well enough to create the required safety backup, restore stops before replacing anything and reports the problem. Source MIDI files are not replaced.

**Rebuild Library…** is a last-resort complete rebuild of persistent Fast/Deep/recommendation intelligence from the configured source folders. It is enabled only when all configured source folders are accessible. A verified rollback backup is created first. Derived stores are cleared before the authoritative Fast/lifecycle catalogue is reset; if a derived-store clear fails, the catalogue remains intact and the error reports the retained verified rollback backup path. Source MIDI files are not deleted.

**Stop Engine** stops Library scanning, Deep Analysis, recommendation preparation, and Library-owned analyzers while leaving the Manager window open.

---

## Persisted lifecycle and Remixer eligibility

The Library does not treat “scanned” as the final state. Each MIDI is persisted through **Discovered → Validated → Analyzed**, then resolves to **Blessed**, **Review Required**, **Rejected**, or **Quarantined**. Review items receive deliberate revalidation; a deterministic repeated failure can become Rejected rather than looping forever. If a rejected source later changes, the changed file may re-enter Review Required.

A Blessed MIDI has one persisted eligibility result: **Mash Ready**, **Fusion Candidate**, or **Usable — Not Mash Candidate**. HOME and Statistics show real database counts for these states plus Needs Review, Rejected, Quarantined, and Unresolved. Browser filters expose the same populations.

**No unresolved file enters Guided or Assisted recommendations.** Guided/Assisted consume only persisted **Blessed · Mash Ready** rows. Fusion candidates remain available to the Version Fusion workflow; usable non-mash files remain in the Library without being suggested as ordinary mash candidates.

### Composition-family reconciliation after upgrading to v0.39.2

The first Engine start after this maintenance upgrade may briefly report **reconciling composition families**. This pass reads the already-saved Deep Analysis database; it does **not** reopen every source MIDI and is not a new Fast/Deep scan. The purpose is to correct old filename/catalogue-only family hints using durable musical evidence.

- Same composition with materially different instrumentation/arrangement → one **Mash Ready** representative plus **Fusion Candidate** version(s).
- Exact duplicate or same composition without a meaningful arrangement difference → **Usable — Not Mash Candidate** for the redundant alternate.
- Different compositions that merely share a common chord progression → remain separate families.
- A file still waiting for Deep Analysis remains provisional until its own Deep result exists; the migration does not invent a permanent family from missing evidence.

Once the reconciliation completes, the Engine records the identity-ruleset version so normal restarts do not repeat the whole pass. New Deep completions are reconciled incrementally.

Rejected rows expose **Filename, Rejection / Review Reason, Last Validation, Recommended Action, File Size, and Location**. They may be shown in Explorer, opened in the default app, quarantined, or permanently deleted after confirmation. Quarantine is reversible: **Restore…** returns the file to its original configured source location and verifies it through Fast revalidation. An unchanged file that had already been Rejected stays Rejected; only a genuine source change reopens it for Review Required.

## 5. Library Browser

The Browser reads the saved catalogue in bounded pages rather than loading the entire collection into Qt at once. The current page size is 120 rows.

### Search and Filter

Search matches title, artist, filename, and folder/path information.

Filter choices separate normal catalogue state from review populations. The visible filters are:

- **All** — every saved Library row matching the Search text.
- **Deep Complete** — rows whose Deep Analysis completed.
- **Deep Unfinished** — catalogued rows whose Deep Analysis is not complete, including pending/paused/active/failure states.
- **Deep Pending** — waiting for Deep Analysis.
- **Deep Paused** — saved Deep work paused/interrupted at a recoverable boundary.
- **Deep Analyzing Now** — rows currently marked as being analyzed.
- **All Deep Analysis Issues** — every row whose Deep Analysis is currently failed/reviewable.
- **Deep Retry Available** — first-failure Deep rows for which the current workflow permits a controlled retry.
- **Deep Analysis Review** — persistent Deep review items after the controlled retry path is exhausted or no normal retry is appropriate.
- **Unreadable Files** — Fast Scan could not parse the source MIDI.
- **Unusual but Loadable** — Fast Scan succeeded, but the MIDI triggered an advisory health heuristic.
- **All File Review Items** — the combined Fast unreadable + unusual/loadable review population.

The important review workflows are described below.

### Actions… and right-click

The Browser is deliberately catalogue-first: selected-row commands do not occupy several permanent button rows underneath a 96K-song list. Select a MIDI and use **Actions…**, or right-click that row. Both gestures open the same menu. The menu includes **Play/Pause**, **Stop Audition**, **Keep Selected in Library · Not Mash…** when the selection is eligible for that review decision, **Open in Default App**, **Show in Folder**, Fast/Deep retry/rebuild actions, Restore, Quarantine, and Delete as applicable. Disabled items mean the selected row is not eligible for that operation; the menu does not silently substitute a different action.

**Retry Unreadable (N)…** remains directly visible because it is a Library-wide operation, not a selected-row command. **Refresh** reloads the saved catalogue summary and current Browser page while preserving the current row/selection where that MIDI remains on the page. **Previous** and **Next** move through the bounded 120-row result pages; they do not trigger a new Library scan.

When a Fast operation is active elsewhere, the Browser mirrors its progress and exposes the same control plane at the bottom. For a normal Fast Scan the buttons read **Pause Fast Scan / Resume Fast Scan** and **Cancel Fast Scan**. During the global unreadable retry they change to **Pause Retry / Resume Retry** and **Cancel Retry**. These control the already-running Engine operation; they do not start a second scan.

### Columns

**Status** says the current Library state of the row.

**Rejection / Review Reason** says what prevented resolution or why the row is being reviewed.

**Recommended Action** tells the user what to do next. A blank recommendation on a normal complete row means no corrective action is required.

Other musical columns include Duration, BPM, Key, Meter, Tracks, Notes, and source/path information. Visible sortable headers are backed by real catalogue-wide sort behavior.

---

## 6. Browser audition

Every Library view that presents selectable MIDI files follows the same transport contract:

- **Play/Pause** — one toggle button
- **Stop** — stops playback immediately and cleans up the Library-owned playback route
- **Volume** — remembered Library audition volume

Play targets exactly one highlighted readable MIDI. **Play is green when available.** While playing, that same button becomes **Pause** with an amber active state, and **Stop becomes red**. When paused the toggle returns to **Play** and resumes that row unless a different row is selected. When nothing is playing, Stop is disabled and visually neutral.

Fast-unreadable rows cannot be auditioned inside MIDI Library because the Library parser already could not read them. Use **Actions… → Open in Default App** or **Actions… → Show in Folder** instead.

### Audio Setup…

Library audition is self-contained and does **not** require MIDI Remixer to be installed. It needs two external audio resources:

- a working **FluidSynth** executable; and
- a General MIDI **SoundFont** (`.sf2` or `.sf3`).

Choose **Audio Setup…** beside the Browser Volume control. MIDI Library first shows any FluidSynth/SoundFont it can already discover from saved/shared locations. Choose **Use Detected** when both are correct, or **Choose Files…** to select them yourself. Stop audition before changing audio configuration. The next Play request performs the real runtime verification; if the chosen executable/bank cannot be used, Library reports the playback error without requiring Remixer.

The Python/package installer does not itself promise to install a SoundFont. A Library-only user should use **Audio Setup…** if automatic discovery does not find suitable audio resources.

---

## 7. Source-file actions

These actions require exactly one selected row.

### Open File

Asks Windows to open the actual selected `.mid`, `.midi`, or `.kar` file in the application's Windows-associated external program. MIDI Library is not the player used by this command.

### Show in Folder

Opens Windows Explorer with the exact selected source file highlighted.

These are asynchronous read-only OS requests. They remain available for exactly one saved row even when the Library source is offline; if Windows cannot open/reveal the saved path, the Browser reports that result without freezing the UI. The Library does not infer that an offline source means the MIDI was deleted. Library audition and source-mutating scan/re-analysis/quarantine/delete controls use the stricter accessible-source rules.

---

## 8. Fast Scan actions in the Browser

### Re-scan Selected…

Forces a fresh Fast parse of the selected eligible source MIDI file(s), bypassing unchanged-file reuse. Use this after repairing/replacing a source file externally or after a Library scanner update.

This command **re-scans**; it does not repair MIDI bytes. It is enabled only for selected files under a currently accessible configured source.

### Retry Unreadable (N)…

This is a **global** action, not a selected-row action. It retries the complete currently persisted unreadable population with a deliberately bounded worker limit because failed files are more likely to be pathological.

Do not repeatedly run this with no relevant change. Because this command spans the entire unreadable population, it is enabled only when **all configured source roots are accessible**. If the same files remain unreadable, use **Rejection / Review Reason**, **Recommended Action**, **Show in Folder**, quarantine, or deletion as appropriate.

---

## 9. Deep Analysis actions in the Browser

### Rebuild Analysis…

Available for selected MIDI files whose Deep Analysis previously completed. It discards/rebuilds derived Deep/recommendation data for those rows. It does not modify the source MIDI or Fast catalogue row.

### Retry Analysis…

Available only for failed Deep Analysis categories where another attempt can plausibly succeed after a relevant source, access, analyzer, or software change.

It is intentionally unavailable for deterministic current **Keep** categories such as valid/unsupported Type 2 or a current unchanged resource/safety limit. Repeating the identical operation would not constitute a repair.

### Keep in Library · Not Mash…

This is an explicit human review decision for a readable MIDI that you want to retain even though current Deep Analysis cannot fully profile it. It is appropriate for cases such as a valid MIDI Type 2 file or a current safety/resource-limit case that you have reviewed and intentionally want to keep.

The source MIDI is **not changed**. Library resolves the row as **Usable — Not Mash Candidate**, removes any stale prepared mash relationship for that file, and excludes it from normal Guided/Assisted recommendation pools. This action is not available for Fast-unreadable files or malformed/corrupt rejection categories.

---

## 10. The three main review populations

### Unreadable Files

Meaning: Fast Scan could not parse the source MIDI.

Recommended workflow:

1. Read **Rejection / Review Reason** and **Recommended Action**.
2. Use **Show in Folder** to inspect the actual source.
3. If you repair/replace the source externally, use **Re-scan Selected…**.
4. After a Library scanner update, **Retry Unreadable (N)…** can retry the whole unreadable population.
5. If still unreadable, keep it outside the active Library with **Quarantine…** when uncertain, or permanently remove it with **Delete…** only when that is intentional.

MIDI Library does not repair the source MIDI itself.

### Unusual but Loadable

Meaning: Fast Scan succeeded, but the MIDI is unusually short, long, sparse, empty, or otherwise abnormal according to review heuristics.

These files are loadable. No retry is normally required. Audition/review them and keep valid files. Quarantine or delete only when the user actually wants that file-management result.

### Deep Analysis Review

Meaning: Fast Scan succeeded but thorough analysis could not finish normally.

This does **not** automatically mean the MIDI is damaged. Examples include:

- **Valid/unsupported MIDI Type 2** — currently a Keep/review state; the source can be valid.
- **Resource/safety limit** — analysis hit a safety boundary; the source can still be valid.
- analyzer/internal error — retry may become appropriate after a Library update.
- missing/access problem — restore access/source first, then retry.
- likely malformed source — inspect or repair externally, then re-scan/retry.

Follow **Recommended Action** rather than assuming every Deep review item should be quarantined or deleted.

For a valid review item you want to keep, select it and choose **Keep in Library · Not Mash…** (or the same right-click action). That resolves the lifecycle decision instead of leaving a known-valid analyzer-limited file permanently in **Needs Review**.

---

## 11. Quarantine and Delete

**Quarantine…** moves eligible selected source files outside active Library roots into the preserved MIDI Library quarantine area. It requires the selected source file to be under a currently accessible configured Library root. Prefer quarantine when uncertain.

**Delete…** permanently deletes only **Rejected** source files, or files already in **Quarantine**, after confirmation. Active-source deletion requires that source to be accessible. The confirmed file is staged before Library records are retired, so a database-cleanup failure restores the source instead of leaving a missing file with stale Library state. If Windows permits staging but refuses the final erase, MIDI Library reports the retained `pending-delete` copy explicitly rather than claiming success. An offline saved snapshot is reviewable, not destructively editable.

**Restore…** is available for Quarantined rows. It restores the file to its original configured and currently accessible source location without overwriting an existing file, then runs a Fast verification pass. A file quarantined from **Needs Review** returns to that review path. An unchanged file quarantined after **Rejected** remains **Rejected**; a genuine content/signature change reopens it as **Needs Review**.

Neither action is enabled merely because Deep Analysis has a review item. File-health/deletion decisions are separate from Deep-analysis capability.

---

## 12. Backup, restore, updates, and moving computers

### Backup Library Data…

A Library backup can contain:

- Fast catalogue database
- Deep Analysis database
- prepared recommendation/relationship database
- discovery/reference evidence
- Library Engine settings, including configured source roots
- backup manifest/schema information

Source MIDI files are **not** included.

### Restore Library Data…

Restore verifies the selected backup before replacing current Library state. A separate verified safety backup of the pre-restore Library is created first. When the Engine is running, the Engine creates that backup before stopping; when Engine startup has already failed, the Manager first proves no detached writer remains and creates the safety backup from the quiescent Library files. If that safety backup cannot be verified, restore does not replace anything. Restored SQLite databases are integrity-checked and the Manager then attempts to start the Engine.

### Updating the program folder

The persistent Library is not stored beside the extracted code by default. Updating/replacing the program folder under the same Windows user profile should continue using the same saved per-user Library.

### Moving to another PC or Windows user

Use Library Backup/Restore. Copying only the code folder does not carry the other profile's per-user Library databases. After restore, the saved source-root configuration also returns. If the new computer uses a different drive letter/folder, HOME reports that source as unavailable; the restored catalogue remains an offline snapshot until you reconnect or deliberately reconfigure the source.

---

## 13. Persistent data locations on Windows

Normal persistent data is stored under the current user's application-data root, normally:

`%APPDATA%\MIDI Remixer\`

Typical subfolders:

- `library\` — `library_scan.sqlite3`, `library_index.sqlite3`, `library_relationships.sqlite3`, and other persistent Library data
- `config\` — `library_engine_settings.json`
- `backups\` — Library backup ZIPs when the default location is used
- `logs\` — startup/runtime diagnostics

Portable QA/development mode can intentionally store data beside the application, but that is not the normal installed-user path.

---

## 14. Closing and stopping the application

Closing MIDI Library Manager defaults to **Stop Engine and Exit**. The Manager requests bounded cleanup of Fast/Deep work, recommendation preparation, analyzer subprocesses, Browser audio, and the detached Engine. The window closes only after process-tree terminality is verified or a bounded failure is reported.

The close dialog's alternative is **Close Window Only · Keep Background Engine Running**. Choose it only when you intentionally want background Library Engine/analyzer processes to remain after the Manager window closes. **Stop Engine and Exit** is the default full-exit choice.

**Stop Engine** is different: it stops the Engine but keeps the Manager window open, allowing saved Library inspection. The same header action then becomes **Start Library Engine**; if startup/attachment fails it can appear as **Retry Engine Start**.

---

## 15. If something looks wrong

### The app briefly says loading

For a large returning Library, aggregate counts are loaded asynchronously. **Loading…** is expected for a short period and is safer than displaying temporary zero counts.

### Saved catalogue exists but no source is currently usable

There are two different states:

- **No active source configured** — the saved Library is an intentional offline snapshot. Use **Add Folder…** when you want filesystem reconciliation to resume.
- **Configured source unavailable** — the saved path is still configured but Windows cannot currently access it (for example an offline/removable drive). Reconnect that drive/path, or remove/reconfigure the source deliberately. If several sources are configured, one offline root blocks whole-Library reconciliation; the Engine does not scan only the remaining roots and infer that the offline collection was deleted.

Do not rebuild simply because a source is temporarily unavailable. Saved committed catalogue data remains separate from source-folder availability. Browsing and saved review remain available; source-dependent scan/re-analysis/quarantine/delete operations wait for the relevant source, and global reconciliation/retry/rebuild waits for all configured roots.

### Fast Scan finds many unreadable files

Do not repeatedly retry them without a reason. Review the saved issue, inspect sources with **Show in Folder**, and retry after a file/scanner/access change.

### Deep Analysis has review items

Use **Rejection / Review Reason** and **Recommended Action**. Some review items are Keep states and require no repair.

### The Manager cannot close

MIDI Library Manager must not require Task Manager for normal full exit. If the bounded shutdown fails, treat it as a product defect and preserve the displayed shutdown/error state for diagnosis rather than normalizing a manual kill as expected behavior.

---

## 16. MIDI Remixer integration

MIDI Library Manager remains independently installable. MIDI Remixer attaches to the authoritative Library Engine/contract rather than owning a second full scan or calculating a separate database path. MIDI Remixer must not perform Library database migration or independently qualify unresolved/questionable files.

Fast/Deep analysis completed by Library is reusable by Remixer through the versioned Library Contract. Newly Blessed + Mash Ready material becomes eligible to Guided/Assisted; Rejected or Quarantined material stops being eligible; stale relationship rows cannot override current Library eligibility.

### Migration failure during an update

MIDI Library treats the persistent Library as valuable user data. If startup discovers a supported legacy Library database but cannot migrate it safely, the Library Engine stops startup rather than silently creating or opening a partial replacement Library. The Manager surfaces the startup error, and the legacy database files are retained for retry. Do **not** delete the legacy Library files to make the message disappear. Resolve the storage/database problem or restore a verified backup, then use **Retry Engine Start**. A failed migration is not treated as a first run and does not reset the curated Library.

---

## License

MIDI Library Manager is provided under the **PolyForm Noncommercial License 1.0.0**. See `LICENSE.md` for the complete terms and the required notice `Copyright © 2026 Kevin Horst`. The license permits noncommercial purposes as defined by its terms; commercial use is not granted by this license. Third-party components and user content keep their own applicable licenses.
