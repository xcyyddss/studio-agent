"""Unified CLI. Importing the package or status never invokes a model."""
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys

from .service import StudioService, DATA_ROOT


def main(argv=None):
    parser = argparse.ArgumentParser(description="Studio — 本地协作工作台")
    parser.add_argument("--data-root", type=Path, default=DATA_ROOT)
    parser.add_argument("--workers", type=int, default=6)
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create", help="建立普通目标；加 --run 立即运行")
    group = create.add_mutually_exclusive_group(required=True)
    group.add_argument("--goal"); group.add_argument("--goal-file", type=Path)
    create.add_argument("--id"); create.add_argument("--model"); create.add_argument("--run", action="store_true")
    create.add_argument("--profile-file", type=Path, help="显式获准的用户协作初始化seeds JSON")
    create.add_argument("--context-policy", type=Path, help="上下文容量与历史整理策略 JSON")
    create.add_argument("--sandbox-tests", action="store_true", help="显式允许通过Windows MXC隔离环境执行生成的Python单元测试")
    create.add_argument("--council-limits", type=Path, help="议会关注窗口 JSON：abstract_chars、scenario_chars、total_chars；整次运行固定，物理容量另行检查")
    create.add_argument("--web-search", choices=["live", "cached", "disabled"], default="live", help="独立检索调用的原生搜索模式；普通认知调用不直接联网")
    for name in ("status", "run", "pause", "resume"):
        sub.add_parser(name).add_argument("id")
    entry = sub.add_parser("input", help="提交真实补充、回答或纠正")
    entry.add_argument("id"); entry.add_argument("--text", default="")
    entry.add_argument("--file", type=Path, action="append", default=[], help="显式导入附件（最多4个，每个4MB），无需复制到运行目录")
    entry.add_argument("--replaces", default="", help="修改版对应的原FILE ID或产物完整相对路径；仅限单文件")
    entry.add_argument("--submission-id", help="重试同一次上传时复用，防止重复投递")
    entry.add_argument("--question"); entry.add_argument("--correction", action="store_true")
    entry.add_argument("--source", choices=["user", "operator", "fixture"], default="user")
    entry.add_argument("--topic"); entry.add_argument("--profile-scope", choices=["none", "profile", "collaboration"], default="collaboration")
    presented = sub.add_parser("presented", help="在实际展示问题之后登记送达，不代表理解或回答")
    presented.add_argument("id"); presented.add_argument("question_id"); presented.add_argument("--channel", default="operator")
    retry = sub.add_parser("retry", help="显式重试；结果不明时可能重复模型消耗")
    retry.add_argument("id"); retry.add_argument("job_id"); retry.add_argument("--reason", required=True)
    retry.add_argument("--allow-uncertain", action="store_true")
    tool_retry = sub.add_parser("retry-tool", help="保留旧收据，以同版文件新建一次隔离测试；不会重跑模型任务")
    tool_retry.add_argument("id"); tool_retry.add_argument("request_id"); tool_retry.add_argument("--reason", required=True)
    tool_retry.add_argument("--allow-uncertain", action="store_true")
    dismiss = sub.add_parser("dismiss-tool-recovery", help="说明原因并结束执行恢复等待；不视为测试通过")
    dismiss.add_argument("id"); dismiss.add_argument("request_id"); dismiss.add_argument("--reason", required=True)
    sub.add_parser("sandbox-check", help="运行固定探针并保存结果，不运行任务代码或模型").add_argument("id")
    recover = sub.add_parser("recover", help="恢复上下文准备或调度阻塞；可更新上下文策略")
    recover.add_argument("id"); recover.add_argument("--context-policy", type=Path)
    compare = sub.add_parser("compare-scope", help="为超限的待处理比较明确选择真实原件片段，不改写历史")
    compare.add_argument("id"); compare.add_argument("request_id")
    compare.add_argument("--selection-file", type=Path, required=True)
    snapshot = sub.add_parser("export", help="导出可验证检查点；在途调用会明确标记")
    snapshot.add_argument("id"); snapshot.add_argument("destination", type=Path)
    verify = sub.add_parser("verify", help="只读验证检查点摘要、数据库及来源")
    verify.add_argument("snapshot", type=Path)
    restore = sub.add_parser("restore", help="恢复成异名只读检查副本，不派发旧任务")
    restore.add_argument("snapshot", type=Path); restore.add_argument("--id", required=True)
    fork = sub.add_parser("fork", help="将本机静止检查点恢复成异名可运行分支，原件不变")
    fork.add_argument("snapshot", type=Path); fork.add_argument("--id", required=True)
    sub.add_parser("list")
    doctor = sub.add_parser("doctor", help="检查本机CLI版本和登录状态，不调用模型")
    doctor.add_argument("--sandbox", action="store_true", help="额外运行固定print探针检查MXC，不执行生成代码")
    web = sub.add_parser("serve", help="启动本机网页工作台")
    web.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    try:
        from .service import validate_workers
        validate_workers(args.workers)
        if args.command == "serve":
            from .web import serve
            serve(root=args.data_root, port=args.port, max_workers=args.workers)
            return 0
        if args.command == "doctor":
            exe = shutil.which("codex")
            if not exe:
                raise ValueError("Codex CLI 不在 PATH 中，请先安装并登录。")
            outputs = {}
            for label, command in [("version", ["--version"]), ("login", ["login", "status"])]:
                result = subprocess.run([exe, *command], capture_output=True, text=True,
                                        encoding="utf-8", errors="replace", timeout=20,
                                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                outputs[label] = {"exit_code": result.returncode,
                                  "message": result.stdout.strip() or result.stderr.strip()}
            outputs["model_call_performed"] = False
            from .media import poppler
            for module in ('PIL', 'pypdf'):
                try:
                    outputs[module] = {'available': True, 'version': __import__(module).__version__}
                except ImportError:
                    outputs[module] = {'available': False, 'message': '请用当前 Python 执行 pip install .'}
            from .context_budget import tokenizer
            codec, method = tokenizer()
            outputs["context_tokenizer"] = {"available":codec is not None, "method":method,
                "network_used":False, "message":"本地代理分词器并非服务端精确计量。" if codec is not None
                else "已降级为保守的 UTF-8 字节计量；请安装依赖并检查 tokenizer_data 目录。"}
            try:
                outputs['pdf_renderer'] = {'available': True, 'path': poppler()}
            except ValueError as exc:
                outputs['pdf_renderer'] = {'available': False, 'message': str(exc)}
            if args.sandbox:
                from .tool_adapters import sandbox_diagnostics
                outputs["sandbox"] = sandbox_diagnostics(probe=True)
            outputs["note"] = "CLI 登录状态不保证模型服务可用；实际运行保留单独回执。"
            print(json.dumps(outputs, ensure_ascii=False, indent=2))
            return 1 if args.sandbox and not outputs["sandbox"]["available"] else 0
        service = StudioService(args.data_root, max_workers=args.workers)
        if args.command == "create":
            goal = args.goal if args.goal is not None else args.goal_file.read_text(encoding="utf-8")
            seeds = json.loads(args.profile_file.read_text(encoding="utf-8"))["seeds"] if args.profile_file else None
            result = service.create(goal, run_id=args.id, model=args.model, profile_seeds=seeds, sandbox_tests=args.sandbox_tests, web_search=args.web_search,
                context_policy=json.loads(args.context_policy.read_text(encoding="utf-8")) if args.context_policy else None,
                council_limits=json.loads(args.council_limits.read_text(encoding="utf-8")) if args.council_limits else None)
            print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
            if args.run:
                result = service.run(result["id"])
        elif args.command == "list":
            result = service.list_runs()
        elif args.command == "status":
            result = service.status(args.id)
        elif args.command in {"run", "resume"}:
            service.resume(args.id)
            result = service.run(args.id)
        elif args.command == "pause":
            result = service.pause(args.id)
        elif args.command == "input":
            import base64
            from .file_exchange import MAX_FILE
            if len(args.file) > 4 or args.replaces and len(args.file) != 1:
                raise ValueError('最多4个文件；--replaces须配合一个文件')
            attachments = []
            for file in args.file:
                with file.open('rb') as handle:
                    raw = handle.read(MAX_FILE+1)
                if len(raw) > MAX_FILE:
                    raise ValueError('单个附件不得超过4MB')
                attachments.append({'name': file.name, 'data_base64': base64.b64encode(raw).decode(), 'replaces': args.replaces})
            result = service.input(args.id, args.text, question_id=args.question,
                                   correction=args.correction, source=args.source, topic_id=args.topic, profile_scope=args.profile_scope,
                                   attachments=attachments, submission_id=args.submission_id)
        elif args.command == "presented":
            result = service.presented(args.id, args.question_id, channel=args.channel)
        elif args.command == "retry":
            result = service.retry(args.id, args.job_id, reason=args.reason, allow_uncertain=args.allow_uncertain)
        elif args.command == "retry-tool":
            result = service.retry_tool(args.id,args.request_id,reason=args.reason,allow_uncertain=args.allow_uncertain)
        elif args.command == "dismiss-tool-recovery":
            result = service.dismiss_tool_recovery(args.id,args.request_id,reason=args.reason)
        elif args.command == "sandbox-check":
            result = service.check_sandbox(args.id)
        elif args.command == "recover":
            result = service.recover(args.id, context_policy=json.loads(args.context_policy.read_text(encoding="utf-8")) if args.context_policy else None)
        elif args.command == "compare-scope":
            result = service.reframe_comparison(args.id, args.request_id,
                json.loads(args.selection_file.read_text(encoding="utf-8")))
        elif args.command == "export":
            result = service.export(args.id, args.destination)
        elif args.command == "verify":
            from .checkpoints import verify_snapshot
            result = verify_snapshot(args.snapshot)
        elif args.command == "restore":
            result = service.restore(args.snapshot, args.id)
        elif args.command == "fork":
            result = service.fork(args.snapshot, args.id)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1 if isinstance(result, dict) and (result.get("error") or result.get("available") is False) else 0
    except KeyboardInterrupt:
        print("调度器已停止；任务状态与在途调用记录保留。使用 run ID 接续。", file=sys.stderr)
        return 130
    except (ValueError, OSError, KeyError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
