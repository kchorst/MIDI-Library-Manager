from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any

from core.atomic_io import atomic_write_json

ENGINE_PROTOCOL_VERSION = 1
ENGINE_API_VERSION = "1.0"
LIBRARY_CONTRACT_VERSION = "4"
ENGINE_PRODUCT_VERSION = "0.36.3"
ENGINE_CAPABILITIES = frozenset({
    "background_analysis",
    "catalogue_deltas",
    "incremental_scan",
    "library_health",
    "persisted_lifecycle_eligibility",
    "library_review_retry",
    "review_disposition",
    "library_backup_restore",
    "maintenance",
    "prepared_recommendations",
    "related_candidates",
    "read_committed_library",
    "storage_contract",
    "reference_evidence",
    "versioned_api",
})
ENDPOINT_FILENAME = "library_engine_endpoint.json"


@dataclass(frozen=True, slots=True)
class EngineEndpoint:
    host: str
    port: int
    token: str
    pid: int
    protocol_version: int = ENGINE_PROTOCOL_VERSION
    api_version: str = ENGINE_API_VERSION
    source_root: str = ""
    force_kill_guard: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "host": self.host,
            "port": int(self.port),
            "token": self.token,
            "pid": int(self.pid),
            "protocol_version": int(self.protocol_version),
            "api_version": self.api_version,
            "source_root": self.source_root,
            "force_kill_guard": self.force_kill_guard,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "EngineEndpoint":
        return cls(
            host=str(data.get("host") or "127.0.0.1"),
            port=int(data.get("port") or 0),
            token=str(data.get("token") or ""),
            pid=int(data.get("pid") or 0),
            protocol_version=int(data.get("protocol_version") or 0),
            api_version=str(data.get("api_version") or ""),
            source_root=str(data.get("source_root") or ""),
            force_kill_guard=str(data.get("force_kill_guard") or ""),
        )


def endpoint_path(library_root: str | Path) -> Path:
    return Path(library_root) / ENDPOINT_FILENAME


def write_endpoint(path: str | Path, endpoint: EngineEndpoint) -> None:
    atomic_write_json(Path(path), endpoint.as_dict())


def read_endpoint(path: str | Path) -> EngineEndpoint | None:
    source = Path(path)
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    try:
        endpoint = EngineEndpoint.from_dict(payload)
    except (TypeError, ValueError):
        return None
    if endpoint.protocol_version != ENGINE_PROTOCOL_VERSION or not endpoint.token or endpoint.port <= 0:
        return None
    return endpoint
