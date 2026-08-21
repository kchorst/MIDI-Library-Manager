CREATE TABLE IF NOT EXISTS library_catalog_files_v11 (
    path TEXT PRIMARY KEY,
    filename TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    modified_ns INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    midi_type INTEGER,
    ticks_per_beat INTEGER,
    track_count INTEGER NOT NULL,
    active_channel_count INTEGER NOT NULL,
    note_count INTEGER NOT NULL,
    duration_seconds REAL,
    bpm REAL,
    bpm_min REAL,
    bpm_max REAL,
    tempo_change_count INTEGER NOT NULL,
    detected_key TEXT,
    mode TEXT,
    key_confidence REAL NOT NULL,
    key_source TEXT NOT NULL,
    meter TEXT,
    arrangement_type TEXT NOT NULL,
    program_families_json TEXT NOT NULL,
    embedded_text_json TEXT NOT NULL,
    composition_id TEXT,
    catalog_artist TEXT,
    catalog_title TEXT,
    catalog_genres TEXT,
    catalog_primary_genre TEXT,
    catalog_confidence REAL NOT NULL,
    catalog_status TEXT NOT NULL,
    catalog_evidence_json TEXT NOT NULL,
    duplicate_group TEXT,
    composition_group TEXT,
    content_duplicate_group TEXT,
    duplicate_kind TEXT NOT NULL,
    musical_fingerprint TEXT,
    quick_status TEXT NOT NULL,
    deep_status TEXT NOT NULL DEFAULT 'pending',
    error TEXT,
    last_scanned_utc TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    family_id TEXT,
    deep_priority INTEGER NOT NULL DEFAULT 70,
    deep_updated_utc TEXT,
    deep_attempt_count INTEGER NOT NULL DEFAULT 0,
    deep_last_error TEXT,
    deep_last_duration_seconds REAL
);
CREATE INDEX IF NOT EXISTS idx_library_sha256 ON library_catalog_files_v11(sha256);
CREATE INDEX IF NOT EXISTS idx_library_composition ON library_catalog_files_v11(composition_id);
CREATE INDEX IF NOT EXISTS idx_library_duplicate_group ON library_catalog_files_v11(duplicate_group);
CREATE INDEX IF NOT EXISTS idx_library_deep_status ON library_catalog_files_v11(deep_status);
CREATE TABLE IF NOT EXISTS library_scan_settings_v11 (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ignored_mash_pairs_v12 (
    identity_a TEXT NOT NULL,
    identity_b TEXT NOT NULL,
    label_a TEXT NOT NULL,
    label_b TEXT NOT NULL,
    created_utc TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(identity_a, identity_b),
    CHECK(identity_a < identity_b)
);
CREATE INDEX IF NOT EXISTS idx_ignored_mash_pairs_created_v12 ON ignored_mash_pairs_v12(created_utc);
