"""Detached local workers and a nonblocking durable queue dispatcher."""
from __future__ import annotations

import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import uuid

from .contracts import JobSpec, atomic_json
from .provider import CodexProvider, ProviderConfig
from .queue import QueueStore


class Executor:
    def __init__(self, store: QueueStore, artifact_root, provider_config=None, max_workers=6, lease_seconds=30):
        if max_workers < 1 or lease_seconds <= 0:
            raise ValueError("positive max_workers and lease_seconds required")
        self.store = store
        self.artifact_root = Path(artifact_root).resolve()
        self.artifact_root.mkdir(parents=True, exist_ok=True)
        self.provider_config = ProviderConfig.from_value(provider_config)
        # Validate configuration before any job is claimed.
        CodexProvider(self.provider_config)
        self.max_workers = max_workers
        self.lease_seconds = lease_seconds
        self.owner = "executor-" + uuid.uuid4().hex
        self._children = {}
        self._closed = False

    def recover(self):
        imported, errors = [], []
        for attempt in self.store.attempts():
            if attempt["state"] not in {"running", "uncertain"} or not attempt["artifact_dir"]:
                continue
            completion = Path(attempt["artifact_dir"]) / "completion.json"
            if completion.exists():
                try:
                    result = json.loads(completion.read_text(encoding="utf-8"))
                    imported.append(self.store.complete(attempt["attempt_id"], result))
                except (ValueError, KeyError, OSError) as exc:
                    errors.append({"attempt_id": attempt["attempt_id"], "error": str(exc)})
        expired = self.store.recover_expired()
        return {"recovered_result_ids": imported, "expired_leases": expired, "recovery_errors": errors}

    def tick(self, run_id=None):
        if self._closed:
            raise RuntimeError("executor closed")
        reaped = []
        for attempt_id, process in list(self._children.items()):
            if process.poll() is not None:
                reaped.append({"attempt_id": attempt_id, "worker_exit_code": process.returncode})
                del self._children[attempt_id]
        report = self.recover()
        report.update(started=[], reaped=reaped, dispatch_errors=[])
        for _ in range(self.max_workers):
            claim = self.store.claim(self.owner, self.lease_seconds, run_id, self.max_workers)
            if not claim:
                break
            attempt_id = claim["attempt_id"]
            directory = self.artifact_root / attempt_id
            directory.mkdir(parents=True, exist_ok=False)
            self.store.set_artifact_dir(attempt_id, directory)
            atomic_json(directory / "worker-config.json", {"provider": asdict(self.provider_config), "lease_seconds": self.lease_seconds})
            command = [sys.executable, "-B", "-m", "studio_runtime.worker", "--db", str(self.store.db_path),
                       "--attempt", attempt_id, "--directory", str(directory)]
            atomic_json(directory / "worker-command.json", command)
            try:
                with (directory / "worker-stdout.txt").open("xb") as stdout, (directory / "worker-stderr.txt").open("xb") as stderr:
                    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
                        cwd=Path(__file__).resolve().parents[1], close_fds=True,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
                        start_new_session=os.name != "nt")
                self._children[attempt_id] = process
                report["started"].append({"job_id": claim["job"]["job_id"], "attempt_id": attempt_id, "worker_pid": process.pid})
            except OSError as exc:
                # No model can have started. The lease protocol safely returns this
                # allocation to queued after expiry; preserve the launch failure.
                atomic_json(directory / "worker-launch-error.json", {"error": str(exc), "at": time.time()})
                report["dispatch_errors"].append({"attempt_id": attempt_id, "error": str(exc)})
        return report

    def pause(self, run_id):
        self.store.pause(run_id)

    def resume(self, run_id):
        self.store.resume(run_id)

    def close(self):
        # Workers are deliberately independent: stopping a supervisor must not
        # discard their raw output or imply cancellation of a remote request.
        self._closed = True
        for process in self._children.values():
            if process.poll() is None:
                # Retain and eventually reap the Popen handle without blocking
                # the caller or terminating an independent provider request.
                threading.Thread(target=process.wait, name="detached-worker-reaper", daemon=True).start()
        self._children.clear()


def run_worker(db_path, attempt_id, directory):
    store = QueueStore(db_path)
    directory = Path(directory).resolve()
    config = json.loads((directory / "worker-config.json").read_text(encoding="utf-8"))
    lease_seconds = config["lease_seconds"]
    if not store.begin_attempt(attempt_id, os.getpid(), lease_seconds):
        return 3
    attempt = next(item for item in store.attempts() if item["attempt_id"] == attempt_id)
    if Path(attempt["artifact_dir"]).resolve() != directory:
        raise ValueError("worker directory does not match registered attempt")
    stopped = threading.Event()

    def heartbeat():
        while not stopped.wait(max(0.05, min(5, lease_seconds / 3))):
            try:
                if not store.heartbeat(attempt_id, lease_seconds):
                    break
            except Exception as exc:
                # Do not fabricate completion; expiry will expose uncertain state.
                print(f"heartbeat failed: {exc}", file=sys.stderr, flush=True)

    thread = threading.Thread(target=heartbeat, name="attempt-heartbeat", daemon=True)
    thread.start()
    try:
        result = CodexProvider(config["provider"]).execute(store.get_job(attempt["job_id"]), attempt_id, directory)
        store.complete(attempt_id, result)
        return 0
    finally:
        stopped.set()
        thread.join(timeout=1)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Internal durable Studio worker")
    parser.add_argument("--db", required=True)
    parser.add_argument("--attempt", required=True)
    parser.add_argument("--directory", required=True)
    args = parser.parse_args(argv)
    return run_worker(args.db, args.attempt, args.directory)


if __name__ == "__main__":
    raise SystemExit(main())
