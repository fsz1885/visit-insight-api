from __future__ import annotations

import argparse
import json
import sys
import threading
import webbrowser
from pathlib import Path
from .config import Settings, RUNS, ROOT
from .jobs import create_run
from .llm import atomic_json
from .models import Meta
from .pipeline import run_pipeline, now


def main():
    parser = argparse.ArgumentParser(description="接访文本 → Kimi Code / DeepSeek → HTML 客户洞察")
    sub = parser.add_subparsers(dest="command")
    serve = sub.add_parser("serve", help="启动本地上传页面")
    serve.add_argument("--port", type=int, default=8767)
    serve.add_argument("--host", default="0.0.0.0", help="监听地址，默认允许内网访问")
    serve.add_argument("--no-browser", action="store_true")
    run = sub.add_parser("run", help="处理一份转写文本")
    run.add_argument("input", type=Path)
    run.add_argument("--client", default="本次接访客户")
    run.add_argument("--project", default="")
    run.add_argument("--advisor", default="")
    run.add_argument("--date", default="")
    resume = sub.add_parser("resume", help="从已完成节点继续")
    resume.add_argument("run_id")
    sub.add_parser("check", help="检查模型配置（不显示密钥）")
    sub.add_parser("schemas", help="导出模型节点 JSON Schema")
    args = parser.parse_args()
    if args.command == "schemas":
        from .models import Extraction, Analysis, Plan, Draft, Audit, CustomerAnalysis, VisitAnalysis, DecisionAnalysis
        for model in (Extraction, Analysis, Plan, Draft, Audit, CustomerAnalysis, VisitAnalysis, DecisionAnalysis):
            atomic_json(ROOT / "schemas" / f"{model.__name__.lower()}.json", model.model_json_schema())
        print("schemas 已生成")
        return
    settings = Settings.load()
    if args.command == "check":
        print(json.dumps(settings.public(), ensure_ascii=False, indent=2))
        return
    if args.command in {None, "serve"}:
        import uvicorn
        from .server import create_app
        port = getattr(args, "port", 8767)
        if not getattr(args, "no_browser", False):
            timer = threading.Timer(1.5, lambda: webbrowser.open(f"http://127.0.0.1:{port}"))
            timer.daemon = True
            timer.start()
        host = getattr(args, "host", "0.0.0.0")
        uvicorn.run(create_app(settings=settings, allow_remote=host not in {"127.0.0.1", "localhost", "::1"}), host=host, port=port, log_level="warning", proxy_headers=False)
        return
    if not settings.api_key:
        parser.error(f"尚未配置 {settings.provider_name} 密钥，请配置 model.json 或 .env")
    if args.command == "run":
        meta = Meta(client_alias=args.client, project=args.project, advisor=args.advisor, visit_date=args.date)
        folder = create_run(args.input.read_bytes(), args.input.name, meta)
    else:
        import re
        if not re.fullmatch(r"[a-f0-9]{16}", args.run_id):
            parser.error("任务 ID 无效")
        folder = RUNS / args.run_id
        if not (folder / "input.json").exists():
            parser.error("任务不存在")
    print(f"任务：{folder.name}", flush=True)
    state = json.loads((folder / "status.json").read_text(encoding="utf-8"))
    state.update(status="running", message="正在运行")

    def event(kind, msg, progress=None):
        print(f"[{kind}] {msg}", flush=True)
        state.update(message=msg, updated_at=now())
        if progress is not None:
            state["progress"] = progress
        state["events"] = (state.get("events", []) + [{"at": now(), "node": kind, "message": msg}])[-120:]
        atomic_json(folder / "status.json", state)

    try:
        report = run_pipeline(folder, settings, event)
        state.update(status=report["quality"]["report_status"], progress=100)
        print(f"报告：{folder / 'report.html'}", flush=True)
    except KeyboardInterrupt:
        state.update(status="interrupted", message="已停止，可继续运行")
        raise SystemExit(130)
    except Exception as exc:
        message = str(exc).replace(settings.api_key, "[redacted]")
        state.update(status="failed", message=message)
        print(message, file=sys.stderr)
        raise SystemExit(1)
    finally:
        state["updated_at"] = now()
        atomic_json(folder / "status.json", state)


if __name__ == "__main__":
    main()
