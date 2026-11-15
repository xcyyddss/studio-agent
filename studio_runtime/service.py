"""Single-user application service; semantic work stays in Workflow roles.

OS file locks serialize supervisors. Workers own their durable attempts and can
outlive the UI/CLI. A controller never fabricates successful model results.
"""
from __future__ import annotations
from .state_store import read_state, write_state
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import threading
import time
import uuid

from .contracts import atomic_json

PACKAGE = Path(__file__).resolve().parent
DATA_ROOT = Path(os.environ.get("STUDIO_DATA_DIR", Path.cwd() / "studio-data"))


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def selected_model():
    from .provider import MODEL
    return MODEL


@contextmanager
def run_lock(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


DEFAULT_WORKERS = 6


def validate_workers(value):
    if type(value) is not int or not 1 <= value <= 32:
        raise ValueError("并发调用上限须为 1–32 的整数")
    return value


class StudioService:
    def __init__(self, root=DATA_ROOT, *, max_workers=DEFAULT_WORKERS):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.max_workers = validate_workers(max_workers)
        self.threads = {}
        self.thread_lock = threading.RLock()
        self.shutdown_event = threading.Event()
        self.errors = {}

    def path(self, run_id, *, exists=True):
        if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,70}", run_id):
            raise ValueError("Invalid run ID")
        path = (self.root / run_id).resolve()
        if path.parent != self.root:
            raise ValueError("Run path escapes storage")
        if exists and not (path / "app.json").is_file():
            raise ValueError("Run does not exist")
        return path

    def list_runs(self):
        result = []
        for path in self.root.glob("*/app.json"):
            if not path.resolve().is_relative_to(self.root):
                continue
            try:
                item = json.loads(path.read_text(encoding="utf-8"))
                if not path.parent.name.startswith("."):
                    result.append(item)
            except (OSError, ValueError):
                continue
        return sorted(result, key=lambda x: x["created_at"], reverse=True)

    def _require_writable_run(self, path):
        manifest = json.loads((path / "app.json").read_text(encoding="utf-8"))
        if manifest.get("checkpoint_read_only"):
            raise ValueError("这是检查点的只读恢复副本，不能派发模型或修改运行状态。")

    def create(self, goal, *, run_id=None, model=None, provider_config=None, profile_seeds=None, sandbox_tests=False, web_search=None, context_policy=None, council_limits=None, max_workers=None):
        workers = validate_workers(self.max_workers if max_workers is None else max_workers)
        from .workflow import Workflow
        from .web_research import MODES
        mode = web_search if web_search is not None else (provider_config or {}).get("web_search", "live")
        if mode not in MODES:
            raise ValueError("web_search must be disabled, cached or live")
        if not isinstance(goal, str) or not goal.strip() or len(goal) > 12000:
            raise ValueError("目标须为 1–12000 字符")
        run_id = run_id or "run-" + uuid.uuid4().hex[:12]
        path = self.path(run_id, exists=False)
        if path.exists():
            raise ValueError("Run already exists; use resume")
        staging = (self.root / (".initializing-" + uuid.uuid4().hex)).resolve()
        if staging.parent != self.root or path.parent != self.root:
            raise ValueError("Initialization paths escaped data root")
        staging.mkdir()
        config = dict(provider_config or {})
        config["web_search"] = mode
        chosen = model or selected_model()
        if chosen:
            config["model"] = chosen
        from .provider import CodexProvider, REASONING_EFFORT
        config["reasoning_effort"] = REASONING_EFFORT
        CodexProvider(config)
        from .context_lifecycle import policy
        policy({"metadata":{"context_policy":context_policy or {}}})
        manifest = {"id": run_id, "goal": goal, "created_at": utcnow(),
                    "version": 4, "provider_config": config, "tool_policy": "local_artifacts_and_native_web",
                    "sandbox_tests": bool(sandbox_tests), "scheduling": {"max_workers": workers}}
        # Publish the app entry only after its workflow is initialized.
        try:
            Workflow.create(staging, goal, run_id=run_id, seed_materials=[], profile_seeds=profile_seeds, council_limits=council_limits,
                            metadata={"sandbox_tests": bool(sandbox_tests), "web_search": mode, "context_policy": context_policy or {}})
            atomic_json(staging / "app.json", manifest)
            # Both resolved directories are checked above and are siblings under
            # this run root. Atomic publication never overwrites an existing run.
            staging.rename(path)
        except Exception as exc:
            atomic_json(staging / "initialization-error.json", {"run_id": run_id,
                        "error": str(exc), "created_at": utcnow(), "published": False})
            raise ValueError("初始化未发布，原输入未截断。可缩小当前目标或拆分材料后重试。" + str(exc)) from exc
        return manifest

    def _components(self, path):
        from .workflow import Workflow
        from .queue import QueueStore
        from .worker import Executor
        manifest = json.loads((path / "app.json").read_text(encoding="utf-8"))
        wf = Workflow(path)
        queue = QueueStore(path / "queue.sqlite")
        executor = Executor(queue, path / "attempts", provider_config=manifest["provider_config"],
                            max_workers=validate_workers(manifest.get("scheduling", {}).get("max_workers", self.max_workers)))
        return wf, queue, executor

    def tick(self, run_id):
        path = self.path(run_id)
        self._require_writable_run(path)
        with run_lock(path / "supervisor.lock"):
            atomic_json(path / "supervisor-heartbeat.json", {"pid": os.getpid(), "time": time.time()})
            wf, queue, executor = self._components(path)
            try:
                executor.recover()
                for result in queue.pending_results(run_id=run_id):
                    wf.apply(result, defer_progress=True)
                    queue.ack_result(result["result_id"])
                wf.repair_contract_failures()
                wf.recover_failures()
                with wf._read_tx() as (_, state):
                    paused = state['paused']
                if not paused:
                    self._execute_tools(path, wf)
                for job in wf.advance():
                    queue.enqueue(job)
                with wf._read_tx() as (_, state):
                    superseded = {jid for jid,j in state['jobs'].items() if j['state'] == 'superseded'}
                for job in queue.jobs(run_id=run_id, include_spec=False):
                    if job["job_id"] in superseded and job["state"] == "queued":
                        queue.cancel(job["job_id"], reason="workflow superseded before dispatch")
                executor.tick(run_id=run_id)
                return self._status(path, wf, queue)

            finally:
                executor.close()

    def _execute_tools(self, path, wf):
        from .tool_adapters import LocalToolAdapter
        manifest = json.loads((path / "app.json").read_text(encoding="utf-8"))
        adapter = LocalToolAdapter(path, enable_sandbox_tests=bool(manifest.get("sandbox_tests")))
        from copy import deepcopy
        with wf._read_tx() as (_, state):
            requests = [deepcopy(r) for r in state['tool_requests'] if r['state'] == 'requested']
        for request in requests:
            if request["kind"] in {"context.retrieve", "context.catalog"}:
                try:
                    result = wf.context_result(request)
                except (ValueError, KeyError, TypeError) as exc:
                    result = {"outcome": "failed", "error": str(exc), "receipt": {"adapter": "sealed_context", "executed": False}}
            elif request["kind"] in {"history.catalog", "history.read"}:
                try:
                    result = wf.history_result(request)
                except (ValueError, KeyError, TypeError) as exc:
                    result = {"outcome":"failed", "error":str(exc), "receipt":{"adapter":"scoped_history"}}
            elif request["kind"] == "user.consult":
                result = wf.user_guidance_result(request)
                if result is None:
                    continue
            elif request["kind"] in {"attachment.read", "attachment.catalog"}:
                try:
                    result = wf.attachment_tool(request)
                except (ValueError, KeyError, TypeError) as exc:
                    result = {"outcome": "failed", "error": str(exc), "receipt": {"adapter": "file_exchange"}}
            elif request["kind"] in {"web.search", "web.read"}:
                try:
                    result = (wf.web_read(request) if request["kind"] == "web.read" else
                              wf.web_result(request, mode=manifest.get("provider_config", {}).get("web_search", "live")))
                except (ValueError, KeyError, TypeError) as exc:
                    result = {"outcome": "failed", "error": str(exc),
                              "receipt": {"adapter": "codex_native_web", "executed": False}}
                if result is None:
                    continue
            elif request["kind"] in {"cognition.process", "cognition.status", "protocol.read"}:
                from .cognitive_operations import read_protocol
                try:
                    result = (wf.cognitive_result(request) if request["kind"] == "cognition.process" else
                              wf.cognitive_status_result(request) if request["kind"] == "cognition.status" else read_protocol(request))
                except (ValueError, KeyError, TypeError, IndexError) as exc:
                    result = {"outcome": "failed", "error": str(exc),
                              "receipt": {"adapter": "cognitive_operations", "executed": False}}
                if result is None:
                    continue
            elif adapter.supports(request["kind"]):
                result = adapter.execute(request)
            else:
                continue  # Unsupported actions remain explicit external duties.
            # Seal the next role only after all immediately available tool
            # results in this tick have been durably imported.
            wf.tool_result(request["id"], result, source="external", defer_progress=True)

    def reframe_comparison(self, run_id, request_id, selection):
        from .workflow import Workflow
        path = self.path(run_id)
        self._require_writable_run(path)
        if not isinstance(selection,dict) or set(selection)!={"result_pointers","old_source_ids","reason"}:
            raise ValueError("comparison scope requires result_pointers, old_source_ids and reason")
        with run_lock(path / "supervisor.lock"):
            return Workflow.open(path).reframe_cognitive_comparison(request_id,**selection)

    def _status(self, path, wf, queue):
        from .tool_recovery import test_status
        state = wf.status(compact=True)
        q = queue.status(run_id=state["run_id"], details=False)
        jobs = queue.jobs(run_id=state["run_id"], include_spec=False)
        attempts = queue.attempts()
        attempts_by_id = {a['attempt_id']:a for a in attempts}
        attempts_per_job = {}
        for attempt in attempts:
            attempts_per_job[attempt['job_id']] = attempts_per_job.get(attempt['job_id'],0) + 1
        semantic = {j["job_id"]: j for j in state["jobs"]}
        for job in jobs:
            job["status"] = job["state"]
            job["queue_status"] = job["state"]
            job["semantic_state"] = semantic.get(job["job_id"], {}).get("state")
            job["semantic_error"] = semantic.get(job["job_id"], {}).get("error")
            job["kind"] = semantic.get(job["job_id"], {}).get("role", "")
            job["attempt_count"] = attempts_per_job.get(job['job_id'],0)
            if job["state"] == "result_pending":
                current = attempts_by_id.get(job['current_attempt'],{})
                if current.get("state") in {"failed", "uncertain"}:
                    job["status"] = current["state"]
            if job["state"] == "acked" and job["semantic_state"] in {"failed", "superseded", "quarantined"}:
                job["status"] = job["semantic_state"]
            job.pop("spec", None)
        known = {j["job_id"] for j in jobs}
        for j in state["jobs"]:
            if j["job_id"] not in known:
                jobs.append({**j,"kind":j["role"],"status":j["state"],"semantic_state":j["state"],"attempt_count":0})
        # UI polling carries the current projection, not every sealed prompt and
        # model output. Full original bytes remain in attempts and SQLite.
        q = {k: v for k, v in q.items() if k not in {"jobs", "attempts", "pending_results"}}
        for topic in state["topics"]:
            revision = wf.parliament.inspect(topic["revision"])
            topic["q"] = "；".join(revision.get("questions", [])) or (state["goal"] if topic["id"] == "ROOT" else topic["id"])
        artifacts = []
        folder = path / "artifacts"
        if folder.exists():
            for item in sorted(folder.rglob("*")):
                if item.is_file() and item.resolve().is_relative_to(folder.resolve()):
                    artifacts.append({"path": item.relative_to(folder).as_posix(), "bytes": item.stat().st_size})
        active = self.threads.get(path.name)
        try:
            heartbeat = json.loads((path / "supervisor-heartbeat.json").read_text(encoding="utf-8"))
            recent = time.time() - heartbeat["time"] < 5
        except (OSError, ValueError, KeyError):
            recent = False
        return {"id": path.name, "workflow": state, "queue": q, "jobs": jobs,
                "python_tests": state['python_tests'],
                "sandbox_check": state.get("sandbox_check"),
                "artifacts": artifacts, "runner_active": bool(active and active.is_alive()) or recent,
                "scheduling": {"max_workers": json.loads((path / "app.json").read_text(encoding="utf-8")).get("scheduling", {}).get("max_workers", self.max_workers),
                    "scope":"per_run", "change_effect":"New dispatches only; active calls are not interrupted."},
                "error": self.errors.get(path.name)}

    def status(self, run_id):
        path = self.path(run_id)
        manifest = json.loads((path / "app.json").read_text(encoding="utf-8"))
        if manifest.get("checkpoint_read_only"):
            from .checkpoints import inspect_restored
            state = inspect_restored(path)
            for item in state["artifacts"]:
                if item["path"].startswith("artifacts/"):
                    item["path"] = item["path"][len("artifacts/"):]
            return state
        from .workflow import Workflow
        from .queue import QueueStore
        # SQLite read snapshots do not contend with model input preparation.
        wf = Workflow(path)
        queue = QueueStore(path / "queue.sqlite", read_only=True)
        return self._status(path, wf, queue)

    def configure_scheduling(self, run_id, max_workers):
        workers = validate_workers(max_workers)
        path = self.path(run_id)
        self._require_writable_run(path)
        with run_lock(path / "supervisor.lock"):
            manifest = json.loads((path / "app.json").read_text(encoding="utf-8"))
            manifest["scheduling"] = {"max_workers":workers}
            atomic_json(path / "app.json", manifest)
        return {"max_workers":workers, "scope":"per_run", "active_calls_unchanged":True}

    def export(self, run_id, destination):
        from .checkpoints import export_snapshot
        path = self.path(run_id)
        self._require_writable_run(path)
        with run_lock(path / "supervisor.lock"):
            return export_snapshot(path, destination, lock_held=True)

    def restore(self, snapshot, run_id):
        from .checkpoints import restore_snapshot
        return restore_snapshot(snapshot, self.path(run_id, exists=False))

    def fork(self, snapshot, run_id):
        from .checkpoints import fork_snapshot
        return fork_snapshot(snapshot, self.path(run_id, exists=False), run_id=run_id)

    def presented(self, run_id, question_id, *, channel="local_ui"):
        path = self.path(run_id)
        self._require_writable_run(path)
        with run_lock(path / "supervisor.lock"):
            wf, _, _ = self._components(path)
            return wf.question_presented(question_id, channel=channel)

    def pause(self, run_id):
        path = self.path(run_id)
        self._require_writable_run(path)
        with run_lock(path / "supervisor.lock"):
            wf, queue, _ = self._components(path)
            queue.pause(run_id)
            wf.pause()
        return {"paused": True, "note": "停止后继派发；在途模型的原始返回仍会保留。"}

    def resume(self, run_id):
        path = self.path(run_id)
        self._require_writable_run(path)
        with run_lock(path / "supervisor.lock"):
            wf, queue, _ = self._components(path)
            wf.resume()
            queue.resume(run_id)
        self.errors.pop(run_id, None)
        return {"resumed": True}

    def input(self, run_id, text, *, question_id=None, correction=False, source="user", topic_id=None, profile_scope="collaboration", attachments=None, submission_id=None):
        if attachments and not text:
            text = "提交附件，请按本次文件版本处理。"
        if not isinstance(text, str) or not text.strip() or len(text) > 12000:
            raise ValueError("输入须为 1–12000 字符")
        path = self.path(run_id)
        self._require_writable_run(path)
        with run_lock(path / "supervisor.lock"):
            wf, _, _ = self._components(path)
            return wf.input(text, question_id=question_id, correction=correction,
                            source="human" if source == "user" else source, topic_id=topic_id, profile_scope=profile_scope,
                            attachments=attachments, submission_id=submission_id)

    def retry(self, run_id, job_id, *, reason, allow_uncertain=False):
        if not reason or not isinstance(reason, str):
            raise ValueError("Retry requires an explicit reason")
        path = self.path(run_id)
        self._require_writable_run(path)
        with run_lock(path / "supervisor.lock"):
            wf, queue, _ = self._components(path)
            # Explicit operator disposition. A successful/unconsumed semantic
            # result cannot be discarded through this execution retry endpoint.
            for result in queue.pending_results(run_id=run_id):
                if result["job_id"] == job_id and result["outcome"] in {"failed", "uncertain"}:
                    if result["outcome"] == "uncertain" and not allow_uncertain:
                        raise ValueError("执行结果不明：需明确允许可能重复的模型调用。")
                    atomic_json(path / "retry-dispositions" / (result["result_id"] + ".json"),
                                {"result_id": result["result_id"], "reason": reason,
                                 "action": "operator_requested_retry", "at": utcnow()})
                    wf.apply(result)
                    queue.ack_result(result["result_id"])
            current = next((j for j in queue.jobs(run_id=run_id) if j["job_id"] == job_id), None)
            if current is None:
                semantic = next((j for j in wf.status()["jobs"] if j["job_id"] == job_id), None)
                if semantic and semantic["state"] in {"failed","quarantined"}:
                    if semantic.get("uncertain") and not allow_uncertain:
                        raise ValueError("执行结果不明：需明确允许可能重复的模型调用。")
                    return {"new_job_id":wf.retry(job_id),"previous_job_id":job_id}
                raise ValueError("Unknown or non-retryable job")
            if current["state"] == "uncertain" and not allow_uncertain:
                raise ValueError("执行结果不明：需明确允许可能重复的模型调用。")
            atomic_json(path / "retry-dispositions" / ("request-" + uuid.uuid4().hex + ".json"),
                        {"job_id": job_id, "reason": reason, "allow_uncertain": allow_uncertain, "at": utcnow()})
            semantic = next((j for j in wf.status()["jobs"] if j["job_id"] == job_id), {})
            if semantic.get("uncertain") and not allow_uncertain:
                raise ValueError("执行结果不明：需明确允许可能重复的模型调用。")
            if semantic.get("state") in {"failed","quarantined"}:
                new_id = wf.retry(job_id)
                return {"new_job_id": new_id, "previous_job_id": job_id}
            return queue.retry(job_id, reason=reason, allow_uncertain=allow_uncertain)

    def check_sandbox(self, run_id):
        from .sandbox_runtime import probe
        from .tool_adapters import _reject_links
        path = self.path(run_id)
        self._require_writable_run(path)
        with run_lock(path / "supervisor.lock"):
            wf, _, _ = self._components(path)
            directory = _reject_links(path / "sandbox-checks" / uuid.uuid4().hex)
            workspace = directory / "workspace"
            workspace.mkdir(parents=True)
            result = probe(workspace, untrusted_root=path, log_dir=directory)
            result.update(checked_at=utcnow(),record_path=directory.relative_to(path).as_posix()+"/result.json")
            atomic_json(directory / "result.json", result)
            with wf._tx() as (_,state):
                state["sandbox_check"] = result
            return result

    def retry_tool(self, run_id, request_id, *, reason, allow_uncertain=False):
        path = self.path(run_id)
        self._require_writable_run(path)
        with run_lock(path / "supervisor.lock"):
            manifest = json.loads((path / "app.json").read_text(encoding="utf-8"))
            if not manifest.get("sandbox_tests"):
                raise ValueError("Python test execution is not enabled for this run")
            wf, _, _ = self._components(path)
            return wf.retry_tool(request_id,reason=reason,allow_uncertain=allow_uncertain)

    def dismiss_tool_recovery(self, run_id, request_id, *, reason):
        path = self.path(run_id)
        self._require_writable_run(path)
        with run_lock(path / "supervisor.lock"):
            wf, _, _ = self._components(path)
            return wf.dismiss_tool_recovery(request_id,reason=reason)

    def start(self, run_id):
        self.resume(run_id)
        with self.thread_lock:
            thread = self.threads.get(run_id)
            if thread and thread.is_alive():
                return {"started": False, "already_running": True}
            thread = threading.Thread(target=self.run, args=(run_id,), daemon=True,
                                      name="StudioSupervisor-" + run_id)
            self.threads[run_id] = thread
            thread.start()
        return {"started": True}

    def recover(self, run_id, *, context_policy=None):
        from .workflow import Workflow
        from .context_lifecycle import policy
        path = self.path(run_id)
        self._require_writable_run(path)
        with run_lock(path / "supervisor.lock"):
            wf = Workflow(path)
            if context_policy is not None:
                with wf._tx() as (_, state):
                    proposed = {**state["metadata"].get("context_policy", {}), **context_policy}
                    policy({"metadata":{"context_policy":proposed}})
                    state["metadata"]["context_policy"] = proposed
            return wf.recover_scheduling()

    def run(self, run_id, *, poll_interval=0.5):
        """No round/time cap. Stop when there is no actionable local work."""
        self.errors.pop(run_id, None)
        while not self.shutdown_event.is_set():
            try:
                snapshot = self.tick(run_id)
                state = snapshot["workflow"]
                jobs = snapshot["jobs"]
                # A worker may finish after tick consumed the earlier results.
                # Drain that durable result before treating attention/waiting as
                # a stop, including failed/uncertain display projections.
                active = any(j["status"] in {"queued", "leased", "claimed", "running", "result_pending"}
                             or j.get("queue_status") == "result_pending" for j in jobs)
                phase = state.get("state", state.get("status"))
                if state.get("automatic_retries_pending"):
                    active = True
                if phase == "paused" or (not active and phase in {"completed", "delivered", "quiescent", "waiting", "blocked", "stalled", "needs_attention", "failed"}):
                    return snapshot
                if not active and any(j["status"] == "uncertain" for j in jobs):
                    return snapshot
            except Exception as exc:
                self.errors[run_id] = type(exc).__name__ + ": " + str(exc)
                return {"error": self.errors[run_id]}
            self.shutdown_event.wait(poll_interval)

    def artifact(self, run_id, name):
        folder = self.path(run_id) / "artifacts"
        path = (folder / name).resolve()
        if not path.is_relative_to(folder.resolve()) or not path.is_file():
            raise ValueError("Artifact not found")
        return path

    def saved_file(self, run_id, file_id, *, preview=False, page=None):
        """Read committed immutable bytes; arbitrary client paths are never opened."""
        import sqlite3
        from contextlib import closing
        from .file_exchange import sha
        path = self.path(run_id)
        with closing(sqlite3.connect((path / 'workflow.db').as_uri() + '?mode=ro', uri=True)) as con:
            state = read_state(con)
            record = state.get('file_exchange', {}).get(file_id)
            if not record:
                raise ValueError('附件不存在')
            if record['origin'] == 'model':
                row = con.execute('SELECT content FROM artifacts WHERE path=?', (record['path'],)).fetchone()
                text = row[0] if row else None
                raw = text.encode('utf-8') if text is not None else None
            else:
                row = con.execute('SELECT raw,text FROM attachment_blobs WHERE id=?', (file_id,)).fetchone()
                raw, text = row if row else (None, None)
            if raw is None or sha(raw) != record['sha256']:
                raise ValueError('附件原件校验失败')
            if page is not None or (preview and record['extraction'].get('kind') == 'image'):
                page = 1 if page is None else page
                pages = record['extraction'].get('pages', [])
                p = next((p for p in pages if p['page'] == page), None)
                if type(page) is not int or not p or not p.get('image_sha256'):
                    raise ValueError('该页图尚未生成；议题请求读取后可查看，原件始终可下载')
                from .media import image_from_storage
                descriptor = {'file_id': file_id, 'file_sha256': record['sha256'], 'version': record['version'],
                              'page': page, 'image_sha256': p['image_sha256']}
                content = image_from_storage(con, state, descriptor)
                return {'name': record['name'], 'content': content, 'sha256': p['image_sha256'], 'content_type': 'image/png'}
            if preview:
                if text is not None and sha(text.encode('utf-8')) != record['extraction']['sha256']:
                    raise ValueError('附件提取正文校验失败')
                if record['extraction'].get('kind') == 'pdf':
                    rows = con.execute('SELECT page,text FROM attachment_pages WHERE file_id=? ORDER BY page', (file_id,)).fetchall()
                    text = '\n\n'.join('=== PDF 物理页 %s ===\n%s' % (p, t or '[无提取文字；需查看页图]') for p, t in rows)
                content = (text if text is not None else '尚不能解析正文。原件已保存，可下载。\n' + record['extraction']['limitations']).encode('utf-8')
            else:
                content = raw
            return {'name': record['name'], 'content': content, 'sha256': record['sha256']}

    def close(self):
        self.shutdown_event.set()
