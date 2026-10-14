"""Durable, thread-safe request ledger; provider usage is never estimated."""
from __future__ import annotations
import json
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    temp.replace(path)


def read_records(path):
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else []


def number(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def summarize(records):
    def group(rows):
        calls = [r for r in rows if r.get('status') != 'cache_hit']
        result = dict(requests=len(calls), cache_hits=len(rows)-len(calls), running_requests=0,
                      unknown_usage_requests=0, legacy_requests=0, failed_requests=0,
                      input_tokens=0, output_tokens=0, total_tokens=0, cached_input_tokens=0,
                      reasoning_tokens=0, measured_requests=0, input_reported_requests=0,
                      output_reported_requests=0, duration_seconds=0)
        for r in calls:
            u = r.get('usage') or {}
            inp, out, total = (number(u.get(k)) for k in ('prompt_tokens','completion_tokens','total_tokens'))
            if total is None and inp is not None and out is not None:
                total = inp + out
            if total is None or inp is None or out is None:
                result['unknown_usage_requests'] += 1
            if total is not None:
                result['measured_requests'] += 1
                result['total_tokens'] += total
            result['input_tokens'] += inp or 0
            result['output_tokens'] += out or 0
            result['input_reported_requests'] += inp is not None
            result['output_reported_requests'] += out is not None
            result['cached_input_tokens'] += number((u.get('prompt_tokens_details') or {}).get('cached_tokens')) or number(u.get('cached_tokens')) or number(u.get('prompt_cache_hit_tokens')) or 0
            result['reasoning_tokens'] += number((u.get('completion_tokens_details') or {}).get('reasoning_tokens')) or 0
            result['running_requests'] += r.get('status') == 'in_progress'
            result['failed_requests'] += r.get('status') in {'failed','cancelled','interrupted'}
            result['legacy_requests'] += 'call_id' not in r
            result['duration_seconds'] += r.get('duration_seconds') or 0
        result['duration_seconds'] = round(result['duration_seconds'], 2)
        result['partial'] = result['unknown_usage_requests'] > 0
        return result
    nodes = {}
    for row in records:
        nodes.setdefault(row.get('node','unknown'), []).append(row)
    return {**group(records), 'nodes':[{'node':name, **group(rows)} for name,rows in nodes.items()],
            'basis':'接口实际返回的 usage；缓存输入属于输入、推理属于输出，不重复相加。未知用量不估算，历史记录可能未覆盖失败请求。'}


class UsageLedger:
    def __init__(self, path: Path | None = None, model: str = "k3", provider: str = "kimi"):
        self.model = model
        self.provider = provider
        self.path = path
        self.lock = threading.RLock()
        self.records = read_records(path) if path else []
        for row in self.records:
            if row.get('status') == 'in_progress':
                row.update(status='interrupted', finished_at=timestamp())
        self.flush()

    def flush(self):
        with self.lock:
            if self.path:
                write_json(self.path, self.records)
                write_json(self.path.with_name('usage-summary.json'), summarize(self.records))

    def start(self, node, attempt, transport_attempt):
        row = dict(call_id=uuid.uuid4().hex, node=node, attempt=attempt,
                   transport_attempt=transport_attempt, provider=self.provider, model=self.model, status='in_progress',
                   started_at=timestamp(), usage={})
        with self.lock:
            self.records.append(row)
            self.flush()
        return row['call_id']

    def finish(self, call_id, status, usage, elapsed):
        with self.lock:
            row = next(r for r in self.records if r.get('call_id') == call_id)
            row.update(status=status, usage=usage, finished_at=timestamp(), duration_seconds=round(elapsed, 3))
            self.flush()

    def cache_hit(self, node):
        with self.lock:
            self.records.append(dict(call_id=uuid.uuid4().hex,node=node,provider=self.provider,model=self.model,status='cache_hit',started_at=timestamp(),usage={}))
            self.flush()

    def summary(self):
        with self.lock:
            return summarize(self.records)
