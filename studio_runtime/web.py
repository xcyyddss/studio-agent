"""Loopback-only local UI. No external assets, remote binding or implicit startup."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import secrets
import re
import uuid
from urllib.parse import urlparse, parse_qs, unquote, quote
from pathlib import Path

from .service import StudioService

STATIC = Path(__file__).parent / "static"


def make_server(service, *, port=8765):
    token = secrets.token_urlsafe(32)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            # Request bodies and user text are not printed to access logs.
            pass

        def send(self, content, *, content_type="application/json; charset=utf-8", status=200, filename=None):
            if not isinstance(content, bytes):
                content = json.dumps(content, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(content)))
            if filename is not None:
                self.send_header("Content-Disposition", "attachment; filename=\"download\"; filename*=UTF-8''" + quote(filename, safe=''))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            self.end_headers()
            self.wfile.write(content)

        def trusted_host(self):
            return self.headers.get("Host") in {
                f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}

        def do_GET(self):
            try:
                if not self.trusted_host():
                    return self.send({"error": "Local host required"}, status=403)
                parsed = urlparse(self.path)
                path = parsed.path
                assets = {"/": ("index.html", "text/html; charset=utf-8"),
                          "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                          "/attachments.js": ("attachments.js", "text/javascript; charset=utf-8"),
                          "/sandbox.js": ("sandbox.js", "text/javascript; charset=utf-8"),
                          "/style.css": ("style.css", "text/css; charset=utf-8")}
                if path in assets:
                    name, mime = assets[path]
                    return self.send((STATIC / name).read_bytes(), content_type=mime)
                if path == "/api/session":
                    return self.send({"token": token, "default_workers":service.max_workers})
                if path == "/api/runs":
                    # Do not expose provider config through the run picker.
                    return self.send({"runs": [{**{k: r[k] for k in ("id", "goal", "created_at")}, "checkpoint_read_only": bool(r.get("checkpoint_read_only"))}
                                               for r in service.list_runs()]})
                parts = path.strip("/").split("/")
                if len(parts) >= 3 and parts[:2] == ["api", "runs"]:
                    run_id = unquote(parts[2])
                    if len(parts) == 3:
                        return self.send(service.status(run_id))
                    if len(parts) == 5 and parts[3] == "file":
                        download = parse_qs(parsed.query).get('download', ['0'])[0] == '1'
                        page = parse_qs(parsed.query).get('page', [None])[0]
                        saved = service.saved_file(run_id, unquote(parts[4]), preview=not download, page=int(page) if page is not None and not download else None)
                        return self.send(saved['content'], content_type='application/octet-stream' if download else saved.get('content_type', 'text/plain; charset=utf-8'),
                                         filename=saved['name'] if download else None)
                    if len(parts) == 4 and parts[3] == "artifact":
                        name = parse_qs(parsed.query).get("path", [""])[0]
                        artifact = service.artifact(run_id, name)
                        # Model-authored HTML must never run with application origin.
                        download = parse_qs(parsed.query).get('download', ['0'])[0] == '1'
                        return self.send(artifact.read_bytes(), content_type="application/octet-stream" if download else "text/plain; charset=utf-8",
                                         filename=artifact.name if download else None)
                self.send({"error": "Not found"}, status=404)
            except (ValueError, OSError, KeyError) as exc:
                self.send({"error": str(exc)}, status=400)

        def do_POST(self):
            try:
                if not self.trusted_host() or self.headers.get("X-Studio-Token") != token:
                    return self.send({"error": "Local session token required"}, status=403)
                origin = self.headers.get("Origin")
                if origin and origin not in {
                    f"http://127.0.0.1:{self.server.server_port}", f"http://localhost:{self.server.server_port}"}:
                    return self.send({"error": "Origin not allowed"}, status=403)
                if self.headers.get_content_type() != "application/json":
                    return self.send({"error": "JSON required"}, status=415)
                length = int(self.headers.get("Content-Length", "0"))
                limit = 24_000_000 if urlparse(self.path).path.endswith('/input') else 1048576
                if not 0 < length <= limit:
                    return self.send({"error": "Invalid body length"}, status=413)
                data = json.loads(self.rfile.read(length))
                if not isinstance(data, dict):
                    raise ValueError("JSON object required")
                path = urlparse(self.path).path
                if path == "/api/runs":
                    result = service.create(data["goal"], web_search=data.get("web_search", "live"), max_workers=data.get("max_workers"), **({"sandbox_tests": True} if data.get("sandbox_tests") is True else {}))
                    if data.get("start", True):
                        service.start(result["id"])
                    return self.send({"id": result["id"]}, status=201)
                parts = path.strip("/").split("/")
                if len(parts) != 4 or parts[:2] != ["api", "runs"]:
                    return self.send({"error": "Not found"}, status=404)
                run_id, action = unquote(parts[2]), parts[3]
                if action == "start":
                    result = service.start(run_id)
                elif action == "scheduling":
                    result = service.configure_scheduling(run_id, data["max_workers"])
                elif action == "pause":
                    result = service.pause(run_id)
                elif action == "input":
                    extra = {k: data[k] for k in ('attachments', 'submission_id', 'source') if k in data}
                    result = service.input(run_id, data.get("text", ""), question_id=data.get("question_id"),
                                           correction=bool(data.get("correction", False)), topic_id=data.get("topic_id"),
                                           profile_scope=data.get("profile_scope", "collaboration"), **extra)
                    service.start(run_id)
                elif action == "presented":
                    result = service.presented(run_id, data["question_id"], channel="local_ui_visible")
                elif action == "export":
                    snapshot_id = run_id + "-" + uuid.uuid4().hex[:8]
                    destination = service.root / "checkpoints" / snapshot_id
                    result = {"snapshot_id": snapshot_id, "result": service.export(run_id, destination)}
                elif action == "fork":
                    snapshot_id = data["snapshot_id"]
                    if not isinstance(snapshot_id, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,90}", snapshot_id):
                        raise ValueError("invalid snapshot identity")
                    result = service.fork(service.root / "checkpoints" / snapshot_id, data["id"])
                elif action == "retry":
                    result = service.retry(run_id, data["job_id"], reason=data["reason"],
                                           allow_uncertain=data.get("allow_uncertain") is True)
                    service.start(run_id)
                elif action == "dismiss-tool-recovery":
                    result = service.dismiss_tool_recovery(run_id, data["request_id"], reason=data["reason"])
                    service.start(run_id)
                elif action == "sandbox-check":
                    result = service.check_sandbox(run_id)
                elif action == "retry-tool":
                    result = service.retry_tool(run_id, data["request_id"], reason=data["reason"],
                                                allow_uncertain=data.get("allow_uncertain") is True)
                    service.start(run_id)
                elif action == "recover":
                    result = service.recover(run_id)
                    service.start(run_id)
                else:
                    return self.send({"error": "Unknown action"}, status=404)
                self.send(result)
            except (ValueError, OSError, KeyError) as exc:
                self.send({"error": str(exc)}, status=400)

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def serve(*, root=None, port=8765, max_workers=6):
    service = StudioService(max_workers=max_workers, **({"root": root} if root else {}))
    server = make_server(service, port=port)
    print(f"Studio: http://127.0.0.1:{server.server_port}", flush=True)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        service.close()
        server.server_close()
