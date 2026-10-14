from __future__ import annotations

import hashlib
import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from . import VERSION
from .config import Settings
from .ingest import read_file, chunk_segments, resolve_visit_date
from .llm import KimiClient, atomic_json, WorkflowError
from .models import Analysis, Audit, Draft, Extraction, Meta, Plan, Segment, CustomerAnalysis, VisitAnalysis, DecisionAnalysis


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def parallel_nodes(tasks, limit, cancel, on_complete=None):
    """Bound concurrency, preserve input order and stop queued work on failure."""
    results = [None] * len(tasks)
    with ThreadPoolExecutor(max_workers=limit, thread_name_prefix="kimi-node") as pool:
        futures = {pool.submit(task): index for index, task in enumerate(tasks)}
        try:
            for count, future in enumerate(as_completed(futures), 1):
                index = futures[future]
                results[index] = future.result()
                if on_complete:
                    on_complete(count, index)
        except BaseException:
            cancel.set()
            for future in futures:
                future.cancel()
            raise
    return results


def validate_branch(result, valid_ids):
    validate_refs(result, valid_ids)
    if isinstance(result, CustomerAnalysis):
        if any(c.status != '推断' for c in result.profile.behavior_hypotheses):
            raise ValueError('行为分析假设只能标为推断')
    if isinstance(result, DecisionAnalysis):
        ids = [b.id for b in result.journey.blockers]
        if len(ids) != len(set(ids)):
            raise ValueError('核心卡点 ID 重复')
        if result.journey.stage != '暂无法判断' and not result.journey.reasons:
            raise ValueError('确定决策阶段必须给出带证据的理由')


def iter_claims(value, path=""):
    if hasattr(value, "model_dump"):
        value = value.model_dump()
    if isinstance(value, dict):
        if "text" in value and "status" in value and "evidence_refs" in value:
            yield path, value
        for key, child in value.items():
            yield from iter_claims(child, f"{path}.{key}".strip("."))
    elif isinstance(value, list):
        for i, child in enumerate(value):
            yield from iter_claims(child, f"{path}[{i}]")


def validate_refs(value, valid_ids: set[str]):
    if hasattr(value, "model_dump"):
        value = value.model_dump()
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "evidence_refs":
                bad = set(child) - valid_ids
                if bad:
                    raise ValueError(f"不存在或属于旁路的证据 ID：{', '.join(sorted(bad))}")
            else:
                validate_refs(child, valid_ids)
    elif isinstance(value, list):
        for child in value:
            validate_refs(child, valid_ids)


def validate_extraction(result: Extraction, valid_ids: set[str]):
    validate_refs(result, valid_ids)
    excluded = {ref for item in result.excluded for ref in item.evidence_refs}
    used = {ref for fact in result.facts for ref in fact.evidence_refs}
    if excluded & used:
        raise ValueError("已排除的旁路片段不能同时作为客户事实的依据：" + ",".join(sorted(excluded & used)))


def validate_analysis(result: Analysis, valid_ids: set[str]):
    validate_refs(result, valid_ids)
    ids = [b.id for b in result.journey.blockers]
    if len(ids) != len(set(ids)):
        raise ValueError("核心卡点 ID 重复")
    if result.journey.stage != "暂无法判断" and not result.journey.reasons:
        raise ValueError("确定决策阶段必须给出带证据的理由")
    for claim in result.profile.behavior_hypotheses:
        if claim.status != "推断":
            raise ValueError("行为经济学 / DISC 标签只能标为推断")


def validate_plan(plan: Plan, analysis: Analysis, valid_ids: set[str]):
    validate_refs(plan, valid_ids)
    blockers = {b.id for b in analysis.journey.blockers}
    for action in plan.actions:
        if action.blocker_ref not in blockers:
            raise ValueError(f"跟进动作引用了不存在的卡点：{action.blocker_ref}")


def validate_draft(draft: Draft, valid_ids: set[str]):
    validate_analysis(draft.analysis, valid_ids)
    validate_plan(draft.plan, draft.analysis, valid_ids)


def merge_extractions(extractions: list[Extraction]) -> dict:
    combined = {"speakers": [], "facts": [], "excluded": [], "unknowns": [], "conflicts": [], "possible_multiple_visits": False}
    seen = set()
    for item in extractions:
        value = item.model_dump()
        combined["possible_multiple_visits"] |= value["possible_multiple_visits"]
        for field in ("speakers", "excluded", "unknowns", "conflicts"):
            for entry in value[field]:
                if entry not in combined[field]:
                    combined[field].append(entry)
        for fact in value["facts"]:
            fact["id"] = ""
            fingerprint = json.dumps({k: fact[k] for k in ("subject", "dimension", "text", "source", "status", "requirement", "numeric")}, ensure_ascii=False, sort_keys=True)
            if fingerprint in seen:
                existing = next(f for f in combined["facts"] if f.get("_key") == fingerprint)
                existing["evidence_refs"] = sorted(set(existing["evidence_refs"] + fact["evidence_refs"]))
                continue
            seen.add(fingerprint)
            fact["_key"] = fingerprint
            combined["facts"].append(fact)
    for i, fact in enumerate(combined["facts"], 1):
        fact.pop("_key", None)
        fact["id"] = f"F{i:04d}"
    # Conflicting cross-chunk classification must not silently remove evidence.
    used = {ref for f in combined["facts"] for ref in f["evidence_refs"]}
    clean_excluded = []
    for item in combined["excluded"]:
        conflict = used.intersection(item["evidence_refs"])
        if conflict:
            combined["unknowns"].append("片段主线归属存在跨块分歧，保留原文待复核：" + ", ".join(sorted(conflict)))
        remaining = [r for r in item["evidence_refs"] if r not in used]
        if remaining:
            clean_excluded.append({**item, "evidence_refs": remaining})
    combined["excluded"] = clean_excluded
    return combined


def numeric_checks(extraction: dict) -> list[dict]:
    issues, scenarios = [], {}
    if extraction["possible_multiple_visits"]:
        issues.append({"severity": "blocker", "path": "source", "problem": "疑似包含多个客户接访，需先复核会话归属，当前仅可作为草稿。", "evidence_refs": [], "required_action": "按客户拆分文本后重新运行"})
    for conflict in extraction["conflicts"]:
        issues.append({"severity": "warning", "path": "facts", "problem": conflict["text"], "evidence_refs": conflict["evidence_refs"], "required_action": "保留冲突，在原文中核实"})
    for fact in extraction["facts"]:
        number = fact.get("numeric")
        if number and number["unit"] in {"万", "万元", "元"} and number["kind"] in {"总价", "首付", "贷款"}:
            value = number["amount"] * (10000 if number["unit"] in {"万", "万元"} else 1)
            key = (fact["subject"], number["scenario"])
            scenarios.setdefault(key, {}).setdefault(number["kind"], []).append((value, fact))
    for (_, scenario), kinds in scenarios.items():
        if all(k in kinds and len({n for n, f in kinds[k]}) == 1 for k in ("总价", "首付", "贷款")):
            total, down, loan = [kinds[k][0][0] for k in ("总价", "首付", "贷款")]
            if abs(total - down - loan) > max(1000, total * .005):
                refs = sorted({r for rows in kinds.values() for _, f in rows for r in f["evidence_refs"]})
                issues.append({"severity": "warning", "path": "facts.numeric", "problem": f"{scenario}：总价 {total/10000:g} 万与首付 {down/10000:g} 万 + 贷款 {loan/10000:g} 万不一致，请核对是否同一方案。", "evidence_refs": refs, "required_action": "核对金额口径，报告中保留冲突"})
    return issues


def run_pipeline(run_dir: Path, settings: Settings, event: Callable | None = None,
                 cancel: threading.Event | None = None, client=None) -> dict:
    callback = event or (lambda *a, **k: None)
    event_lock = threading.RLock()
    def event(*args):
        with event_lock:
            callback(*args)
    cancel = cancel or threading.Event()
    config = json.loads((run_dir / "input.json").read_text(encoding="utf-8"))
    filename = config["filename"]
    meta = resolve_visit_date(Meta.model_validate(config["meta"]), filename)
    source = run_dir / "source" / config["stored_filename"]
    event("ingest", "读取文本，建立原文证据索引", 5)
    text, segments, uncertainty = read_file(source.read_bytes(), filename)
    serialized = [s.model_dump() for s in segments]
    atomic_json(run_dir / "transcript.json", {"filename": filename, "segments": serialized, "warnings": uncertainty})
    atomic_json(run_dir / "evidence.json", {s.id: s.model_dump() for s in segments})
    manifest = {"workflow_version": VERSION, "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                "run_id": run_dir.name, "provider": settings.provider, "model": settings.model, "base_url": settings.base_url,
                "reasoning_effort": settings.effort, "input_kind": "transcript_text",
                "source_name": filename, "template_version": "1.0", "started_at": now(),
                "execution_mode": "parallel_dag", "max_concurrency": settings.max_concurrency, "model_settings": settings.options()}
    atomic_json(run_dir / "manifest.json", manifest)
    if client is None:
        client = KimiClient(settings, cancel, lambda kind, msg: event(kind, msg, None), usage_path=run_dir / "usage.json")
    cache = run_dir / "nodes"
    try:
        chunks = chunk_segments(segments)
        event("extract", f"并发提取 {len(chunks)} 个文本分块（最多 {settings.max_concurrency} 路）", 8)
        def extract_chunk(i, chunk):
            client.check_cancel()
            ids = {s.id for s in chunk}
            return client.structured(f"extract-{i:03d}", "extract", {"meta": meta.model_dump(), "segments": [s.model_dump() for s in chunk]}, Extraction, cache,
                                       lambda obj, ids=ids: validate_extraction(obj, ids))
        results = parallel_nodes([lambda i=i, chunk=chunk: extract_chunk(i, chunk) for i, chunk in enumerate(chunks, 1)],
                                 settings.max_concurrency, cancel,
                                 lambda done, index: event("extract", f"事实提取已完成 {done}/{len(chunks)} 块", 8 + int(28*done/len(chunks))))
        extraction = merge_extractions(results)
        atomic_json(run_dir / "facts.json", extraction)
        program_issues = numeric_checks(extraction)
        excluded_ids = {r for item in extraction["excluded"] for r in item["evidence_refs"]}
        active = [s for s in serialized if s["id"] not in excluded_ids]
        active_ids = {s["id"] for s in active}
        if not active:
            raise WorkflowError("没有识别到有效接访主线，请核对文本是否属于客户接访")
        common = {"meta": meta.model_dump(), "extraction": extraction, "transcript": active,
                  "program_issues": program_issues, "uncertainty": uncertainty}
        event("analyze", "并发分析：客户需求 / 带看事实 / 决策阶段", 42)
        branches = [('customer', CustomerAnalysis), ('visit', VisitAnalysis), ('decision', DecisionAnalysis)]
        parts = parallel_nodes([lambda name=name, schema=schema: client.structured('analysis-'+name, 'analyze-'+name, common, schema, cache,
                                    lambda obj: validate_branch(obj, active_ids)) for name, schema in branches],
                               settings.max_concurrency, cancel,
                               lambda done, index: event('analyze', f"分析分支已完成 {done}/3：{branches[index][0]}", 42+done*5))
        analysis = Analysis.model_validate({key:value for part in parts for key,value in part.model_dump().items()})
        validate_analysis(analysis, active_ids)
        atomic_json(run_dir / 'analysis.json', analysis.model_dump())
        event("plan", "提炼核心洞察，形成可执行的跟进建议", 60)
        plan = client.structured("plan", "plan", {**common, "analysis": analysis.model_dump()}, Plan, cache,
                                 lambda obj: validate_plan(obj, analysis, active_ids))
        draft = Draft(analysis=analysis, plan=plan)
        audit = None
        for revision in range(3):
            event("audit", f"对照原文独立质检（第 {revision+1} 轮）", 72 + revision*6)
            audit = client.structured(f"audit-{revision}", "audit", {**common, "draft": draft.model_dump()}, Audit, cache, lambda obj: validate_refs(obj, active_ids))
            if audit.verdict != "needs_review" or revision == 2:
                break
            event("repair", "修正无依据或不一致的结论", 75 + revision*6)
            draft = client.structured(f"repair-{revision}", "repair", {**common, "draft": draft.model_dump(), "audit": audit.model_dump()}, Draft, cache,
                                      lambda obj: validate_draft(obj, active_ids))
        client.check_cancel()
        findings = program_issues + [i.model_dump() for i in audit.issues]
        status = "needs_review" if audit.verdict == "needs_review" or any(i["severity"] == "blocker" for i in findings) else "complete"
        quality = {"report_status": status, "audit_verdict": audit.verdict,
                   "warnings": list(dict.fromkeys(uncertainty + extraction["unknowns"])), "issues": findings,
                   "excluded": extraction["excluded"], "has_timestamps": any(s.start_sec is not None for s in segments),
                   "basis": "仅分析用户提供的转写文本；未核验原始录音、声纹、库存或政策。"}
        report = {"meta": {**meta.model_dump(), "run_id": run_dir.name, "filename": filename, "generated_at": now(),
                           "provider": settings.provider, "model": settings.model, "workflow_version": VERSION, "is_demo": False},
                  "analysis": draft.analysis.model_dump(), "plan": draft.plan.model_dump(),
                  "quality": quality, "evidence": {s.id: s.model_dump() for s in segments},
                  "usage": client.ledger.summary() if hasattr(client, 'ledger') else None}
        atomic_json(run_dir / "report.json", report)
        atomic_json(run_dir / "quality.json", quality)
        event("render", "生成 HTML 看板和证据跳转", 94)
        from .render import render_report
        render_report(report, run_dir / "report.html")
        manifest["completed_at"] = now()
        manifest["report_status"] = status
        atomic_json(run_dir / "manifest.json", manifest)
        event("complete", "看板已生成" if status == "complete" else "看板已生成，部分结论需要复核", 100)
        return report
    finally:
        if hasattr(client, 'ledger'):
            client.ledger.flush()
