from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from core.atomic_io import atomic_write_json
import json

from core.multi_source import MultiSourceSettings, SourceSpec
from core.remix_engine import RemixSettings, RemixTake
from core.remix_version import APP_VERSION


@dataclass
class MidiRemixerProject:
    app_name: str = "MIDI Remixer"
    app_version: str = APP_VERSION
    project_version: int = 10
    library_paths: list[str] = field(default_factory=list)
    sources: list[dict] = field(default_factory=list)
    anchor_path: str | None = None
    output_directory: str | None = None
    soundfont_path: str | None = None
    settings: dict = field(default_factory=lambda: asdict(MultiSourceSettings()))
    takes: list[dict] = field(default_factory=list)
    guidance_mode: str = "guided"
    trials: list[dict] = field(default_factory=list)
    active_trial_id: str | None = None
    session_id: str | None = None
    decision_history: list[dict] = field(default_factory=list)
    listening_reviews: list[dict] = field(default_factory=list)
    # Compatibility fields for projects created before the multi-source model.
    source_a: str | None = None
    source_b: str | None = None

    def set_source_specs(self, specs: list[SourceSpec]) -> None:
        self.sources = [asdict(spec) for spec in specs]
        enabled = [spec for spec in specs if spec.enabled]
        self.anchor_path = enabled[0].path if enabled else None
        self.source_a = enabled[0].path if enabled else None
        self.source_b = enabled[1].path if len(enabled) > 1 else None

    def source_specs(self) -> list[SourceSpec]:
        if self.sources:
            result: list[SourceSpec] = []
            for item in self.sources:
                try:
                    result.append(SourceSpec(**item))
                except TypeError:
                    continue
            if result:
                return result
        legacy = [path for path in (self.source_a, self.source_b) if path]
        return [SourceSpec(path=path, role="anchor" if index == 0 else "auto") for index, path in enumerate(legacy)]

    def set_takes(self, takes: list[RemixTake]) -> None:
        self.takes = [take.to_dict() for take in takes]

    def save(self, path: str | Path) -> Path:
        target = Path(path)
        if target.suffix.lower() != ".miremix":
            target = target.with_suffix(".miremix")
        target.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(target, asdict(self))
        return target

    @classmethod
    def load(cls, path: str | Path) -> "MidiRemixerProject":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        project = cls()
        valid = set(project.__dataclass_fields__.keys())
        for key, value in data.items():
            if key in valid:
                setattr(project, key, value)
        if not project.sources and (project.source_a or project.source_b):
            project.set_source_specs(project.source_specs())
        return project
