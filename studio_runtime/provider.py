"""Fresh Codex CLI invocations with durable, explicit input and raw receipts."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import time

from .contracts import JobSpec, SchemaError, atomic_json, digest, validate_output
from .cognitive_protocols import inspect_prompt
from .input_lifecycle import validate_input_task, InputContractError

MODEL = "gpt-6-astra"
REASONING_EFFORT = "xhigh"


@dataclass
class ProviderConfig:
    executable: str = "codex.exe" if os.name == "nt" else "codex"
    model: str | None = MODEL
    reasoning_effort: str = REASONING_EFFORT
    timeout_seconds: float = 3600
    command_prefix: list[str] = field(default_factory=list)
    evidence_kind: str = "real_codex_cli"
    web_search: str = "live"

    @classmethod
    def from_value(cls, config=None):
        return config if isinstance(config, cls) else cls(**(config or {}))


class CodexProvider:
    def __init__(self, config=None):
        self.config = ProviderConfig.from_value(config)
        if self.config.model not in {None, MODEL} or self.config.reasoning_effort != REASONING_EFFORT:
            raise ValueError("Studio requires gpt-6-astra with reasoning effort xhigh")
        if self.config.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if self.config.command_prefix and self.config.evidence_kind == "real_codex_cli":
            raise ValueError("custom command_prefix requires an explicit non-real evidence_kind")
        if self.config.web_search not in {"disabled", "cached", "live"}:
            raise ValueError("web_search must be disabled, cached or live")

    def _model(self):
        return MODEL, "studio_model_policy"

    def execute(self, job: JobSpec, attempt_id: str, attempt_dir: Path) -> dict:
        native_web = job.metadata.get("native_web")
        search_mode = "disabled"
        if native_web:
            if json.loads(job.prompt).get("native_web") != native_web:
                raise ValueError("native search policy differs from sealed input")
            if native_web.get("mode") != self.config.web_search or self.config.web_search == "disabled":
                raise ValueError("native search policy differs from runtime configuration")
            search_mode = self.config.web_search
        cognitive_manifest = None
        if "cognitive_protocol" in job.metadata:
            cognitive_manifest = inspect_prompt(job.prompt, job.role, require_current=False)
            if cognitive_manifest != job.metadata["cognitive_protocol"]:
                raise ValueError("dispatch cognitive protocol differs from sealed job")
        directory = Path(attempt_dir).resolve()
        directory.mkdir(parents=True, exist_ok=True)
        completion_path = directory / "completion.json"
        if completion_path.exists():
            previous = json.loads(completion_path.read_text(encoding="utf-8"))
            if previous["job_id"] != job.job_id or previous["attempt_id"] != attempt_id or previous["receipt"]["job_sha256"] != digest(job.to_dict()):
                raise ValueError("attempt directory belongs to a different invocation")
            return previous
        # A started invocation must never be relaunched by calling execute again.
        marker = directory / "invocation-intent.json"
        if marker.exists():
            raise RuntimeError("invocation already has an intent; reconcile instead of relaunching")
        started = time.time()
        atomic_json(marker, {"job_id": job.job_id, "attempt_id": attempt_id, "job_sha256": digest(job.to_dict()), "created_at": started})
        prompt_path = directory / "prompt.txt"
        with prompt_path.open("x", encoding="utf-8", newline="") as handle:
            handle.write(job.prompt)
            handle.flush()
            os.fsync(handle.fileno())
        atomic_json(directory / "schema.json", job.schema)
        atomic_json(directory / "job.json", job.to_dict())
        stdout_path, stderr_path = directory / "events.jsonl", directory / "stderr.txt"
        last_path = directory / "last-message.txt"
        receipt = {
            "job_id": job.job_id, "attempt_id": attempt_id, "job_sha256": digest(job.to_dict()),
            "prompt_sha256": hashlib.sha256(job.prompt.encode("utf-8")).hexdigest(),
            "schema_sha256": digest(job.schema), "started_at": started,
            "evidence_kind": self.config.evidence_kind, "cwd": str(directory),
            "prompt_path": str(prompt_path), "schema_path": str(directory / "schema.json"),
            "events_path": str(stdout_path), "stderr_path": str(stderr_path), "raw_result_path": str(last_path),
            "fresh_process": True, "ephemeral": True, "resume_or_fork": False,
            "permissions": {"sandbox": "read-only", "approval_policy": "never", "shell": False,
                            "apps": False, "plugins": False, "multi_agent": False, "web_search": search_mode},
            "context_boundary": "Exact prompt and schema retained; project documents disabled. CLI/platform instructions and authentication context are not proven absent. Fresh CLI does not prove absolute context isolation.",
            "authentication": "Uses existing host authentication without copying credentials or modifying configuration.",
            "environment": "Inherited process environment; secrets and values are not copied into receipts.",
            "exit_code": None, "pid": None, "usage": None, "usage_source": None,
            "cognitive_protocol": cognitive_manifest,
        }
        outcome, payload, error = "failed", None, None
        command = []
        process = None
        try:
            if job.role in {"parent", "author"}:
                validate_input_task(json.loads(job.prompt).get("task", {}))
            model, model_source = self._model()
            receipt.update(model=model, model_source=model_source, reasoning_effort=REASONING_EFFORT)
            from .context_budget import MODEL_CONTEXT_TOKENS, CLI_MAX_INPUT_CHARS
            if len(job.prompt) > CLI_MAX_INPUT_CHARS:
                # Recheck immutable jobs made by an older installation before
                # starting a process. This is a known non-executed rejection.
                raise ValueError('input_too_large: ' + json.dumps({
                    "max_chars": CLI_MAX_INPUT_CHARS, "actual_chars": len(job.prompt)}))
            budget = job.metadata.get("capacity", {})
            window = budget.get("context_tokens", MODEL_CONTEXT_TOKENS)
            compact_at = budget.get("effective_context_tokens", window * 95 // 100) - budget.get("output_reserve_tokens", 128000)
            receipt["context_capacity"] = {
                "requested_window_tokens": window, "auto_compact_token_limit": compact_at,
                "preflight": budget, "source": "sealed_job_policy",
                "effective_server_window_verified": False,
                "boundary": "Explicit CLI configuration, not proof of server capacity. Provider errors and actual usage are recorded independently.",
            }
            prefix = self.config.command_prefix or [shutil.which(self.config.executable) or self.config.executable]
            version = subprocess.run(prefix + ["--version"], capture_output=True, timeout=15,
                                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            receipt["provider_version"] = version.stdout.decode("utf-8", errors="replace").strip()
            command = prefix + ["exec", "--ephemeral", "--ignore-user-config", "--json", "--skip-git-repo-check",
                "--sandbox", "read-only", "--disable", "shell_tool", "--disable", "apps", "--disable", "plugins",
                "--disable", "multi_agent", "-c", "project_doc_max_bytes=0", "-c", 'web_search=' + json.dumps(search_mode),
                "-c", 'approval_policy="never"', "-C", str(directory), "--output-schema", str(directory / "schema.json"),
                "-o", str(last_path)]
            if model:
                command += ["--model", model]
            command += ["-c", 'model_reasoning_effort="xhigh"']
            command += ["-c", f"model_context_window={window}",
                        "-c", f"model_auto_compact_token_limit={compact_at}"]
            if native_web and native_web.get("allowed_domains"):
                command += ["-c", "tools.web_search.allowed_domains=" + json.dumps(native_web["allowed_domains"])]
            from .media import dispatch_images
            image_paths, images = dispatch_images(job, directory)
            if images:
                receipt['native_images'] = images
                receipt['native_images_dispatched'] = False
                atomic_json(directory / 'image-manifest.json', images)
                for path in image_paths:
                    command += ['--image', str(path)]
            # --image accepts multiple values. End option parsing so the stdin
            # prompt marker is never consumed as another image filename.
            command += ["--", "-"]
            atomic_json(directory / "command.json", command)
            receipt["command"] = command
            atomic_json(directory / "receipt-started.json", receipt)
            with stdout_path.open("xb") as stdout, stderr_path.open("xb") as stderr:
                process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=stdout, stderr=stderr,
                    cwd=directory, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                receipt["pid"] = process.pid
                atomic_json(directory / "process.json", {"pid": process.pid, "attempt_id": attempt_id, "started_at": time.time()})
                try:
                    process.communicate(job.prompt.encode("utf-8"), timeout=self.config.timeout_seconds)
                    if images:
                        receipt['native_images_dispatched'] = process.returncode == 0
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate()
                    # The remote request may have executed even when local process is killed.
                    outcome = "uncertain"
                    error = {"type": "provider_timeout", "message": "Local provider terminated after timeout; remote completion is unknown. Explicit resolution required."}
                receipt["exit_code"] = process.returncode
                stdout.flush()
                stderr.flush()
                os.fsync(stdout.fileno())
                os.fsync(stderr.fileno())
            events = []
            malformed = []
            for line_number, line in enumerate(stdout_path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                try:
                    event = json.loads(line)
                    if isinstance(event, dict):
                        events.append((line_number, event))
                    else:
                        malformed.append(line_number)
                except json.JSONDecodeError:
                    malformed.append(line_number)
            receipt["malformed_event_lines"] = malformed
            receipt["compaction_events"] = [{"line": line, "type": e.get("type"), "item_type": e.get("item", {}).get("type")}
                for line, e in events if "compact" in str(e.get("type", "")) or
                "compact" in str(e.get("item", {}).get("type", ""))]
            if native_web:
                from .web_research import capture_events
                captured = capture_events(events, mode=search_mode,
                    events_sha256=hashlib.sha256(stdout_path.read_bytes()).hexdigest(),
                    started_at=started, finished_at=time.time())
                atomic_json(directory / "web-evidence.json", captured)
                receipt["native_web"] = captured
                receipt["native_web_sha256"] = digest(captured)
            for line_number, event in events:
                if event.get("type") == "thread.started":
                    receipt["provider_thread_id"] = event.get("thread_id")
                if event.get("type") == "turn.completed" and isinstance(event.get("usage"), dict):
                    receipt["usage"] = event["usage"]
                    receipt["usage_source"] = {"path": str(stdout_path), "line": line_number, "event_type": "turn.completed"}
            if error is None:
                if process.returncode != 0:
                    messages = [str(e.get("error") or e.get("message") or "") for _,e in events if e.get("type") in {"error","turn.failed"}]
                    error = {"type": "provider_exit", "message": f"Provider exited with code {process.returncode}. " + "\n".join(messages)[-4000:]}
                elif not last_path.exists():
                    error = {"type": "missing_final_output", "message": "Successful process exit without final-message artifact."}
                else:
                    raw = last_path.read_text(encoding="utf-8")
                    try:
                        parsed = json.loads(raw)
                        if not isinstance(parsed, dict):
                            raise SchemaError("Structured role output must be a JSON object")
                        validate_output(parsed, job.schema)
                        payload = parsed
                        outcome = "succeeded"
                    except (ValueError, KeyError, TypeError) as exc:
                        error = {"type": "invalid_structured_output", "message": str(exc)}
        except InputContractError as exc:
            error = {"type": "invalid_input_contract", "message": str(exc)}
            receipt["fresh_process"] = False
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            error = {"type": "provider_launch_or_io_error", "message": str(exc)}
            if process is not None:
                outcome = "uncertain"
                if process.poll() is None:
                    process.kill()
                    process.wait()
                receipt["exit_code"] = process.returncode
        if error:
            words = str(error).lower()
            if stderr_path.exists():
                # Classify diagnostics without copying potentially sensitive
                # stderr into the public UI error message.
                words += stderr_path.read_text(encoding="utf-8", errors="replace")[-12000:].lower()
            category = ("input_contract" if error["type"] == "invalid_input_contract" else
                        "context_capacity" if any(x in words for x in ("context_length", "context window", "too many tokens", "maximum context")) else
                        "authentication" if any(x in words for x in ("unauthorized", "authentication", "401", "403")) else
                        "transport" if any(x in words for x in ("timed out", "timeout", "connection", "network", "routing discovery", "429", "503", "502", "rate limit", "dns error", "failed to resolve", "error sending request", "temporarily unavailable")) else
                        "output_contract" if error["type"] in {"invalid_structured_output","missing_final_output"} else "unknown")
            error.update(category=category, retryable=category == "transport" and outcome != "uncertain")
            from .context_budget import character_error
            limit_error = character_error(words)
            if limit_error:
                error.update(limit_error)
            if process is None:
                receipt["fresh_process"] = False
        receipt.update(finished_at=time.time(), outcome=outcome, error=error, command=command)
        for label, path in (("events_sha256", stdout_path), ("stderr_sha256", stderr_path), ("raw_result_sha256", last_path)):
            if path.exists():
                receipt[label] = hashlib.sha256(path.read_bytes()).hexdigest()
        receipt_path = directory / "receipt.json"
        atomic_json(receipt_path, receipt)
        result = {"job_id": job.job_id, "attempt_id": attempt_id, "outcome": outcome, "payload": payload,
                  "error": error, "receipt": receipt, "receipt_path": str(receipt_path)}
        result["result_sha256"] = digest(result)
        atomic_json(completion_path, result)
        return result
