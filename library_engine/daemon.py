from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import secrets
import signal
import socketserver
import threading
import time
from typing import Any

from core.app_paths import AppPaths
from core.atomic_io import atomic_write_json
from core.runtime_cleanup import runtime_supervisor
from library_engine.protocol import (
    ENGINE_API_VERSION, ENGINE_CAPABILITIES, ENGINE_PRODUCT_VERSION, ENGINE_PROTOCOL_VERSION, LIBRARY_CONTRACT_VERSION,
    EngineEndpoint, endpoint_path, write_endpoint,
)
from library_engine.service import LibraryEngineService
from library_engine.timing import ENGINE_SHUTDOWN_HARD_SECONDS


class _EngineServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, handler, *, source_root: Path, token: str):
        super().__init__(address, handler)
        self.service: LibraryEngineService | None = None
        self.source_root = Path(source_root).resolve()
        self.token = token
        self.shutdown_requested = threading.Event()
        self.startup_state = "opening persistent library"
        # ThreadingTCPServer may have several admitted commands in flight when a
        # shutdown request arrives. Track them without serializing normal IPC so
        # daemon teardown can stop admission, drain current commands, and only
        # then close shared SQLite owners.
        self._command_condition = threading.Condition()
        self._active_service_commands = 0

    def begin_service_command(self) -> bool:
        with self._command_condition:
            if self.shutdown_requested.is_set():
                return False
            self._active_service_commands += 1
            return True

    def end_service_command(self) -> None:
        with self._command_condition:
            self._active_service_commands = max(0, self._active_service_commands - 1)
            self._command_condition.notify_all()

    def wait_for_service_commands(self, timeout: float) -> bool:
        deadline = time.monotonic() + max(0.0, float(timeout))
        with self._command_condition:
            while self._active_service_commands > 0:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._command_condition.wait(timeout=remaining)
            return True

    def attach_service(self, service: LibraryEngineService) -> None:
        self.service = service
        self.startup_state = "starting background work"


class _Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        raw = self.rfile.readline(4 * 1024 * 1024)
        if not raw:
            return
        try:
            request = json.loads(raw.decode("utf-8"))
            if not isinstance(request, dict):
                raise ValueError("Request must be a JSON object")
            if str(request.get("token") or "") != self.server.token:
                raise PermissionError("Invalid Library Engine token")
            if int(request.get("protocol_version") or 0) != ENGINE_PROTOCOL_VERSION:
                raise RuntimeError("Library Engine protocol version mismatch")
            command = str(request.get("command") or "").casefold()
            tracked = command not in {"ping", "shutdown"}
            admitted = False
            if tracked:
                admitted = bool(self.server.begin_service_command())
                if not admitted:
                    raise RuntimeError("Library Engine is shutting down; new commands are not accepted.")
            try:
                response = self._dispatch(request)
            finally:
                if admitted:
                    self.server.end_service_command()
        except Exception as exc:
            response = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        self.wfile.write((json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8"))

    @property
    def engine(self) -> LibraryEngineService:
        service = self.server.service
        if service is None:
            raise RuntimeError("Library Engine is still opening its persistent databases.")
        return service

    def _dispatch(self, request: dict[str, Any]) -> dict[str, Any]:
        command = str(request.get("command") or "").casefold()
        if command == "ping":
            return {
                "ok": True,
                "engine_version": ENGINE_PRODUCT_VERSION,
                "api_version": ENGINE_API_VERSION,
                "protocol_version": ENGINE_PROTOCOL_VERSION,
                "library_contract_version": LIBRARY_CONTRACT_VERSION,
                "pid": os.getpid(),
                "source_root": str(self.server.source_root),
                "capabilities": sorted(ENGINE_CAPABILITIES),
                "service_ready": self.server.service is not None,
            }
        if command == "status" and self.server.service is None:
            # The endpoint intentionally exists before SQLite/schema opening.
            # Return a valid lightweight startup snapshot so clients never
            # mistake a large one-time database open/migration for a dead Engine.
            return {"ok": True, "status": {
                "engine": "starting",
                "startup": {"state": self.server.startup_state, "running": True, "error": ""},
                "roots": [],
                "catalogue": {},
                "deep": {"state": "starting", "remaining": 0, "failed": 0, "complete": 0, "total": 0},
                "relationships": {},
                "readiness": {},
                "scan": {"running": False, "paused": False},
                "recent_errors": [],
            }}
        if command == "shutdown":
            self.server.shutdown_requested.set()
            threading.Thread(target=self.server.shutdown, name="LibraryEngineShutdown", daemon=True).start()
            return {"ok": True, "shutting_down": True}
        if command == "status":
            return {"ok": True, "status": self.engine.status()}
        if command == "configure_roots":
            roots = self.engine.configure_roots(request.get("roots") or [])
            return {"ok": True, "roots": roots}
        if command == "set_throughput":
            return {"ok": True, **self.engine.set_throughput_mode(str(request.get("mode") or "maximum"))}
        if command == "set_auto_start":
            return {"ok": True, "enabled": self.engine.set_manager_auto_start(bool(request.get("enabled", True)))}
        if command == "scan":
            return {"ok": True, "started": self.engine.start_incremental_scan()}
        if command == "scan_paths":
            return {"ok": True, "started": self.engine.start_scan_paths(
                request.get("paths") or [], replace_existing=bool(request.get("replace_existing", False)),
                include_subfolders=bool(request.get("include_subfolders", True)),
            )}
        if command in {"start_maximum", "start_analysis"}:
            return {"ok": True, "started": bool(self.engine.start_maximum_analysis())}
        if command == "pause_analysis":
            self.engine.pause_analysis()
            return {"ok": True}
        if command == "resume_analysis":
            return {"ok": True, "resumed": bool(self.engine.resume_analysis())}
        if command == "cancel_analysis":
            self.engine.cancel_analysis()
            return {"ok": True, "cancelled": True}
        if command == "retry_failed":
            paths = request.get("paths")
            return {"ok": True, "retried": self.engine.retry_failed(paths if paths else None)}
        if command == "rescan_paths":
            return {"ok": True, **self.engine.rescan_paths(request.get("paths") or [])}
        if command == "rescan_failed_fast":
            return {"ok": True, **self.engine.rescan_failed_fast()}
        if command == "reanalyze_paths":
            return {"ok": True, "requeued": self.engine.reanalyze_paths(request.get("paths") or [])}
        if command == "keep_reviewed_not_mash":
            return {"ok": True, **self.engine.keep_reviewed_not_mash(request.get("paths") or [])}
        if command == "prioritize":
            changed = self.engine.prioritize_paths(request.get("paths") or [], int(request.get("priority") or 0))
            return {"ok": True, "prioritized": changed}
        if command == "prepare_anchor":
            return {"ok": True, **self.engine.prepare_anchor(str(request.get("path") or ""))}
        if command == "suspicious_candidates":
            return {"ok": True, "candidates": self.engine.suspicious_candidates(limit=int(request.get("limit") or 5000))}
        if command == "remove_paths":
            return {"ok": True, **self.engine.remove_paths(
                request.get("paths") or [], permanent=bool(request.get("permanent", False))
            )}
        if command == "restore_quarantine":
            return {"ok": True, **self.engine.restore_quarantined_paths(request.get("paths") or [])}
        if command == "catalogue_changes":
            return {"ok": True, **self.engine.catalogue_changes(int(request.get("since_revision") or 0))}
        if command == "related_candidates":
            paths = self.engine.related_candidate_paths(
                str(request.get("anchor_path") or ""), limit=int(request.get("limit") or 256)
            )
            return {"ok": True, "paths": list(paths)}
        if command == "recommended_pairs":
            pairs = self.engine.recommended_pairs(
                mode=str(request.get("mode") or "guided"),
                limit=int(request.get("limit") or 64), offset=int(request.get("offset") or 0),
                anchor_path=str(request.get("anchor_path") or "") or None,
            )
            return {"ok": True, "pairs": pairs}
        if command == "ignore_pair":
            identity_a = str(request.get("identity_a") or "")
            identity_b = str(request.get("identity_b") or "")
            self.engine.scan.database.ignore_pair(
                identity_a, identity_b, str(request.get("label_a") or "Song A"), str(request.get("label_b") or "Song B"),
            )
            self.engine.record_feedback(identity_a, identity_b, "ignore")
            return {"ok": True}
        if command == "restore_pair":
            identity_a = str(request.get("identity_a") or "")
            identity_b = str(request.get("identity_b") or "")
            self.engine.scan.database.restore_pair(identity_a, identity_b)
            self.engine.restore_feedback(identity_a, identity_b)
            return {"ok": True}
        if command == "record_feedback":
            self.engine.record_feedback(
                str(request.get("identity_a") or ""), str(request.get("identity_b") or ""),
                str(request.get("action") or ""),
            )
            return {"ok": True}
        if command == "refresh_reference_evidence":
            return {"ok": True, "discovery_graph": self.engine.refresh_reference_evidence()}
        if command == "pause_scan":
            return {"ok": True, "paused": self.engine.pause_scan()}
        if command == "resume_scan":
            return {"ok": True, "resumed": self.engine.resume_scan()}
        if command == "cancel_scan":
            self.engine.stop_scan()
            return {"ok": True}
        if command == "rebuild_library":
            return {"ok": True, **self.engine.rebuild_library_intelligence(
                backup_target=str(request.get("backup_target") or "") or None
            )}
        if command == "backup":
            return {"ok": True, "path": self.engine.backup(str(request.get("target") or ""))}
        raise ValueError(f"Unknown Library Engine command: {command}")


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _acquire_start_lock(path: Path) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    for _attempt in range(2):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode("ascii"))
            os.fsync(fd)
            return fd
        except FileExistsError:
            try:
                existing = int(path.read_text(encoding="ascii").strip() or "0")
            except (OSError, ValueError):
                existing = 0
            if existing and _pid_alive(existing):
                raise RuntimeError(f"MIDI Library Engine already appears to be starting/running as PID {existing}.")
            try:
                path.unlink()
            except OSError:
                pass
    raise RuntimeError("Could not acquire MIDI Library Engine startup lock.")


def run(app_root: str | Path) -> int:
    root = Path(app_root).resolve()
    paths = AppPaths.for_app(root).ensure()
    lock_path = paths.library / "library_engine_start.lock"
    lock_fd = _acquire_start_lock(lock_path)
    endpoint_file = endpoint_path(paths.library)
    service: LibraryEngineService | None = None
    server: _EngineServer | None = None
    server_thread: threading.Thread | None = None
    try:
        # QA-only deterministic race injection: target-Windows qualification can
        # hold the detached process after it owns the startup lock but before it
        # publishes an endpoint.  Manager full-exit must still cancel/reap this
        # exact process rather than closing first and leaving Python behind.
        try:
            qa_pre_endpoint_delay = max(0.0, min(20.0, float(
                os.environ.get("MIDI_LIBRARY_QA_STARTUP_DELAY_BEFORE_ENDPOINT", "0") or 0
            )))
        except ValueError:
            qa_pre_endpoint_delay = 0.0
        if qa_pre_endpoint_delay > 0.0:
            time.sleep(qa_pre_endpoint_delay)

        token = secrets.token_urlsafe(32)
        # Bring up the localhost heartbeat before opening/migrating potentially
        # large persistent SQLite stores.  The lifetime PID lock already
        # guarantees this is the only writer, so early IPC is safe.
        server = _EngineServer(("127.0.0.1", 0), _Handler, source_root=root, token=token)
        host, port = server.server_address
        endpoint = EngineEndpoint(
            str(host), int(port), token, os.getpid(), source_root=str(root),
            force_kill_guard=secrets.token_urlsafe(24),
        )
        write_endpoint(endpoint_file, endpoint)
        server_thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.25},
            name="MidiLibraryEngineIPC", daemon=True,
        )
        server_thread.start()

        # QA-only deterministic database-opening boundary. The Engine already
        # owns its lifetime lock and answers heartbeat/status, but persistent
        # LibraryDatabase construction/migration has not started yet. This lets
        # the target-Windows Manager prove Close during database opening without
        # relying on machine-speed timing.
        try:
            qa_post_endpoint_delay = max(0.0, min(20.0, float(
                os.environ.get("MIDI_LIBRARY_QA_STARTUP_DELAY_AFTER_ENDPOINT", "0") or 0
            )))
        except ValueError:
            qa_post_endpoint_delay = 0.0
        if qa_post_endpoint_delay > 0.0:
            server.startup_state = "opening persistent library databases"
            # This is a QA-only deterministic startup boundary after IPC is
            # already live. A real shutdown request must interrupt the hold
            # immediately rather than wait out an artificial sleep.
            if server.shutdown_requested.wait(qa_post_endpoint_delay):
                if server_thread.is_alive():
                    server.shutdown()
                return 0

        if str(os.environ.get("MIDI_LIBRARY_QA_FAIL_STARTUP_AFTER_ENDPOINT", "")).strip().casefold() in {
            "1", "true", "yes", "on",
        }:
            raise RuntimeError("QA injected Engine startup failure after endpoint publication")

        server.startup_state = "opening persistent library databases"
        service = LibraryEngineService(root)
        server.attach_service(service)
        service.start_background()

        def stop_handler(_signum, _frame) -> None:
            if server is not None:
                server.shutdown_requested.set()
                threading.Thread(target=server.shutdown, name="LibraryEngineSignalShutdown", daemon=True).start()

        try:
            signal.signal(signal.SIGTERM, stop_handler)
            signal.signal(signal.SIGINT, stop_handler)
        except (ValueError, OSError):
            pass

        # serve_forever owns IPC on its dedicated thread so the main daemon
        # thread can supervise startup/lifetime without blocking heartbeat.
        while server_thread.is_alive() and not server.shutdown_requested.wait(0.5):
            pass
        if server_thread.is_alive():
            server.shutdown()
        return 0
    finally:
        # Daemon teardown has one absolute budget shared by IPC and the Engine
        # service. The client wait margin is deliberately longer than this.
        shutdown_deadline = time.monotonic() + ENGINE_SHUTDOWN_HARD_SECONDS
        cleanup_errors: list[str] = []
        if server is not None:
            try:
                server.shutdown()
            except OSError as exc:
                cleanup_errors.append(f"IPC shutdown: {type(exc).__name__}: {exc}")
        if server_thread is not None and server_thread.is_alive():
            server_thread.join(timeout=max(0.0, shutdown_deadline - time.monotonic()))
            if server_thread.is_alive():
                cleanup_errors.append("IPC server thread exceeded the shared Engine shutdown deadline")
        if service is not None:
            # Publish one-way shutdown intent to the service *before* draining
            # already-admitted IPC handlers. This blocks maintenance completion
            # from restarting Fast/Deep/relationship work while still allowing
            # the admitted transaction itself to reach a coherent boundary.
            # SQLite owners are closed only after the handler drain below.
            service.request_shutdown()
            runtime_supervisor.request_shutdown()
            remaining = max(0.0, shutdown_deadline - time.monotonic())
            commands_drained = bool(server is None or server.wait_for_service_commands(remaining))
            if not commands_drained:
                cleanup_errors.append("in-flight IPC service commands exceeded the shared Engine shutdown deadline")
            else:
                try:
                    remaining = max(0.0, shutdown_deadline - time.monotonic())
                    if remaining <= 0:
                        raise RuntimeError("Engine shutdown deadline expired before service cleanup could start")
                    service.close(timeout=remaining)
                except (OSError, RuntimeError, ValueError, TypeError) as exc:
                    cleanup_errors.append(f"Engine service: {type(exc).__name__}: {exc}")
        # Even if an orderly service owner reports a cleanup problem, never
        # abandon registered analyzer subprocesses. Escalate through the
        # supervisor before releasing the Engine lifetime lock.
        report = runtime_supervisor.cleanup(timeout=max(0.0, shutdown_deadline - time.monotonic()))
        if report.remaining_processes:
            cleanup_errors.append(
                "owned subprocesses survived cleanup: "
                + ", ".join(str(pid) for pid in report.remaining_processes)
            )
        if report.callback_errors:
            cleanup_errors.extend(f"runtime cleanup: {item}" for item in report.callback_errors)
        if server is not None:
            try:
                server.server_close()
            except OSError as exc:
                cleanup_errors.append(f"IPC close: {type(exc).__name__}: {exc}")
        try:
            endpoint_file.unlink(missing_ok=True)
        except OSError as exc:
            cleanup_errors.append(f"endpoint cleanup: {type(exc).__name__}: {exc}")
        if lock_fd >= 0:
            try:
                os.close(lock_fd)
            except OSError as exc:
                cleanup_errors.append(f"lifetime lock close: {type(exc).__name__}: {exc}")
        try:
            lock_path.unlink(missing_ok=True)
        except OSError as exc:
            cleanup_errors.append(f"lifetime lock cleanup: {type(exc).__name__}: {exc}")
        if cleanup_errors:
            raise RuntimeError("; ".join(cleanup_errors))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="MIDI Library Engine")
    parser.add_argument("--app-root", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return run(args.app_root)
    except Exception as exc:
        # Daemon has no UI. Preserve a startup diagnostic in the shared logs.
        try:
            paths = AppPaths.for_app(args.app_root).ensure()
            atomic_write_json(paths.logs / "library-engine-startup-error.json", {
                "error": f"{type(exc).__name__}: {exc}", "pid": os.getpid(), "time": time.time(),
            })
        except (OSError, RuntimeError, ValueError, TypeError) as diagnostic_exc:
            # stderr remains available when the daemon is launched from a
            # console/service wrapper and is preferable to silently discarding
            # a failure in the diagnostic path itself.
            try:
                print(
                    f"MIDI Library Engine startup failed: {type(exc).__name__}: {exc}; "
                    f"could not write startup diagnostic: {type(diagnostic_exc).__name__}: {diagnostic_exc}",
                    file=__import__("sys").stderr,
                )
            except OSError:
                # Nothing else can safely be persisted if both the shared log
                # path and stderr are unavailable. Returning non-zero still
                # exposes failure to the launcher.
                return 1
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
