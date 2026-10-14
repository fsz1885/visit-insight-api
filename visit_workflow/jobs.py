from __future__ import annotations

import json
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from .config import RUNS, Settings
from .ingest import read_file, resolve_visit_date
from .llm import atomic_json, Cancelled
from .models import Meta
from .pipeline import now, run_pipeline
from .usage import read_records, summarize, UsageLedger


def create_run(data: bytes, filename: str, meta: Meta, root: Path = RUNS) -> Path:
    read_file(data, filename)
    meta = resolve_visit_date(meta, filename)
    run_id = uuid.uuid4().hex[:16]
    folder = root / run_id
    (folder / "source").mkdir(parents=True)
    stored = "transcript" + Path(filename).suffix.lower()
    (folder / "source" / stored).write_bytes(data)
    atomic_json(folder / "input.json", {"filename": Path(filename.replace("\\", "/")).name, "stored_filename": stored, "meta": meta.model_dump()})
    atomic_json(folder / "status.json", {"id": run_id, "filename": filename, "client": meta.client_alias,
                "status": "ready", "progress": 0, "message": "等待运行", "created_at": now(), "updated_at": now(), "events": []})
    return folder


class JobManager:
    def __init__(self, root: Path = RUNS):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="visit-workflow")
        self.active: dict[str, threading.Event] = {}
        for folder in root.iterdir():
            path = folder / "status.json"
            if path.is_file():
                try:
                    state = json.loads(path.read_text(encoding="utf-8"))
                    if state["status"] in {"running", "queued", "stopping"}:
                        state.update(status="interrupted", message="上次服务已停止，可以继续运行", updated_at=now())
                        atomic_json(path, state)
                        UsageLedger(folder / 'usage.json')
                except (ValueError, KeyError):
                    continue

    def folder(self, run_id: str) -> Path:
        import re
        if not re.fullmatch(r"[a-f0-9]{16}", run_id) or not (self.root / run_id / "status.json").is_file():
            raise FileNotFoundError("任务不存在")
        return self.root / run_id

    def get(self, run_id: str) -> dict:
        with self.lock:
            return json.loads((self.folder(run_id) / "status.json").read_text(encoding="utf-8"))

    def list(self) -> list[dict]:
        result = []
        for folder in self.root.iterdir():
            try:
                item = self.get(folder.name)
                item['usage'] = summarize(read_records(folder / 'usage.json'))
                result.append({k: v for k, v in item.items() if k != "events"})
            except (FileNotFoundError, ValueError):
                continue
        return sorted(result, key=lambda r: r.get("created_at", ""), reverse=True)

    def update(self, run_id: str, **fields):
        with self.lock:
            state = self.get(run_id)
            state.update(fields, updated_at=now())
            atomic_json(self.folder(run_id) / "status.json", state)

    def start(self, run_id: str, settings: Settings):
        with self.lock:
            if run_id in self.active:
                raise ValueError("任务已经在队列或运行中")
            cancel = threading.Event()
            self.active[run_id] = cancel
            self.update(run_id, status="queued", message="已排队，前一个任务结束后开始", error=None)
            self.pool.submit(self._worker, run_id, settings, cancel)

    def stop(self, run_id: str):
        with self.lock:
            if run_id not in self.active:
                raise ValueError("任务当前没有运行")
            self.active[run_id].set()
            self.update(run_id, status="stopping", message="正在停止，保留已完成节点")

    def _worker(self, run_id: str, settings: Settings, cancel: threading.Event):
        try:
            if cancel.is_set():
                raise Cancelled("任务已停止")
            self.update(run_id, status="running", message="正在启动工作流")

            def event(kind, message, progress=None):
                with self.lock:
                    state = self.get(run_id)
                    state["events"] = (state.get("events", []) + [{"at": now(), "node": kind, "message": message}])[-120:]
                    state["message"] = message
                    if progress is not None:
                        state["progress"] = progress
                    state["updated_at"] = now()
                    atomic_json(self.folder(run_id) / "status.json", state)

            report = run_pipeline(self.folder(run_id), settings, event, cancel)
            self.update(run_id, status=report["quality"]["report_status"], progress=100,
                        message="看板已生成" if report["quality"]["report_status"] == "complete" else "看板已生成，存在待复核事项")
        except Cancelled:
            self.update(run_id, status="cancelled", message="已停止，可继续运行")
        except Exception as exc:
            message = str(exc)
            if settings.api_key:
                message = message.replace(settings.api_key, "[redacted]")
            self.update(run_id, status="failed", message=message[:700], error=type(exc).__name__)
        finally:
            with self.lock:
                self.active.pop(run_id, None)

    def close(self):
        with self.lock:
            for cancel in self.active.values():
                cancel.set()
        self.pool.shutdown(wait=False, cancel_futures=True)
