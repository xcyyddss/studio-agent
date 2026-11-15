"""Verified snapshots, default read-only restore and strict same-host forks.

This module never resumes a provider session, dispatches work, copies host
credentials, overwrites a run, or deletes a directory. The service must refuse
mutating operations when app.json has checkpoint_read_only=true.
"""
from __future__ import annotations
from .state_store import read_state, write_state

from contextlib import closing, contextmanager, nullcontext
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import sqlite3
import subprocess
import uuid

from .contracts import atomic_json, canonical, digest

FORMAT = "studio.checkpoint.v1"
DATABASES = ("workflow.db", "queue.sqlite")


class CheckpointError(ValueError):
    pass


def _now():
    return datetime.now(timezone.utc).isoformat()


def _hash(path):
    checksum = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            checksum.update(block)
    return checksum.hexdigest()


def _safe(root, relative):
    if not isinstance(relative, str) or "\\" in relative or ":" in relative or any(ord(c) < 32 for c in relative):
        raise CheckpointError("snapshot paths must be relative POSIX paths")
    parts = PurePosixPath(relative)
    if parts.is_absolute() or not parts.parts or relative != parts.as_posix() or any(p in {".", ".."} for p in parts.parts):
        raise CheckpointError("snapshot path escapes its directory")
    if any(p.endswith((".", " ")) or re.fullmatch(r"(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", p) for p in parts.parts):
        raise CheckpointError("snapshot path uses a reserved filesystem name")
    root = _no_links(root)
    path = root.joinpath(*parts.parts)
    probe = root
    for part in parts.parts:
        probe = probe / part
        if probe.is_symlink() or (probe.exists() and getattr(probe.stat(follow_symlinks=False), "st_file_attributes", 0) & 0x400):
            raise CheckpointError("links and junctions are not accepted in snapshots")
    if not path.resolve().is_relative_to(root):
        raise CheckpointError("snapshot path escapes its directory")
    if path.is_file() and path.stat().st_nlink > 1:
        raise CheckpointError("hard-linked files are not accepted in checkpoints")
    return path


def _no_links(path):
    path = Path(path).absolute()
    for probe in (path, *path.parents):
        if probe.is_symlink() or (probe.exists() and getattr(probe.stat(follow_symlinks=False), "st_file_attributes", 0) & 0x400):
            raise CheckpointError("links and junctions are not accepted in checkpoints")
    return path.resolve()


def _host_binding():
    # A compatibility guard, not remote attestation or a security identity.
    return {"format": "studio.local-host.v1", "fingerprint": digest({"node": platform.node(), "os": os.name, "machine": platform.machine()})}


def _read_db(path, *, immutable=False):
    path = Path(path).resolve()
    if not path.is_file():
        raise CheckpointError(f"database is missing: {path.name}")
    con = sqlite3.connect(path.as_uri() + "?mode=ro" + ("&immutable=1" if immutable else ""), uri=True, timeout=30)
    con.row_factory = sqlite3.Row
    return con


@contextmanager
def _source_lock(path):
    """Lock the existing supervisor file without creating/modifying source files."""
    lock = path / "supervisor.lock"
    if not lock.is_file():
        raise CheckpointError("supervisor.lock is absent; export through the service under run_lock with lock_held=True")
    with lock.open("r+b") as handle:
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


def _backup(source, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(target)) as destination:
        source.backup(destination)


def _copy_file(source, target):
    """Capture exactly the initial file length; growing attempt logs are partial."""
    target.parent.mkdir(parents=True, exist_ok=True)
    with source.open("rb") as incoming, target.open("xb") as output:
        stat = os.fstat(incoming.fileno())
        remaining = stat.st_size
        while remaining:
            block = incoming.read(min(1024 * 1024, remaining))
            if not block:
                raise CheckpointError(f"source file was truncated during export: {source.name}")
            output.write(block)
            remaining -= len(block)
        output.flush()
        os.fsync(output.fileno())
    return {"source_size_at_open": stat.st_size, "source_mtime_ns": stat.st_mtime_ns}


def _code_version():
    project = Path(__file__).resolve().parents[1]
    files = {}
    for directory in ("studio_runtime",):
        for path in sorted((project / directory).glob("*.py")):
            files[path.relative_to(project).as_posix()] = _hash(path)
    try:
        result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=project, capture_output=True,
            text=True, timeout=10, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        commit = result.stdout.strip() if result.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        commit = None
    return {"git_commit": commit, "source_sha256": files,
            "boundary": "Source hashes identify working-tree code, including uncommitted edits; source code is not installed by restore."}


def _database_summary(payload):
    with closing(_read_db(payload / "workflow.db", immutable=True)) as workflow, closing(_read_db(payload / "queue.sqlite", immutable=True)) as queue:
        for name, con in (("workflow.db", workflow), ("queue.sqlite", queue)):
            check = [row[0] for row in con.execute("PRAGMA integrity_check")]
            if check != ["ok"] or list(con.execute("PRAGMA foreign_key_check")):
                raise CheckpointError(f"database integrity failed: {name}")
        state = read_state(workflow)
        from .file_exchange import validate_storage
        validate_storage(workflow, state)
        app = json.loads((payload / "app.json").read_text(encoding="utf-8"))
        if app.get("id") != state["run_id"]:
            raise CheckpointError("app and workflow run identities differ")
        jobs = [dict(row) for row in queue.execute("SELECT * FROM jobs")]
        attempts = [dict(row) for row in queue.execute("SELECT * FROM attempts")]
        by_attempt = {attempt["attempt_id"]: attempt for attempt in attempts}
        for job in jobs:
            spec = json.loads(job["spec"])
            if digest(spec) != job["spec_sha256"] or spec["job_id"] != job["job_id"] or spec["run_id"] != job["run_id"]:
                raise CheckpointError("queued job identity or input digest mismatch")
            if job["run_id"] != state["run_id"] or job["job_id"] not in state["jobs"]:
                raise CheckpointError("queue job has no matching workflow intent")
            if spec != state["jobs"][job["job_id"]]["spec"]:
                raise CheckpointError("workflow and queue immutable job inputs differ")
            if job["current_attempt"] and (job["current_attempt"] not in by_attempt or by_attempt[job["current_attempt"]]["job_id"] != job["job_id"]):
                raise CheckpointError("current attempt refers to another or missing job")
        for result in queue.execute("SELECT * FROM results"):
            body = json.loads(result["body"])
            if result["attempt_id"] not in by_attempt or by_attempt[result["attempt_id"]]["job_id"] != result["job_id"]:
                raise CheckpointError("result has no matching execution attempt")
            if body["result_id"] != result["result_id"] or body["result_sha256"] != result["result_sha256"]:
                raise CheckpointError("result identity or digest columns disagree")
        artifacts = []
        for row in workflow.execute("SELECT path,content FROM artifacts"):
            path = _safe(payload, row["path"])
            if not row["path"].startswith("artifacts/") or not path.is_file() or path.read_text(encoding="utf-8") != row["content"]:
                raise CheckpointError("published artifact differs from workflow database")
            artifacts.append(row["path"])
        return {"source_run_id": state["run_id"], "workflow_state": state["state"], "workflow_paused": state["paused"],
                "job_count": len(jobs), "attempt_count": len(attempts), "artifact_paths": artifacts,
                "inflight": [{"attempt_id": a["attempt_id"], "job_id": a["job_id"], "source_state": a["state"]}
                             for a in attempts if a["state"] in {"leased", "running", "uncertain"}],
                "pending_result_ids": [r[0] for r in queue.execute("SELECT result_id FROM results WHERE acked_at IS NULL")]}


def export_snapshot(run_dir, snapshot_dir, *, lock_held=False):
    """Export under the service supervisor lock; never pause or mutate source.

    Workers may still write their own queue/results. Queue backup is a pinned
    read view; in-flight files are captured as explicitly partial evidence.
    Callers already holding run_lock must set lock_held=True.
    """
    source, destination = _no_links(run_dir), _no_links(snapshot_dir)
    if destination.exists() or destination.is_relative_to(source) or source.is_relative_to(destination):
        raise CheckpointError("snapshot destination must be new and outside the source run")
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = destination.with_name(".checkpoint-export-" + uuid.uuid4().hex)
    staging.mkdir()
    payload = staging / "payload"
    payload.mkdir()
    manifest = {"format": FORMAT, "snapshot_id": "snapshot-" + uuid.uuid4().hex, "created_at": _now(),
                "source_run_path": str(source), "files": {}, "skipped_transient_files": [],
                "restoration_mode": "isolated_inspection_only", "host_credentials_included": False,
                "host_binding": _host_binding(),
                "boundary": "Database snapshots are consistent under the supervisor lock. In-flight raw files are partial captures taken after the queue read view, not a remote execution checkpoint. No live provider process/session is restored."}
    try:
        with nullcontext() if lock_held else _source_lock(source):
            for name in ("app.json", *DATABASES):
                _safe(source, name)
            with closing(_read_db(source / "workflow.db")) as workflow, closing(_read_db(source / "queue.sqlite")) as queue:
                workflow.execute("BEGIN")
                workflow.execute("SELECT count(*) FROM workflow").fetchone()
                queue.execute("BEGIN")
                queue.execute("SELECT count(*) FROM jobs").fetchone()
                _backup(workflow, payload / "workflow.db")
                _backup(queue, payload / "queue.sqlite")
                inflight = {row[0] for row in queue.execute("SELECT attempt_id FROM attempts WHERE state IN ('leased','running','uncertain')")}
                app = json.loads((source / "app.json").read_text(encoding="utf-8"))
                if app.get("checkpoint_read_only"):
                    raise CheckpointError("export the original retained snapshot for an inspection-only restore")
                allowed_config = {"executable", "model", "reasoning_effort", "timeout_seconds", "command_prefix", "evidence_kind", "web_search"}
                if set(app.get("provider_config", {})) - allowed_config:
                    raise CheckpointError("unrecognized provider configuration; refuse to export possible credentials")
                if app.get("provider_config", {}).get("web_search", "live") not in {"disabled", "cached", "live"}:
                    raise CheckpointError("invalid search mode in provider configuration")
                _copy_file(source / "app.json", payload / "app.json")
                for directory in ("attempts", "artifacts", "retry-dispositions", "tool-executions", "sandbox-checks"):
                    root = _safe(source, directory)
                    if not root.exists():
                        continue
                    for path in sorted(root.rglob("*")):
                        relative = path.relative_to(source).as_posix()
                        path = _safe(source, relative)
                        if directory == "attempts" and any(part.lower() in {".codex", ".aws", ".ssh", "auth.json", "credentials"} for part in PurePosixPath(relative).parts):
                            raise CheckpointError("credential locations are not permitted in attempt archives")
                        if not path.is_file():
                            continue
                        if directory == "attempts" and path.name.endswith(".tmp"):
                            manifest["skipped_transient_files"].append(relative)
                            continue
                        details = _copy_file(path, _safe(payload, relative))
                        parts = PurePosixPath(relative).parts
                        details["partial_inflight_evidence"] = directory == "attempts" and len(parts) > 1 and parts[1] in inflight
                        manifest["files"]["payload/" + relative] = details
            manifest["database_summary"] = _database_summary(payload)
        manifest["source_run_id"] = manifest["database_summary"]["source_run_id"]
        manifest["code_version"] = _code_version()
        for path in sorted(payload.rglob("*")):
            if path.is_file():
                relative = path.relative_to(staging).as_posix()
                entry = manifest["files"].setdefault(relative, {})
                entry.update(sha256=_hash(path), bytes=path.stat().st_size)
        atomic_json(staging / "manifest.json", manifest)
        (staging / "manifest.sha256").write_text(_hash(staging / "manifest.json") + "\n", encoding="ascii")
        verify_snapshot(staging)
        staging.rename(destination)
        return dict(manifest, snapshot_path=str(destination))
    except Exception as exc:
        atomic_json(staging / "export-error.json", {"error": str(exc), "created_at": _now(), "published": False})
        raise


def verify_snapshot(snapshot_dir):
    root = _no_links(snapshot_dir)
    manifest_path = _safe(root, "manifest.json")
    if not manifest_path.is_file() or not _safe(root, "manifest.sha256").is_file():
        raise CheckpointError("snapshot manifest or checksum is missing")
    expected = (root / "manifest.sha256").read_text(encoding="ascii").strip()
    if _hash(manifest_path) != expected:
        raise CheckpointError("snapshot manifest checksum mismatch")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("format") != FORMAT or not isinstance(manifest.get("files"), dict):
        raise CheckpointError("unsupported or invalid snapshot manifest")
    expected_paths = set(manifest["files"])
    for required in ("payload/app.json", "payload/workflow.db", "payload/queue.sqlite"):
        if required not in expected_paths:
            raise CheckpointError("snapshot is missing required state")
    actual_paths = set()
    for path in root.rglob("*"):
        relative = path.relative_to(root).as_posix()
        path = _safe(root, relative)
        if path.is_file() and relative not in {"manifest.json", "manifest.sha256"}:
            actual_paths.add(relative)
    if actual_paths != expected_paths:
        raise CheckpointError("snapshot file set differs from manifest")
    for relative, entry in manifest["files"].items():
        if not relative.startswith("payload/"):
            raise CheckpointError("snapshot payload path is outside payload")
        path = _safe(root, relative)
        if not path.is_file() or path.stat().st_size != entry["bytes"] or _hash(path) != entry["sha256"]:
            raise CheckpointError(f"snapshot file hash mismatch: {relative}")
    summary = _database_summary(root / "payload")
    if summary != manifest["database_summary"] or summary["source_run_id"] != manifest["source_run_id"]:
        raise CheckpointError("database summary differs from manifest")
    return {"valid": True, "snapshot_id": manifest["snapshot_id"], "source_run_id": manifest["source_run_id"],
            "files_verified": len(expected_paths), "database_summary": summary, "restoration_mode": "isolated_inspection_only"}


def restore_snapshot(snapshot_dir, target_dir):
    """Restore into a new directory; original snapshot remains independently valid.

    The run identity inside immutable jobs is preserved. The target's directory
    name is its inspection identity; it is never a dispatchable fork.
    """
    source, target = _no_links(snapshot_dir), _no_links(target_dir)
    verified = verify_snapshot(source)
    if target.exists() or target.is_relative_to(source) or source.is_relative_to(target):
        raise CheckpointError("restore target must not exist or overlap the snapshot")
    manifest = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
    original_path = Path(manifest["source_run_path"]).resolve()
    if original_path.exists() and (target == original_path or target.is_relative_to(original_path) or original_path.is_relative_to(target)):
        raise CheckpointError("restore target overlaps the existing source run")
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = target.with_name(".checkpoint-restore-" + uuid.uuid4().hex)
    staging.mkdir()
    try:
        archive = staging / "checkpoint-source"
        shutil.copytree(source, archive)
        verify_snapshot(archive)
        for name in DATABASES:
            with closing(_read_db(archive / "payload" / name, immutable=True)) as con:
                _backup(con, staging / name)
        artifacts = archive / "payload" / "artifacts"
        if artifacts.exists():
            shutil.copytree(artifacts, staging / "artifacts")
        original_app = json.loads((archive / "payload" / "app.json").read_text(encoding="utf-8"))
        restored_at = _now()
        app = dict(original_app, id=target.name, source_run_id=manifest["source_run_id"], checkpoint_read_only=True,
                   provider_config={}, restored_at=restored_at,
                   checkpoint={"snapshot_id": manifest["snapshot_id"], "manifest_sha256": _hash(source / "manifest.json"),
                               "original_run_path": manifest["source_run_path"], "original_run_id": manifest["source_run_id"],
                               "archive": "checkpoint-source", "mode": "isolated_inspection_only"})
        atomic_json(staging / "app.json", app)
        with closing(sqlite3.connect(staging / "queue.sqlite")) as con, con:
            con.execute("UPDATE attempts SET worker_pid=NULL,owner='checkpoint:no-live-owner',lease_until=0,artifact_dir=NULL")
            con.execute("UPDATE attempts SET state='uncertain',note='Restored snapshot; original external execution is not attached and may have progressed after capture' WHERE state IN ('leased','running')")
            con.execute("UPDATE jobs SET state='uncertain' WHERE state IN ('leased','running')")
            for run_id, in con.execute("SELECT DISTINCT run_id FROM jobs").fetchall() + [(manifest["source_run_id"],)]:
                con.execute("INSERT INTO controls(run_id,paused) VALUES(?,1) ON CONFLICT(run_id) DO UPDATE SET paused=1", (run_id,))
        with closing(sqlite3.connect(staging / "workflow.db")) as con, con:
            state = read_state(con)
            state["paused"] = True
            state["checkpoint_read_only"] = True
            state["checkpoint_provenance"] = app["checkpoint"]
            state["state_reason"] = "Inspection-only checkpoint: original execution and remote sessions are not resumed."
            write_state(con, state)
        restored_files = {path.relative_to(staging).as_posix(): {"sha256": _hash(path), "bytes": path.stat().st_size}
                          for path in staging.rglob("*") if path.is_file() and not path.is_relative_to(archive)}
        atomic_json(staging / "restoration.json", {"format": "studio.restoration.v1", "restored_at": restored_at,
            "snapshot_id": verified["snapshot_id"], "source_run_id": manifest["source_run_id"], "target_path": str(target),
            "mode": "isolated_inspection_only", "active_processes_restored": False, "automatic_dispatch": False,
            "inflight_requiring_disposition": manifest["database_summary"]["inflight"],
            "restored_files": restored_files,
            "historical_reference_boundary": "Original absolute paths, PIDs and immutable receipts remain only as historical evidence in checkpoint-source and original result bodies. They are never used to locate live workers or auto-recover a provider. inspect_restored returns local archive paths."})
        # No stale supervisor heartbeat, lock, PID, attempt command or executable
        # provider configuration is published as a live control record.
        staging.rename(target)
        return inspect_restored(target)
    except Exception as exc:
        atomic_json(staging / "restore-error.json", {"error": str(exc), "created_at": _now(), "published": False})
        raise


def inspect_restored(run_dir):
    """Read archived status without constructing a Workflow, QueueStore or worker."""
    root = Path(run_dir).resolve()
    app = json.loads((root / "app.json").read_text(encoding="utf-8"))
    if not app.get("checkpoint_read_only"):
        raise CheckpointError("this is not an inspection-only restored checkpoint")
    archive = root / "checkpoint-source"
    verified = verify_snapshot(archive)
    restoration = json.loads((root / "restoration.json").read_text(encoding="utf-8"))
    for relative, entry in restoration["restored_files"].items():
        path = _safe(root, relative)
        if not path.is_file() or path.stat().st_size != entry["bytes"] or _hash(path) != entry["sha256"]:
            raise CheckpointError("restored inspection copy has changed; recover again from its verified archive")
    with closing(_read_db(root / "workflow.db", immutable=True)) as con:
        state = read_state(con)
    with closing(_read_db(root / "queue.sqlite", immutable=True)) as con:
        attempts = [dict(row) for row in con.execute("SELECT attempt_id,job_id,state,worker_pid,owner,lease_until,artifact_dir FROM attempts")]
        jobs = [dict(row) for row in con.execute("SELECT job_id,run_id,state,current_attempt FROM jobs")]
        pending = [{"result_id": row["result_id"], "job_id": row["job_id"], "attempt_id": row["attempt_id"],
                    "original_result_sha256": row["result_sha256"], "outcome": json.loads(row["body"])["outcome"],
                    "receipt_path": str(archive / "payload" / "attempts" / row["attempt_id"] / "receipt.json")}
                   for row in con.execute("SELECT * FROM results WHERE acked_at IS NULL")]
    return {"id": app["id"], "source_run_id": app["source_run_id"], "checkpoint_read_only": True,
            "state": "inspection_only", "paused": True, "automatic_dispatch": False,
            "workflow": {"goal": state["goal"], "source_state": state["state"], "topics": list(state["topics"].values()),
                         "questions": list(state["questions"].values()), "deliveries": state["deliveries"],
                         "unresolved_obligations": state["obligations"]},
            "jobs": jobs, "attempts": attempts, "pending_results": pending,
            "artifacts": [{"path": p, "local_path": str(_safe(root, p))} for p in verified["database_summary"]["artifact_paths"]],
            "restoration": restoration, "archive_verification": verified}


def _tool_quiescence(payload):
    """Incomplete local claims are unknown even though built-ins are read-only."""
    root = _safe(payload, "tool-executions")
    if not root.exists():
        return []
    completed = []
    for directory in sorted(root.iterdir()):
        relative = directory.relative_to(payload).as_posix()
        directory = _safe(payload, relative)
        if not directory.is_dir() or not re.fullmatch(r"[0-9a-f]{64}", directory.name):
            raise CheckpointError("unrecognized tool execution ledger entry")
        paths = [_safe(payload, relative + "/" + name) for name in ("request.json", "result.json", "receipt.json")]
        if not all(path.is_file() for path in paths):
            raise CheckpointError("local tool execution is incomplete; only read-only restore is supported")
        request, result, receipt = [json.loads(path.read_text(encoding="utf-8")) for path in paths]
        checksum = digest(request["request"])
        if request.get("request_sha256") != checksum or result.get("request_sha256") != checksum or receipt.get("request_sha256") != checksum:
            raise CheckpointError("tool execution request digest mismatch")
        if directory.name != hashlib.sha256(request["request"]["id"].encode("utf-8")).hexdigest():
            raise CheckpointError("tool execution request identity mismatch")
        if receipt.get("phase") != "completed" or result.get("outcome") not in {"succeeded", "failed"}:
            raise CheckpointError("local tool execution is uncertain; only read-only restore is supported")
        if receipt.get("outcome") != result["outcome"] or receipt.get("result_sha256") != digest(result):
            raise CheckpointError("tool execution result/receipt mismatch")
        completed.append(request["request"]["id"])
    return completed


def _fork_eligibility(payload):
    """A terminal label alone cannot authorize a runnable fork."""
    summary = _database_summary(payload)
    with closing(_read_db(payload / "workflow.db", immutable=True)) as con:
        state = read_state(con)
        if state.get("schema") not in {"studio_workflow.v3", "studio_workflow.v4"}:
            raise CheckpointError("workflow schema has no supported runnable migration")
        if state.get("checkpoint_read_only") or state.get("state") not in {"delivered", "quiescent"}:
            raise CheckpointError("runnable fork requires delivered/quiescent workflow; use read-only restore")
        if any(intent.get("state") == "pending" for intent in state.get("schedule_intents", [])):
            raise CheckpointError("pending scheduling responsibility prevents runnable fork")
        if any(item.get("kind") in {"input_budget_blocked", "input_contract_blocked"} and item.get("status") == "blocked"
               for item in state.get("diagnostics", [])):
            raise CheckpointError("unresolved input scheduling block prevents runnable fork")
        if any(job.get("state") not in {"applied", "superseded"} for job in state["jobs"].values()):
            raise CheckpointError("unfinished semantic jobs prevent runnable fork")
        for job in state["jobs"].values():
            if job["spec"]["run_id"] != state["run_id"]:
                raise CheckpointError("workflow job has a foreign run identity")
        for topic in state["topics"].values():
            if topic.get("dirty") or topic.get("new_returns") or topic.get("state") not in {"delivered", "quiescent", "returned", "sleeping", "dormant"}:
                raise CheckpointError("unsettled topic duties prevent runnable fork")
        if any(i.get("state") not in {"closed", "superseded"} for i in state.get("inquiries", {}).values()):
            raise CheckpointError("unfinished inquiry prevents runnable fork")
        if any(q.get("state") == "proposed" or q.get("state") in {"open","linked"} and q.get("blocking")
               for q in state.get("questions", {}).values()):
            raise CheckpointError("blocking questions prevent runnable fork")
        if any(h["state"] not in {"delivered","cancelled"} for h in state.get("question_handoffs", [])):
            raise CheckpointError("pending scoped question handoffs prevent runnable fork")
        if any(t.get("state") == "requested" for t in state.get("tool_requests", [])):
            raise CheckpointError("unresolved tool request prevents runnable fork")
        if any(e.get("state") == "pending" for e in state.get("escalations", [])):
            raise CheckpointError("pending escalation prevents runnable fork")
        if any(e.get("state") == "pending" for e in state.get("late_results", [])) or any(e.get("state") == "pending" for e in state.get("reassessments", {}).values()):
            raise CheckpointError("pending result disposition or reassessment prevents runnable fork")
        if any(o.get("kind") == "selection" and o.get("state") == "open" for o in state.get("obligations", [])):
            raise CheckpointError("open selection duty prevents runnable fork")
        user = state.get("user_collaboration")
        if user:
            # Use the runtime's actual event responsibility rule, not a second
            # approximation of how USER pending work is represented.
            from .user_collaboration import pending_events
            if pending_events(state):
                raise CheckpointError("pending USER collaboration events prevent runnable fork")
        objects = [dict(row) for row in con.execute("SELECT id,kind,body FROM objects")]
        # Objects presently do not need an operational run ID. If an extension
        # adds a top-level one, migrate it explicitly; never rewrite source text.
        object_ids = []
        for row in objects:
            body = json.loads(row["body"])
            if "run_id" in body and row["kind"] != "fork_lineage":
                if body["run_id"] != state["run_id"]:
                    raise CheckpointError("object has an unsupported foreign operational run identity")
                object_ids.append(row["id"])
    with closing(_read_db(payload / "queue.sqlite", immutable=True)) as con:
        if any(row[0] not in {"acked", "cancelled"} for row in con.execute("SELECT state FROM jobs")):
            raise CheckpointError("queued, running, uncertain or unacknowledged jobs prevent runnable fork")
        if any(row[0] not in {"succeeded", "failed", "not_started"} for row in con.execute("SELECT state FROM attempts")):
            raise CheckpointError("historical uncertain or live attempts prevent runnable fork; use read-only restore")
        if con.execute("SELECT 1 FROM results WHERE acked_at IS NULL LIMIT 1").fetchone():
            raise CheckpointError("pending result acknowledgment prevents runnable fork")
        if any(row[0] != state["run_id"] for row in con.execute("SELECT run_id FROM controls")):
            raise CheckpointError("queue controls contain an unsupported foreign run identity")
    return {"source_run_id": summary["source_run_id"], "source_state": state["state"],
            "jobs_rebased": len(state["jobs"]), "object_ids_rebased": object_ids,
            "completed_local_tools": _tool_quiescence(payload), "inflight": [], "pending_results": []}


def fork_snapshot(snapshot_dir, target_dir, *, run_id=None):
    """Create a runnable same-host branch without starting or reissuing any work.

    Strictly terminal semantic state and entirely settled execution history are
    required. Even a historical uncertain attempt rejects this operation. The
    default restore_snapshot remains an inspection-only path for every snapshot.
    Original raw/results/events remain historical evidence in their source run;
    active state/specs/controls are rebased, with original hashes in fork.json.
    """
    source, target = _no_links(snapshot_dir), _no_links(target_dir)
    verified = verify_snapshot(source)
    manifest = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("host_binding") != _host_binding():
        raise CheckpointError("snapshot has no matching same-host binding; use read-only restore or export again on its original host")
    run_id = run_id or target.name
    if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,70}", run_id) or run_id != target.name or run_id == manifest["source_run_id"]:
        raise CheckpointError("fork requires a new run_id matching its target directory name")
    if target.exists() or target.is_relative_to(source) or source.is_relative_to(target):
        raise CheckpointError("fork target must be new and outside the source snapshot")
    original = Path(manifest["source_run_path"]).resolve()
    if target == original or target.is_relative_to(original) or original.is_relative_to(target):
        raise CheckpointError("fork target must not overlap the original run path")
    eligibility = _fork_eligibility(source / "payload")
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = target.with_name(".checkpoint-fork-" + uuid.uuid4().hex)
    staging.mkdir()
    try:
        archive = staging / "checkpoint-source"
        shutil.copytree(source, archive)
        verify_snapshot(archive)
        # Validate the copied input too, then operate only on this local copy.
        _fork_eligibility(archive / "payload")
        for name in DATABASES:
            with closing(_read_db(archive / "payload" / name, immutable=True)) as con:
                _backup(con, staging / name)
        for name in ("artifacts", "tool-executions", "sandbox-checks"):
            copied = archive / "payload" / name
            if copied.exists():
                shutil.copytree(copied, staging / name)
        created_at = _now()
        lineage = {"snapshot_id": verified["snapshot_id"], "manifest_sha256": _hash(source / "manifest.json"),
                   "original_run_path": manifest["source_run_path"], "original_run_id": manifest["source_run_id"],
                   "run_id": run_id, "archive": "checkpoint-source", "mode": "same_host_quiescent_fork", "created_at": created_at}
        original_app = json.loads((archive / "payload" / "app.json").read_text(encoding="utf-8"))
        app = dict(original_app, id=run_id, source_run_id=manifest["source_run_id"], checkpoint_read_only=False,
                   created_at=created_at, checkpoint=lineage)
        atomic_json(staging / "app.json", app)
        hashes = {}
        with closing(sqlite3.connect(staging / "workflow.db")) as con, con:
            state = read_state(con)
            for jid, job in state["jobs"].items():
                hashes[jid] = {"source_spec_sha256": digest(job["spec"])}
                job["spec"]["run_id"] = run_id
                hashes[jid]["fork_spec_sha256"] = digest(job["spec"])
            state.update(run_id=run_id, paused=False, checkpoint_read_only=False, checkpoint_provenance=lineage)
            write_state(con, state)
            for oid in eligibility["object_ids_rebased"]:
                body = json.loads(con.execute("SELECT body FROM objects WHERE id=?", (oid,)).fetchone()[0])
                body["run_id"] = run_id
                con.execute("UPDATE objects SET body=? WHERE id=?", (canonical(body), oid))
            con.execute("INSERT INTO objects(id,kind,body) VALUES(?,?,?)", ("FORK." + uuid.uuid4().hex, "fork_lineage", canonical(lineage)))
        with closing(sqlite3.connect(staging / "queue.sqlite")) as con, con:
            for jid, spec_text in con.execute("SELECT job_id,spec FROM jobs").fetchall():
                spec = json.loads(spec_text)
                spec["run_id"] = run_id
                con.execute("UPDATE jobs SET run_id=?,spec=?,spec_sha256=? WHERE job_id=?", (run_id, canonical(spec), digest(spec), jid))
            con.execute("UPDATE attempts SET worker_pid=NULL,owner='checkpoint-fork:no-live-owner',lease_until=0,artifact_dir=NULL")
            con.execute("DELETE FROM controls")
            con.execute("INSERT INTO controls VALUES(?,0)", (run_id,))
        fork_summary = _database_summary(staging)
        if fork_summary["source_run_id"] != run_id or fork_summary["inflight"] or fork_summary["pending_result_ids"]:
            raise CheckpointError("fork identity or settlement validation failed")
        atomic_json(staging / "fork.json", {"format": "studio.fork.v1", **lineage, "host_binding": _host_binding(),
                    "eligibility": eligibility, "job_identity_mapping": hashes, "active_processes_restored": False,
                    "automatic_dispatch": False, "historical_attempts_reissued": False,
                    "history_boundary": "Raw attempts, result bodies, receipts, object sources and event history retain original identities. They are historical evidence, not new execution. Active job specs and queue controls use the new run_id. Original bytes remain verifiable in checkpoint-source.",
                    "published_files": {p.relative_to(staging).as_posix(): {"sha256": _hash(p), "bytes": p.stat().st_size}
                                        for p in staging.rglob("*") if p.is_file() and not p.is_relative_to(archive)}})
        staging.rename(target)
        return {"id": run_id, "source_run_id": manifest["source_run_id"], "checkpoint_read_only": False,
                "state": eligibility["source_state"], "paused": False, "mode": "same_host_quiescent_fork",
                "automatic_dispatch": False, "active_processes_restored": False,
                "path": str(target), "fork_manifest": str(target / "fork.json"), "checkpoint": lineage}
    except Exception as exc:
        atomic_json(staging / "fork-error.json", {"error": str(exc), "created_at": _now(), "published": False})
        raise
