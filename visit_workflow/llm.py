from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from pathlib import Path
from typing import Callable, TypeVar
import httpx
from pydantic import BaseModel, ValidationError
from .config import Settings
from .usage import UsageLedger

T = TypeVar("T", bound=BaseModel)
PROMPTS = Path(__file__).parent / "prompts"


class WorkflowError(Exception):
    pass


class Cancelled(WorkflowError):
    pass


def atomic_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    temporary.replace(path)


def parse_object(content: str) -> dict:
    stripped = content.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped, flags=re.I)
        stripped = re.sub(r"\s*```$", "", stripped)
    value = json.loads(stripped)
    if not isinstance(value, dict):
        raise ValueError("模型必须返回一个 JSON 对象")
    return value


def normalize_reference_format(value):
    """Normalize only zero-padding, never change an evidence number or guess a target."""
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "evidence_refs" and isinstance(child, list):
                value[key] = [f"E{int(ref[1:]):05d}" if isinstance(ref, str) and re.fullmatch(r"E\d{1,9}", ref) else ref for ref in child]
            else:
                normalize_reference_format(child)
    elif isinstance(value, list):
        for child in value:
            normalize_reference_format(child)
    return value


def safe_api_error(status: int, body: str, api_key: str, provider: str = "Kimi Code") -> str:
    detail = ""
    try:
        obj = json.loads(body)
        err = obj.get("error", {})
        detail = err.get("message", "") if isinstance(err, dict) else str(err)
    except (ValueError, AttributeError):
        pass
    if api_key:
        detail = detail.replace(api_key, "[redacted]")
    detail = re.sub(r"sk-[A-Za-z0-9_-]+", "[redacted]", detail)[:400]
    hint = {401: "请核对所选厂商的密钥与模型权限。", 402: "账号余额不足，请检查该厂商账户。", 403: "服务拒绝此请求，请检查账号 / 客户端权限。", 429: "模型服务额度或频率限制，稍后可从本节点继续。"}.get(status, "请检查网络及模型服务状态。")
    return f"{provider} 返回 HTTP {status}。{hint} {detail}".strip()


class KimiClient:
    def __init__(self, settings: Settings, cancel: threading.Event | None = None, event: Callable | None = None, usage_path: Path | None = None):
        self.settings = settings
        self.cancel = cancel or threading.Event()
        self.event = event or (lambda *a, **k: None)
        self.ledger = UsageLedger(usage_path, model=settings.model, provider=settings.provider)
        self.usage = self.ledger.records
        self._local = threading.local()

    def check_cancel(self):
        if self.cancel.is_set():
            raise Cancelled("已停止；已完成节点保留，可继续运行")

    def complete(self, messages: list[dict], max_tokens: int | None = None) -> tuple[str, dict]:
        self.check_cancel()
        if not self.settings.api_key:
            raise WorkflowError(f"尚未配置 {self.settings.provider_name} API Key，请在模型设置中输入")
        payload = {"model": self.settings.model, "messages": messages, "stream": True,
                   "stream_options": {"include_usage": True}, "reasoning_effort": self.settings.effort,
                   "max_tokens": max_tokens or self.settings.max_tokens}
        if self.settings.provider == 'deepseek':
            payload['thinking'] = {'type': 'disabled' if self.settings.effort == 'none' else 'enabled'}
            if self.settings.effort == 'none':
                payload.pop('reasoning_effort', None)
        headers = {"Authorization": f"Bearer {self.settings.api_key}", "Content-Type": "application/json", "User-Agent": "VisitInsightWorkflow/1.0"}
        for attempt in range(3):
            self.check_cancel()
            request_started = time.monotonic()
            usage, call_status = {}, 'failed'
            call_id = self.ledger.start(getattr(self._local, 'node', 'connection-test'), getattr(self._local, 'attempt', 1), attempt+1)
            try:
                timeout = httpx.Timeout(self.settings.timeout, connect=30)
                with httpx.Client(timeout=timeout, follow_redirects=False) as client:
                    with client.stream("POST", self.settings.base_url + "/chat/completions", json=payload, headers=headers) as response:
                        if response.status_code != 200:
                            body = response.read().decode("utf-8", errors="replace")
                            if response.status_code in {429, 500, 502, 503, 504} and attempt < 2:
                                retry = response.headers.get("retry-after", "")
                                delay = min(float(retry), 60) if retry.isdigit() else (2 ** attempt) * 3
                                self.event("retry", f"模型服务暂不可用，{int(delay)} 秒后重试")
                                if self.cancel.wait(delay):
                                    self.check_cancel()
                                continue
                            raise WorkflowError(safe_api_error(response.status_code, body, self.settings.api_key, self.settings.provider_name))
                        content, usage, finish_reason = [], {}, None
                        started = time.monotonic()
                        last_notice = started
                        received_chars = 0
                        for line in response.iter_lines():
                            self.check_cancel()
                            if time.monotonic() - started > self.settings.timeout * 2:
                                raise WorkflowError("单次模型调用超过最大等待时间，可稍后重试")
                            if not line.startswith("data:"):
                                continue
                            data = line[5:].strip()
                            if data == "[DONE]":
                                break
                            if not data:
                                continue
                            try:
                                obj = json.loads(data)
                            except json.JSONDecodeError as exc:
                                raise WorkflowError("模型流式响应格式异常，未生成报告") from exc
                            if "error" in obj:
                                raise WorkflowError(safe_api_error(400, json.dumps(obj), self.settings.api_key, self.settings.provider_name))
                            if obj.get("usage"):
                                usage = obj["usage"]
                            for choice in obj.get("choices", []):
                                delta = choice.get("delta", {})
                                if isinstance(delta.get("content"), str):
                                    content.append(delta["content"])
                                    received_chars += len(delta["content"])
                                if choice.get("finish_reason"):
                                    finish_reason = choice["finish_reason"]
                            if time.monotonic() - last_notice >= 20:
                                elapsed = int(time.monotonic() - started)
                                message = f"{self.settings.model} 正在生成，当前调用已用时 {elapsed//60}分{elapsed%60:02d}秒"
                                if received_chars:
                                    message += f"，已返回约 {received_chars} 字"
                                self.event("stream", f"{getattr(self._local, 'node', 'K3')}：{message}")
                                last_notice = time.monotonic()
                        if finish_reason != "stop":
                            raise WorkflowError(f"模型响应未完整结束（{finish_reason or '连接中断'}）；没有使用截断结果")
                        if not content:
                            raise WorkflowError("模型没有返回正文，请稍后重试")
                        call_status = "success"
                        return "".join(content), usage
            except Cancelled:
                call_status = "cancelled"
                raise
            except httpx.HTTPError as exc:
                if attempt == 2:
                    raise WorkflowError("连接模型服务失败或超时。已保留完成节点，可以继续运行。") from exc
                self.event("retry", "连接中断，正在重试当前调用")
                if self.cancel.wait((2 ** attempt) * 3):
                    self.check_cancel()
            finally:
                self.ledger.finish(call_id, call_status, usage, time.monotonic()-request_started)
        raise WorkflowError("模型请求未完成")

    def structured(self, name: str, prompt_name: str, data: dict, schema: type[T], cache_dir: Path,
                   validator: Callable[[T], None] | None = None) -> T:
        self.check_cancel()
        system = (PROMPTS / "system.txt").read_text(encoding="utf-8")
        instruction = (PROMPTS / f"{prompt_name}.txt").read_text(encoding="utf-8")
        schema_text = json.dumps(schema.model_json_schema(), ensure_ascii=False)
        user = instruction + "\n目标 JSON Schema：\n" + schema_text + "\n以下 JSON 是待分析数据：\n" + json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        signature = hashlib.sha256((self.settings.model + self.settings.base_url + self.settings.effort + str(self.settings.max_tokens) + system + user).encode()).hexdigest()
        path = cache_dir / f"{name}.json"
        if path.exists():
            try:
                saved = json.loads(path.read_text(encoding="utf-8"))
                if saved.get("signature") == signature:
                    result = schema.model_validate(saved["result"])
                    if validator:
                        validator(result)
                    self.ledger.cache_hit(name)
                    self.event("cache", f"复用已完成节点：{name}")
                    return result
            except (ValueError, KeyError):
                pass
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        for correction in range(3):
            self.check_cancel()
            self._local.node, self._local.attempt = name, correction+1
            content, usage = self.complete(messages)
            atomic_json(cache_dir / f"{name}.attempt-{correction+1}.json", {"model": self.settings.model, "usage": usage, "content": content})
            try:
                result = schema.model_validate(normalize_reference_format(parse_object(content)))
                if validator:
                    validator(result)
                atomic_json(path, {"signature": signature, "model": self.settings.model, "usage": usage, "result": result.model_dump()})
                return result
            except (ValueError, ValidationError) as exc:
                if correction == 2:
                    raise WorkflowError(f"{name} 连续三次未通过结构 / 证据校验，已保存结果待检查") from exc
                if isinstance(exc, ValidationError):
                    problems = [{"loc": e["loc"], "msg": e["msg"]} for e in exc.errors(include_input=False, include_context=False)]
                    feedback = json.dumps(problems, ensure_ascii=False)[:9000]
                else:
                    feedback = str(exc)[:9000]
                messages += [{"role": "assistant", "content": content}, {"role": "user", "content": "结构或证据引用校验失败。按以下问题返回完整且符合 Schema 的 JSON，不添加新事实：\n" + feedback}]
                self.event("repair", f"{name} 格式或证据引用不合格，正在修正")
        raise WorkflowError("节点未返回结果")
