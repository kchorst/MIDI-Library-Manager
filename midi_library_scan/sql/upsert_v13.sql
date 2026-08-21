INSERT INTO library_catalog_files_v11 (
            path,filename,size_bytes,modified_ns,sha256,midi_type,ticks_per_beat,track_count,
            active_channel_count,note_count,duration_seconds,bpm,bpm_min,bpm_max,tempo_change_count,
            detected_key,mode,key_confidence,key_source,meter,arrangement_type,program_families_json,
            embedded_text_json,composition_id,catalog_artist,catalog_title,catalog_genres,
            catalog_primary_genre,catalog_confidence,catalog_status,catalog_evidence_json,
            duplicate_group,composition_group,content_duplicate_group,duplicate_kind,musical_fingerprint,quick_status,error,
            family_id,deep_priority,last_scanned_utc
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)
        ON CONFLICT(path) DO UPDATE SET
            filename=excluded.filename,size_bytes=excluded.size_bytes,modified_ns=excluded.modified_ns,
            sha256=excluded.sha256,midi_type=excluded.midi_type,ticks_per_beat=excluded.ticks_per_beat,
            track_count=excluded.track_count,active_channel_count=excluded.active_channel_count,
            note_count=excluded.note_count,duration_seconds=excluded.duration_seconds,bpm=excluded.bpm,
            bpm_min=excluded.bpm_min,bpm_max=excluded.bpm_max,tempo_change_count=excluded.tempo_change_count,
            detected_key=excluded.detected_key,mode=excluded.mode,key_confidence=excluded.key_confidence,
            key_source=excluded.key_source,meter=excluded.meter,arrangement_type=excluded.arrangement_type,
            program_families_json=excluded.program_families_json,embedded_text_json=excluded.embedded_text_json,
            composition_id=excluded.composition_id,catalog_artist=excluded.catalog_artist,
            catalog_title=excluded.catalog_title,catalog_genres=excluded.catalog_genres,
            catalog_primary_genre=excluded.catalog_primary_genre,catalog_confidence=excluded.catalog_confidence,
            catalog_status=excluded.catalog_status,catalog_evidence_json=excluded.catalog_evidence_json,
            duplicate_group=excluded.duplicate_group,composition_group=excluded.composition_group,
            content_duplicate_group=excluded.content_duplicate_group,duplicate_kind=excluded.duplicate_kind,
            musical_fingerprint=excluded.musical_fingerprint,quick_status=excluded.quick_status,
            family_id=excluded.family_id,
            deep_status=CASE WHEN library_catalog_files_v11.sha256 <> excluded.sha256 OR library_catalog_files_v11.modified_ns <> excluded.modified_ns THEN 'pending' ELSE library_catalog_files_v11.deep_status END,
            deep_priority=CASE WHEN library_catalog_files_v11.sha256 <> excluded.sha256 OR library_catalog_files_v11.modified_ns <> excluded.modified_ns THEN MIN(library_catalog_files_v11.deep_priority,excluded.deep_priority) ELSE library_catalog_files_v11.deep_priority END,
            error=excluded.error,last_scanned_utc=CURRENT_TIMESTAMP
