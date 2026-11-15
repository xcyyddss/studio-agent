"""Local readers and opt-in native-sandbox tests with a durable ledger.

Model-selected paths can only read ordinary files below this run's artifacts.
The ledger is separately owned under tool-executions. Defaults never execute
code. Explicitly enabled Python tests delegate enforcement to official Codex MXC,
not Python/AST filtering; the command and policy are fixed by trusted application
code. Registered custom handlers have Python process authority, not a sandbox.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import threading
import time

from .contracts import atomic_json, canonical, digest, validate_output


class ToolError(ValueError):
    pass


class ToolConflict(ToolError):
    pass


class _ToolOutcome:
    def __init__(self, outcome, payload, error=None):
        self.outcome, self.payload, self.error = outcome, payload, error


def _sandbox_environment():
    """No inherited API tokens, provider credentials, PYTHONPATH or proxy vars."""
    allowed = {"systemroot", "systemdrive", "windir", "comspec", "path", "pathext", "temp", "tmp", "userprofile",
               "localappdata", "appdata", "programfiles", "programfiles(x86)", "programdata",
               "programw6432", "commonprogramfiles", "commonprogramfiles(x86)", "commonprogramw6432",
               "allusersprofile", "public", "homedrive", "homepath", "username", "userdomain",
               "userdomain_roamingprofile", "processor_architecture", "number_of_processors", "os"}
    return {key: value for key, value in os.environ.items() if key.lower() in allowed}


def _sandbox_command(workspace, *, probe=False):
    from .sandbox_runtime import command
    return command(workspace, probe=probe)


def _terminate_own_process_tree(process):
    killer = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "taskkill.exe"
    try:
        killed = subprocess.run([str(killer), "/PID", str(process.pid), "/T", "/F"],
                                stdin=subprocess.DEVNULL, capture_output=True, timeout=5, shell=False,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        termination = {"method": "taskkill_own_process_tree", "exit_code": killed.returncode}
    except (OSError, subprocess.TimeoutExpired) as exc:
        termination = {"method": "taskkill_own_process_tree", "error": type(exc).__name__}
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)
    return termination


def _run_bounded(command, *, cwd, timeout_seconds, output_limit=65536, log_dir=None):
    """Bound pipe memory and time; never feed command text to a system shell."""
    process = subprocess.Popen(command, cwd=cwd, env=_sandbox_environment(), stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    chunks = {"stdout": bytearray(), "stderr": bytearray()}
    exceeded = threading.Event()
    def drain(name, pipe):
        try:
            while block := pipe.read(8192):
                room = max(0, output_limit - len(chunks[name]))
                chunks[name].extend(block[:room])
                if len(block) > room:
                    exceeded.set()
        finally:
            pipe.close()
    readers = [threading.Thread(target=drain, args=(name, getattr(process, name)), daemon=True)
               for name in ("stdout", "stderr")]
    for thread in readers:
        thread.start()
    deadline, interruption, termination = time.monotonic() + timeout_seconds, None, None
    try:
        while process.poll() is None:
            if exceeded.is_set() or time.monotonic() >= deadline:
                interruption = "output_limit" if exceeded.is_set() else "timeout"
                # Only our own Popen PID; native MXC also tears down remaining
                # sandbox children after its foreground command ends.
                termination = _terminate_own_process_tree(process)
                break
            try:
                process.wait(timeout=0.05)
            except subprocess.TimeoutExpired:
                pass
    except BaseException:
        if process.poll() is None:
            _terminate_own_process_tree(process)
        raise
    for thread in readers:
        thread.join(timeout=2)
    if any(thread.is_alive() for thread in readers):
        interruption = interruption or "unclosed_output_pipe"
    if exceeded.is_set():
        interruption = interruption or "output_limit"
    if log_dir is not None:
        for name in ("stdout", "stderr"):
            _reject_links(Path(log_dir) / (name + ".bin")).write_bytes(bytes(chunks[name]))
    return {"exit_code": process.returncode, "stdout": bytes(chunks["stdout"]).decode("utf-8", "replace"),
            "stderr": bytes(chunks["stderr"]).decode("utf-8", "replace"), "timeout": interruption == "timeout",
            "interruption": interruption, "output_truncated": exceeded.is_set(), "output_limit_bytes_per_stream": output_limit,
            "termination": termination, "controller_pid": process.pid,
            "output_view_encoding": "utf-8 with replacement; retained .bin files contain exact captured bytes",
            "output_sha256": {name: hashlib.sha256(bytes(chunks[name])).hexdigest() for name in chunks}}


def sandbox_diagnostics(*, probe=False, cwd=None):
    from .sandbox_runtime import command, probe as check, diagnosis
    workspace = Path(cwd or Path.cwd())
    if probe:
        return check(workspace)
    status = {"adapter": "sandbox.python_tests", "default_enabled": False,
              "probe_performed": False, "available": None}
    try:
        argv, policy = command(workspace, probe=True)
        status.update(executable=argv[0], python=policy["installations"]["python"]["resolved"],
                      policy=policy, reason="Installation resolved; native capability has not been probed")
    except (OSError, ValueError, RuntimeError) as exc:
        status.update(available=False, reason=str(exc), diagnostic=diagnosis(
            getattr(exc, "code", "startup_failed"), str(exc), details={"path":getattr(exc,"path",None)}))
    return status


def _now():
    return datetime.now(timezone.utc).isoformat()


def _reject_links(path):
    """Reject symlink/reparse ancestors before resolve can hide them."""
    path = Path(path).absolute()
    for probe in (path, *path.parents):
        if probe.is_symlink():
            raise ToolError("symbolic links are not permitted: " + str(probe))
        if probe.exists():
            info = probe.stat(follow_symlinks=False)
            if getattr(info, "st_file_attributes", 0) & 0x400:
                raise ToolError("junctions and reparse points are not permitted: " + str(probe))
    return path.resolve()


def _relative(value):
    if not isinstance(value, str) or not value or len(value) > 512:
        raise ToolError("path must be nonempty relative POSIX text, at most 512 characters")
    if "\\" in value or ":" in value or any(ord(c) < 32 for c in value):
        raise ToolError("path contains a forbidden separator or control character")
    p = PurePosixPath(value)
    if p.is_absolute() or value != p.as_posix() or any(part in {".", ".."} for part in p.parts):
        raise ToolError("path must be canonical and cannot escape artifacts")
    for part in p.parts:
        if part.endswith((".", " ")) or re.fullmatch(r"(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", part):
            raise ToolError("reserved filesystem path")
    return p


def _json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ToolError("duplicate JSON property: " + key)
            result[key] = value
        return result
    def constant(value):
        raise ToolError("non-finite JSON number: " + value)
    value = json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)
    _bound_tree(value)
    return value


def _bound_tree(value, *, max_nodes=50000, max_depth=64):
    todo, count = [(value, 0)], 0
    while todo:
        item, depth = todo.pop()
        count += 1
        if count > max_nodes or depth > max_depth:
            raise ToolError("JSON structure exceeds the supported size/depth")
        if isinstance(item, float) and not math.isfinite(item):
            raise ToolError("non-finite JSON number")
        if isinstance(item, dict):
            todo.extend((v, depth + 1) for v in item.values())
        elif isinstance(item, list):
            todo.extend((v, depth + 1) for v in item)


def _safe_schema(schema):
    """Bounded non-regex, non-reference subset; no regex or ref recursion DoS."""
    allowed = {"type", "properties", "required", "additionalProperties", "items", "enum", "const",
               "minItems", "maxItems", "uniqueItems", "minLength", "maxLength", "minimum", "maximum",
               "exclusiveMinimum", "exclusiveMaximum", "minProperties", "maxProperties", "description", "title"}
    _bound_tree(schema, max_nodes=2000, max_depth=24)
    todo = [schema]
    while todo:
        current = todo.pop()
        if isinstance(current, bool):
            continue
        if not isinstance(current, dict) or set(current) - allowed:
            raise ToolError("unsupported JSON schema; references, regexes and combinators are not enabled")
        if "type" in current:
            types = [current["type"]] if isinstance(current["type"], str) else current["type"]
            if not isinstance(types, list) or not types or any(t not in {"null", "boolean", "object", "array", "string", "integer", "number"} for t in types):
                raise ToolError("schema type must contain supported JSON types")
        if "required" in current and (not isinstance(current["required"], list) or any(not isinstance(x, str) for x in current["required"])):
            raise ToolError("schema required must be a string array")
        if "enum" in current and (not isinstance(current["enum"], list) or not current["enum"]):
            raise ToolError("schema enum must be a nonempty array")
        if "uniqueItems" in current and not isinstance(current["uniqueItems"], bool):
            raise ToolError("schema uniqueItems must be boolean")
        for key in ("minItems", "maxItems", "minLength", "maxLength", "minProperties", "maxProperties"):
            if key in current and (type(current[key]) is not int or current[key] < 0):
                raise ToolError("schema count limits must be nonnegative integers")
        for key in ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum"):
            if key in current and (isinstance(current[key], bool) or not isinstance(current[key], (int, float))):
                raise ToolError("schema numeric limits must be numbers")
        if "properties" in current:
            if not isinstance(current["properties"], dict):
                raise ToolError("schema properties must be an object")
            todo.extend(current["properties"].values())
        for key in ("items", "additionalProperties"):
            if key in current:
                todo.append(current[key])


class ArtifactReader:
    """Bounded file reading capability passed to explicitly trusted handlers."""
    def __init__(self, root, max_input_bytes):
        self.root = _reject_links(root)
        self.max_input_bytes = max_input_bytes
        self.reads = []

    def read(self, path):
        relative = _relative(path)
        parts = relative.parts[1:] if relative.parts[0] == "artifacts" else relative.parts
        if not parts or parts[0] in {".tool-executions", "tool-executions"}:
            raise ToolError("a public artifact file is required")
        selected = _reject_links(self.root.joinpath(*parts))
        if not selected.is_relative_to(self.root) or not selected.is_file():
            raise ToolError("artifact is missing or outside the run artifacts directory")
        # Hardlinks can otherwise expose an inode outside the allowed tree.
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(selected, flags)
        with os.fdopen(descriptor, "rb") as handle:
            info = os.fstat(handle.fileno())
            if info.st_nlink > 1:
                raise ToolError("hard-linked artifacts are not accepted")
            if info.st_size > self.max_input_bytes:
                raise ToolError("artifact exceeds max_input_bytes")
            data = handle.read(self.max_input_bytes + 1)
            after = os.fstat(handle.fileno())
            if len(data) > self.max_input_bytes or info.st_size != after.st_size or info.st_mtime_ns != after.st_mtime_ns:
                raise ToolError("artifact exceeded its limit or changed while reading")
        record = {"path": "artifacts/" + "/".join(parts), "bytes": len(data),
                  "sha256": hashlib.sha256(data).hexdigest()}
        self.reads.append(record)
        return data, record

    def text(self, path):
        data, info = self.read(path)
        return data.decode("utf-8-sig", errors="strict"), info


def _file_info(reader, arguments):
    data, info = reader.read(arguments["path"])
    try:
        data.decode("utf-8-sig", errors="strict")
        utf8 = True
    except UnicodeDecodeError:
        utf8 = False
    return dict(info, utf8=utf8)


def _read(reader, arguments):
    text, info = reader.text(arguments["path"])
    offset, limit = arguments.get("offset", 0), arguments.get("max_chars", 16000)
    chunk = text[offset:offset + limit]
    return dict(info, content=chunk, offset=offset, end=offset + len(chunk), total_chars=len(text),
                complete=offset == 0 and len(chunk) == len(text), encoding="utf-8")


def _text_analyze(reader, arguments):
    text, info = reader.text(arguments["path"])
    return dict(info, chars=len(text), lines=len(text.splitlines()), whitespace_segments=len(text.split()),
                contains=[{"literal": item, "occurrences": text.count(item)} for item in arguments.get("contains", [])])


def _json_validate(reader, arguments):
    text, info = reader.text(arguments["path"])
    value = _json(text)
    if "schema" in arguments:
        _safe_schema(arguments["schema"])
        validate_output(value, arguments["schema"])
    typename = "null" if value is None else "boolean" if isinstance(value, bool) else "object" if isinstance(value, dict) else "array" if isinstance(value, list) else "string" if isinstance(value, str) else "number"
    return dict(info, valid=True, json_type=typename, schema_checked="schema" in arguments,
                top_level_items=len(value) if isinstance(value, (list, dict)) else None)


def _args(**properties):
    return {"type": "object", "properties": {"path": {"type": "string", "minLength": 1, "maxLength": 512}, **properties},
            "required": ["path"], "additionalProperties": False}


class LocalToolAdapter:
    BUILT_INS = frozenset({"artifact.file_info", "artifact.read", "artifact.text_analyze", "artifact.json_validate", "sandbox.python_tests"})

    def __init__(self, run_dir, *, max_input_bytes=1024 * 1024, max_output_bytes=1024 * 1024, enable_sandbox_tests=False):
        self.run_dir = _reject_links(run_dir)
        if not self.run_dir.is_dir():
            raise ToolError("run directory does not exist")
        if not isinstance(max_input_bytes, int) or not 1 <= max_input_bytes <= 16 * 1024 * 1024:
            raise ToolError("max_input_bytes must be 1..16777216")
        if not isinstance(max_output_bytes, int) or not 1024 <= max_output_bytes <= 16 * 1024 * 1024:
            raise ToolError("max_output_bytes must be 1024..16777216")
        self.max_input_bytes, self.max_output_bytes = max_input_bytes, max_output_bytes
        if type(enable_sandbox_tests) is not bool:
            raise ToolError("enable_sandbox_tests must be an explicit boolean")
        self.enable_sandbox_tests = enable_sandbox_tests
        self.operations = {}
        self.register("artifact.file_info", _file_info, arguments_schema=_args(), description="Read bounded artifact bytes and return SHA-256, size and UTF-8 status", trusted_application_code=True)
        self.register("artifact.read", _read, arguments_schema=_args(offset={"type": "integer", "minimum": 0}, max_chars={"type": "integer", "minimum": 1, "maximum": 65536}), description="Read an explicit UTF-8 artifact range; no evidence-reference expansion", trusted_application_code=True)
        self.register("artifact.text_analyze", _text_analyze, arguments_schema=_args(contains={"type": "array", "maxItems": 32, "items": {"type": "string", "minLength": 1, "maxLength": 256}}), description="Count text characters, lines, whitespace segments and literal occurrences", trusted_application_code=True)
        self.register("artifact.json_validate", _json_validate, arguments_schema=_args(schema={"type": ["object", "boolean"]}), description="Parse strict JSON and optionally validate a bounded non-regex/non-reference schema subset", trusted_application_code=True)
        if enable_sandbox_tests:
            files = {"type": "array", "minItems": 1, "maxItems": 32, "items": {"type": "object", "properties": {
                "path": {"type": "string", "minLength": 1, "maxLength": 512}, "destination": {"type": "string", "minLength": 1, "maxLength": 200}},
                "required": ["path", "destination"], "additionalProperties": False}}
            self.register("sandbox.python_tests", self._python_tests, arguments_schema={"type": "object", "properties": {
                "files": files, "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 60}}, "required": ["files"], "additionalProperties": False},
                description="Opt-in: copy explicit artifact files into an isolated workspace and run fixed Python -I -S -B standard-library unittest discovery under native Windows Codex MXC. No shell/executable/flags arguments; network off, only workspace writable. Include test_*.py. Paths can use @self/name in author output.", trusted_application_code=True)

    def register(self, name, handler, *, arguments_schema, description, trusted_application_code=False):
        if trusted_application_code is not True:
            raise ToolError("registration requires explicit trusted_application_code=True; callbacks are not sandboxed")
        if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_.]{0,79}", name) or name in self.operations or not callable(handler):
            raise ToolError("operation name must be unique and handler callable")
        if name == "sandbox.python_tests" and (not self.enable_sandbox_tests or handler != self._python_tests):
            raise ToolError("sandbox.python_tests is reserved for the explicit native MXC adapter")
        _safe_schema(arguments_schema)
        self.operations[name] = (handler, arguments_schema, description)

    def catalog(self):
        return [{"kind": name, "arguments_schema": schema, "description": description,
                 "permission": "Codex MXC OS sandbox: root denied, explicit runtime reads, isolated workspace writes, network disabled" if name == "sandbox.python_tests" else "read run/artifacts only; write tool-executions ledger" if name in self.BUILT_INS else "trusted application callback with Python process authority; not sandboxed",
                 "evidence_kind": "local_tool", "built_in": name in self.BUILT_INS}
                for name, (_, schema, description) in self.operations.items()]

    def supports(self, kind):
        return kind in self.operations

    def _python_tests(self, reader, arguments):
        from .sandbox_runtime import command as build, probe, verify_installations, diagnosis, START, FINISH
        directory = reader.execution_directory
        workspace = _reject_links(directory / "workspace")
        workspace.mkdir()
        manifest, destinations, total_bytes = [], set(), 0
        for item in arguments["files"]:
            relative = _relative(item["destination"])
            if any(part.startswith(".") for part in relative.parts) or relative.suffix.lower() not in {".py", ".json", ".txt", ".csv", ".md"}:
                raise ToolError("test inputs allow only non-hidden .py/.json/.txt/.csv/.md artifact destinations")
            key = relative.as_posix().casefold()
            if key in destinations:
                raise ToolError("test destinations must be unique, including case-insensitive aliases")
            destinations.add(key)
            data, original = reader.read(item["path"])
            total_bytes += len(data)
            if total_bytes > min(self.max_input_bytes * 2, 2 * 1024 * 1024):
                raise ToolError("total copied test inputs exceed 2 MiB or configured input allowance")
            target = _reject_links(workspace.joinpath(*relative.parts))
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("xb") as output:
                output.write(data)
            manifest.append({**original, "destination": relative.as_posix()})
        if not any(PurePosixPath(item["destination"]).name.startswith("test_") and item["destination"].endswith(".py") for item in manifest):
            raise ToolError("at least one explicit test_*.py artifact is required")
        expected = getattr(reader, "expected_inputs", None)
        if expected is not None and {x["path"]:x["sha256"] for x in manifest} != expected:
            raise ToolConflict("retry input bytes changed; create a new test request for changed code")
        atomic_json(directory / "sandbox-inputs.json", {"files": manifest, "total_bytes": total_bytes})
        preflight_dir = directory / "preflight"
        preflight_dir.mkdir()
        preflight = probe(workspace, untrusted_root=self.run_dir, log_dir=preflight_dir)
        atomic_json(directory / "sandbox-preflight.json", preflight)
        if not preflight["available"]:
            return _ToolOutcome("failed", {"preflight":preflight, "inputs":manifest,
                "test_code_started":False, "diagnostic":preflight["diagnostic"]}, preflight["reason"])
        command, policy = build(workspace, untrusted_root=self.run_dir)
        # Resolve once per phase, require the successful probe to have used the
        # same interpreter/executor, and pin those physical paths for launch.
        if policy["installations"] != preflight["policy"]["installations"]:
            return _ToolOutcome("failed", {"preflight":preflight, "inputs":manifest,
                "test_code_started":False, "diagnostic":diagnosis("installation_changed", "Installation changed after probe")},
                "Installation changed after probe")
        verify_installations(policy)
        invocation = {"argv": command, "cwd": str(workspace), "policy": policy, "timeout_seconds": arguments.get("timeout_seconds", 30),
                      "evidence_kind": "local_sandbox_execution", "model_call": False, "started_at": _now()}
        atomic_json(directory / "sandbox-invocation.json", invocation)
        execution = _run_bounded(command, cwd=workspace, timeout_seconds=invocation["timeout_seconds"], log_dir=directory)
        counts = re.findall(r"^Ran (\d+) tests? in ", execution["stderr"], re.MULTILINE)
        count = int(counts[-1]) if counts else None
        started = START in execution["stdout"].splitlines()
        summaries = [line[len(FINISH):] for line in execution["stdout"].splitlines() if line.startswith(FINISH)]
        try:
            summary = json.loads(summaries[-1]) if summaries else None
        except (ValueError, TypeError):
            summary = None
        if not isinstance(summary, dict) or type(summary.get("tests")) is not int:
            summary = None
        payload = {**execution, "policy": policy, "preflight":preflight, "inputs": manifest,
                   "test_code_started":started, "reported_test_count": count, "reported_summary":summary,
                   "invocation_path": (directory / "sandbox-invocation.json").relative_to(self.run_dir).as_posix(),
                   "stdout_path": (directory / "stdout.bin").relative_to(self.run_dir).as_posix(),
                   "stderr_path": (directory / "stderr.bin").relative_to(self.run_dir).as_posix(),
                   "boundary": "Actual native sandbox process evidence. Test counts are parsed from untrusted runner output, not independent proof of test quality. No CPU/memory/disk quota is claimed; execution time and captured output are bounded. No external side effects are assumed undone after interruption."}
        if execution["interruption"]:
            payload["diagnostic"] = diagnosis("interrupted", "Sandbox execution interrupted", phase="execution", executed=started)
            return _ToolOutcome("uncertain", payload, "Sandbox execution interrupted: " + execution["interruption"] + "; no automatic retry")
        if not started or summary is None:
            payload["diagnostic"] = diagnosis("runner_incomplete", "No complete runner result; test execution cannot be established",
                phase="execution", executed=started)
            return _ToolOutcome("uncertain", payload, payload["diagnostic"]["message"])
        if count == 0 or summary["tests"] == 0:
            payload["diagnostic"] = diagnosis("no_tests", "No tests discovered", phase="tests", executed=True)
            return _ToolOutcome("failed", payload, "No tests discovered")
        if summary.get("skipped") == summary["tests"]:
            payload["diagnostic"] = diagnosis("all_skipped", "All discovered tests were skipped; execution is not validated", phase="tests", executed=True)
            return _ToolOutcome("failed", payload, "All tests skipped")
        if execution["exit_code"] != 0 or not summary.get("successful"):
            payload["diagnostic"] = diagnosis("test_failure", "Python tests reported failures/errors", phase="tests", executed=True)
            return _ToolOutcome("failed", payload, "Python tests reported failures/errors; inspect stderr")
        if count is None or count != summary["tests"]:
            payload["diagnostic"] = diagnosis("runner_incomplete", "Runner summary/count inconsistent", phase="execution", executed=True)
            return _ToolOutcome("uncertain", payload, "Runner summary/count inconsistent")
        return _ToolOutcome("succeeded", payload)

    def _ledger(self, request_id):
        root = _reject_links(self.run_dir / "tool-executions")
        root.mkdir(exist_ok=True)
        path = root / hashlib.sha256(request_id.encode("utf-8")).hexdigest()
        return _reject_links(path)

    def execute(self, request):
        """Return persisted result, or uncertain without rerunning an existing intent.

        Must be called by the service under its run lock. mkdir is an additional
        cross-process claim; an interrupted claim is conservatively uncertain.
        """
        app = _reject_links(self.run_dir / "app.json")
        app_data = _json(app.read_text(encoding="utf-8")) if app.exists() else {}
        if app_data.get("checkpoint_read_only"):
            raise ToolError("inspection-only restored checkpoints cannot execute tools")
        if not isinstance(request, dict) or not isinstance(request.get("id"), str) or not 1 <= len(request["id"]) <= 200:
            raise ToolError("request requires a nonempty id of at most 200 characters")
        # Semantic state changes are not a new action; only the sealed request
        # fields participate in identity, allowing service replay after import.
        sealed = {key: request[key] for key in ("id", "kind", "tool", "arguments", "topic_id", "source_job", "purpose", "retry_of", "expected_inputs") if key in request}
        if len(canonical(sealed).encode("utf-8")) > 65536:
            raise ToolError("tool request exceeds 64 KiB")
        checksum = digest(sealed)
        directory = self._ledger(request["id"])
        request_path, result_path, receipt_path = (directory / name for name in ("request.json", "result.json", "receipt.json"))
        try:
            directory.mkdir()
        except FileExistsError:
            for path in (request_path, result_path, receipt_path):
                _reject_links(path)
            if request_path.is_file():
                previous = _json(request_path.read_text(encoding="utf-8"))
                if previous.get("request_sha256") != checksum or previous.get("request") != sealed:
                    raise ToolConflict("request id is already bound to a different immutable request")
            if result_path.is_file() and receipt_path.is_file():
                result = _json(result_path.read_text(encoding="utf-8"))
                receipt = _json(receipt_path.read_text(encoding="utf-8"))
                if result.get("request_sha256") != checksum or receipt.get("request_sha256") != checksum:
                    raise ToolConflict("tool result/receipt integrity mismatch")
                if receipt.get("phase") == "completed":
                    if receipt.get("result_sha256") != digest(result):
                        raise ToolConflict("tool result/receipt integrity mismatch")
                    return dict(result, receipt=receipt)
            return {"request_id": request["id"], "request_sha256": checksum, "outcome": "uncertain", "payload": None,
                    "error": "An earlier claim has no complete result and receipt; no automatic re-execution is permitted.",
                    "receipt": {"evidence_kind": "local_tool", "phase": "incomplete_claim", "automatic_retry": False,
                                "request_path": request_path.relative_to(self.run_dir).as_posix(), "recovered_at": _now()}}
        started = _now()
        kind = sealed.get("kind", sealed.get("tool"))
        atomic_json(request_path, {"request": sealed, "request_sha256": checksum, "created_at": started})
        receipt = {"request_id": request["id"], "request_sha256": checksum, "started_at": started,
                   "run_id": app_data.get("id", self.run_dir.name),
                   "evidence_kind": "local_tool", "phase": "started", "automatic_retry": False,
                   "request_path": request_path.relative_to(self.run_dir).as_posix(),
                   "result_path": result_path.relative_to(self.run_dir).as_posix(),
                   "receipt_path": receipt_path.relative_to(self.run_dir).as_posix(),
                   "permissions": {"input_root": "artifacts", "shell": False, "network": False, "arbitrary_code": False},
                   "boundary": "Built-ins are bounded local readers. Trusted registered Python handlers are not sandboxed. Local filesystem ownership is assumed; hostile concurrent filesystem mutation is outside this boundary."}
        if isinstance(kind, str) and kind in self.operations and kind not in self.BUILT_INS:
            receipt["permissions"] = {"authority": "explicitly trusted application Python callback", "sandboxed": False}
        if kind == "sandbox.python_tests" and self.enable_sandbox_tests:
            receipt["permissions"] = {"executor": "Codex native MXC", "network": False, "arbitrary_shell": False,
                                      "code_execution": "explicit copied Python artifacts inside isolated OS sandbox", "unsandboxed_fallback": False}
            receipt["boundary"] = "Opt-in native sandbox execution; fixed unittest argv. Setup failures never fall back. Timeout/output interruption is uncertain and never automatically reissued."
        atomic_json(receipt_path, receipt)
        reader = None
        result = {"request_id": request["id"], "request_sha256": checksum, "outcome": "failed", "payload": None, "error": None}
        try:
            reader = ArtifactReader(self.run_dir / "artifacts", self.max_input_bytes)
            reader.execution_directory = directory
            reader.expected_inputs = sealed.get("expected_inputs")
            if kind not in self.operations:
                raise ToolError("unsupported operation; choose an explicit registered kind")
            handler, schema, _ = self.operations[kind]
            arguments = sealed.get("arguments")
            if isinstance(arguments, str):
                arguments = _json(arguments)
            validate_output(arguments, schema)
            payload = handler(reader, arguments)
            explicit = payload if isinstance(payload, _ToolOutcome) else None
            if explicit:
                payload = explicit.payload
            if len(canonical(payload).encode("utf-8")) > self.max_output_bytes:
                raise ToolError("tool output exceeds max_output_bytes")
            result.update(outcome=explicit.outcome if explicit else "succeeded", payload=payload, error=explicit.error if explicit else None)
        except Exception as exc:
            result["error"] = (type(exc).__name__ + ": " + str(exc))[:2000]
            if kind == "sandbox.python_tests":
                from .sandbox_runtime import diagnosis
                # Once invocation is persisted an exception cannot prove that
                # no child ran. Keep it uncertain, including Popen/log failures.
                launched = (directory / "sandbox-invocation.json").exists()
                result["outcome"] = "uncertain" if launched else "failed"
                result["diagnostic"] = diagnosis(getattr(exc,"code","execution_unknown" if launched else "input_or_setup"),
                    result["error"], phase="execution" if launched else "preflight", executed=None if launched else False,
                    details={"path":getattr(exc,"path",None)})
        if kind == "sandbox.python_tests":
            result.setdefault("diagnostic", (result.get("payload") or {}).get("diagnostic"))
        receipt.update(phase="completed", outcome=result["outcome"], finished_at=_now(), reads=reader.reads if reader else [],
                       result_sha256=digest(result))
        # A crash between these two writes remains uncertain, never success.
        atomic_json(result_path, result)
        atomic_json(receipt_path, receipt)
        return dict(result, receipt=receipt)


def execute_tool(run_dir, request, **limits):
    return LocalToolAdapter(run_dir, **limits).execute(request)
