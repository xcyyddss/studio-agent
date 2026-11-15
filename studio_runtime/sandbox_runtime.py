"""Fixed native test runner. Installation aliases are not task file capabilities.

Only the host-selected Codex and current Python installation may resolve links.
Task paths retain strict no-link checks. No fallback, install or elevation occurs.
"""
from pathlib import Path
import hashlib
import json
import os
import shutil
import sys


START = "STUDIO_UNITTEST_STARTED_V1"
PROBE = "STUDIO_MXC_PYTHON_PROBE_OK"
FINISH = "STUDIO_UNITTEST_RESULT_V1:"
# Standard-library discovery is fixed by application code, not model arguments.
# Counts/markers describe runner output; test code is untrusted and can spoof it.
RUNNER = '''import json, sys, unittest
print("STUDIO_UNITTEST_STARTED_V1", flush=True)
suite = unittest.defaultTestLoader.discover(".", pattern="test_*.py", top_level_dir=".")
result = unittest.TextTestRunner(verbosity=2).run(suite)
print("STUDIO_UNITTEST_RESULT_V1:" + json.dumps({"tests": result.testsRun,
 "failures": len(result.failures), "errors": len(result.errors),
 "skipped": len(result.skipped), "successful": result.wasSuccessful()}), flush=True)
sys.exit(0 if result.wasSuccessful() and result.testsRun > 0 else 1)
'''


class SandboxSetupError(ValueError):
    def __init__(self, code, message, *, path=None):
        super().__init__(message)
        self.code, self.path = code, str(path) if path else None


def _identity(path):
    stat = path.stat()
    return {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns, "inode": stat.st_ino}


def _installation(path, *, name, untrusted_root=None, directory=False):
    from .tool_adapters import _reject_links
    original = Path(path).absolute()
    try:
        resolved = original.resolve(strict=True)
        _reject_links(resolved)
        if not (resolved.is_dir() if directory else resolved.is_file()):
            raise ValueError("not an ordinary " + ("directory" if directory else "file"))
        if not directory and resolved.suffix.lower() != ".exe":
            raise ValueError("native .exe required; command scripts are not accepted")
        if untrusted_root and resolved.is_relative_to(Path(untrusted_root).resolve()):
            raise ValueError("installation must not be supplied by run contents")
    except (OSError, ValueError, RuntimeError) as exc:
        raise SandboxSetupError("installation_path", f"{name} installation is unusable: {exc}", path=original) from exc
    return {"requested": str(original), "resolved": str(resolved),
            "alias_resolved": str(original).casefold() != str(resolved).casefold(),
            "identity": _identity(resolved)}


def command(workspace, *, probe=False, untrusted_root=None):
    from .tool_adapters import _reject_links
    if os.name != "nt":
        raise SandboxSetupError("unsupported_host", "Python tests require native Windows Codex MXC; no fallback is enabled")
    executable = shutil.which("codex.exe")
    if not executable:
        raise SandboxSetupError("codex_missing", "Codex executable is not on the service process PATH")
    workspace = _reject_links(workspace)
    installations = {
        "codex": _installation(executable, name="Codex", untrusted_root=untrusted_root),
        "python": _installation(sys.executable, name="Python", untrusted_root=untrusted_root),
        "base_runtime": _installation(sys.base_prefix, name="Python runtime", untrusted_root=untrusted_root, directory=True),
        "runtime": _installation(sys.prefix, name="Python environment", untrusted_root=untrusted_root, directory=True)}
    runtime_roots = sorted({installations[k]["resolved"] for k in ("base_runtime", "runtime")})
    if not any(Path(installations["python"]["resolved"]).is_relative_to(Path(root)) for root in runtime_roots):
        raise SandboxSetupError("python_runtime", "Python executable is outside its interpreter runtime roots")
    quote = lambda value: json.dumps(value, ensure_ascii=False)
    # A unique profile prevents an unrelated user profile of the same name from
    # adding path grants. Managed requirements still apply and can reject it.
    profile = "studio_tests_" + hashlib.sha256(str(workspace).encode()).hexdigest()[:20]
    fs = {":root": "deny", ":minimal": "read", **{Path(r).as_posix(): "read" for r in runtime_roots},
          workspace.as_posix(): "write"}
    config = 'permissions.' + profile + '={filesystem={' + ','.join(quote(k)+'='+quote(v) for k,v in fs.items()) + '},network={enabled=false}}'
    argv = [installations["codex"]["resolved"], "-c", 'windows.sandbox="mxc"', "-c", config,
            "sandbox", "--include-managed-config", "--permission-profile", profile, "-C", str(workspace),
            "--", installations["python"]["resolved"], "-I", "-S", "-B", "-c",
            'import unittest; print("'+PROBE+'", flush=True)' if probe else RUNNER]
    policy = {"implementation": "codex_native_mxc", "file_system_root": "deny", "network": False,
              "readable_runtime": runtime_roots, "minimal_platform_runtime": "read", "workspace": str(workspace),
              "workspace_access": "write", "fallback": "none", "python_isolated": True,
              "automatic_site_loading": False, "environment": "explicit non-secret name allowlist",
              "installations": installations}
    return argv, policy


def verify_installations(policy):
    """Recheck the pinned physical paths, not a possibly updated alias."""
    from .tool_adapters import _reject_links
    for item in policy["installations"].values():
        path = _reject_links(item["resolved"])
        if _identity(path) != item["identity"]:
            raise SandboxSetupError("installation_changed", "Installation changed during preparation; recheck before retry", path=path)


def diagnosis(code, message, *, phase="preflight", executed=False, details=None):
    hints = {
        "mxc_unavailable": "原生 MXC 不可用或处于嵌套沙箱；在获准宿主上检查 Windows/MXC 支持，不自动切换为无隔离执行。",
        "policy_rejected": "检查宿主的受管理权限约束；不得通过关闭隔离绕过。",
        "codex_missing": "将 Codex 原生可执行文件安装入口加入启动 Studio 的 PATH 后重新检查。",
        "installation_path": "检查显示的宿主安装路径；任务文件仍不能使用联接或符号链接。",
        "installation_changed": "工具安装在检查期间变化；重新检查并创建新的执行记录。",
        "test_failure": "Python 已执行；查看 stderr 中的失败/导入错误，修改代码或测试后发起新请求。",
        "no_tests": "执行器没有发现测试；检查 test_*.py、TestCase 和目录组织。",
        "interrupted": "执行结果不明；先检查原始记录，再显式决定是否允许重跑。",
        "startup_failed": "固定探针未通过；查看原始 stderr、退出码及实际安装路径。",
    }
    return {"code": code, "phase": phase, "test_code_started": executed,
            "message": message, "recovery": hints.get(code, "检查保留的诊断后以新请求恢复，原结果保留。"),
            "automatic_retry": False, **({"details": details} if details else {})}


def probe(workspace, *, untrusted_root=None, log_dir=None):
    from .tool_adapters import _run_bounded
    status = {"adapter": "sandbox.python_tests", "implementation": "codex_native_mxc", "probe_performed": True,
              "available": False, "test_code_started": False}
    try:
        argv, policy = command(workspace, probe=True, untrusted_root=untrusted_root)
        status.update(policy=policy, executable=argv[0], python=policy["installations"]["python"]["resolved"])
        verify_installations(policy)
        run = _run_bounded(argv, cwd=workspace, timeout_seconds=10, log_dir=log_dir)
        status["probe"] = run
        ok = run["exit_code"] == 0 and not run["interruption"] and PROBE in run["stdout"]
        status["available"] = ok
        if not ok:
            stderr = run["stderr"].lower()
            code = ("mxc_unavailable" if "mxc is unavailable" in stderr or "mxc capabilities" in stderr else
                    "policy_rejected" if any(t in stderr for t in ("requirements", "not allowed", "permission profile")) else "startup_failed")
            status["diagnostic"] = diagnosis(code, "Native MXC fixed probe failed", details=run)
        status["reason"] = "Native MXC Python/unittest probe succeeded" if ok else status["diagnostic"]["recovery"]
    except (OSError, ValueError, RuntimeError) as exc:
        status.update(reason=str(exc), diagnostic=diagnosis(getattr(exc,"code","startup_failed"),str(exc),
            details={"path":getattr(exc,"path",None)}))
    return status
