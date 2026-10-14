"""Explicit synthetic data for offline rendering tests; never used as an LLM fallback."""
from __future__ import annotations

from pathlib import Path
from .config import ROOT
from .ingest import read_file
from .models import Analysis, Draft, Extraction, Plan
from .llm import atomic_json
from .render import render_report


def claim(text, refs=(), status="明确"):
    return {"text": text, "status": status, "evidence_refs": list(refs)}


def demo_draft() -> Draft:
    need = lambda: {"must": [], "want": [], "excluded": [], "unknowns": []}
    needs = {key: need() for key in ["who", "what", "why", "when", "where", "how", "how_much"]}
    needs["who"]["must"] = [claim("夫妻需要共同商量，爱人本次未到场。", ["E00009"])]
    needs["what"]["must"] = [claim("四房是必须条件，认可114平方米。", ["E00005"])]
    needs["what"]["want"] = [claim("需要实际比较四楼与八楼的采光。", ["E00007"])]
    needs["why"]["must"] = [claim("从两房换四房，满足一家三口自住需求。", ["E00002", "E00011"])]
    needs["why"]["excluded"] = [claim("明确表示不以投资为目的。", ["E00011"], "明确否定")]
    needs["when"]["unknowns"] = ["入住和复访日期尚未确定。"]
    needs["where"]["unknowns"] = ["区域、通勤和教育要求未提及。"]
    needs["how"]["must"] = [claim("实地比较后再与爱人共同商量。", ["E00009", "E00012"])]
    needs["how_much"]["want"] = [claim("总价希望300万元以内，首付准备90万元，月供希望不超过8000元。", ["E00003"])]
    a = Analysis.model_validate({
        "profile": {"motivations": [claim("居住改善：当前两房难以满足家庭的空间需求，希望换四房。", ["E00002"], "推断")],
            "household": [claim("夫妻和一个孩子组成的三口之家。", ["E00002"])],
            "decision_roles": [claim("夫妻共同商量，爱人本次没有到场。", ["E00009"])],
            "communication": [claim("客户主动要求实地验证采光，沟通时应提供可核实的对比资料。", ["E00007"], "推断")], "behavior_hypotheses": []},
        "needs": needs,
        "maturity": [{"name": "四房与114平方米", "state": "已稳定", "detail": claim("客户明确认可这两个产品条件。", ["E00005"])},
            {"name": "四楼或八楼的取舍", "state": "发育中", "detail": claim("尚未决定，希望先比较实际采光。", ["E00005", "E00007"])},
            {"name": "入住与复访时间", "state": "待确认", "detail": claim("具体时间尚未确定。", ["E00011", "E00012"])}],
        "core_tension": claim("已认可114平方米四房，但尚未确认楼层差价对应的实际居住体验是否值得。", ["E00004", "E00005", "E00007"], "推断"),
        "key_moments": [{"title": "需要实地证据，才能比较楼层", "before": claim("顾问介绍八楼视野并建议实地查看。", ["E00006"]), "reaction": claim("客户要求了解实际采光差异，不接受只听口头介绍。", ["E00007"]), "significance": claim("下一步应围绕实地对比展开，先帮助客户明确取舍标准。", ["E00007"], "推断")},
            {"title": "家庭共同决策尚未完成", "before": claim("开始讨论两套房源的实地比较。", ["E00008"]), "reaction": claim("客户强调还需与未到场的爱人商量。", ["E00009"]), "significance": claim("应补齐共同决策人的意见，不能视为已经达成购买共识。", ["E00009", "E00012"], "推断")}],
        "journey": {"stage": "聚焦收敛", "reasons": [claim("已明确四房、114平方米，并缩小到两个楼层候选。", ["E00005", "E00012"], "推断")],
            "gaps": [claim("楼层体验差异尚未实地核实，共同决策人的意见未确认。", ["E00007", "E00009"], "推断")],
            "readiness": claim("提出了预算与首付准备，但未确定入住、复访时间，也未承诺购买。", ["E00003", "E00011", "E00012"], "推断"),
            "blockers": [{"id": "B1", "title": "楼层差异缺少体验依据", "kind": "需求型", "detail": claim("需要实地比较采光，再决定四楼或八楼。", ["E00007"])},
                         {"id": "B2", "title": "共同决策人尚未参与", "kind": "条件型", "detail": claim("爱人未到场，尚待家庭商量。", ["E00009"])}]},
        "budget": claim("客户希望总价不超过300万元，首付90万元，月供不超过8000元；贷款方案尚未核实。", ["E00003"]),
        "properties": [{"name": "A楼四层", "area": claim("114平方米", ["E00004"]), "price": claim("销售报价277万元", ["E00004"]), "advantages": [], "drawbacks": [], "reaction": claim("仍在与八楼比较，未决定。", ["E00005"])},
                       {"name": "A楼八层", "area": claim("114平方米", ["E00004"]), "price": claim("销售报价297万元", ["E00004"]), "advantages": [claim("销售称视野更好，尚待实地核实。", ["E00006"])], "drawbacks": [], "reaction": claim("需要比较采光后再决定。", ["E00007"])}],
        "competitors": [], "customer_next_steps": [claim("先比较楼层，再约爱人一起来，具体时间未定。", ["E00012"])],
        "blind_spots": ["两套房源的同日成交口径与实际采光未核实。", "爱人的需求和意见尚未获得。", "贷款年限、利率、首付到位时间未讨论。"]})
    p = Plan.model_validate({"summary": claim("客户已聚焦114平方米四房，当前需要实地比较楼层差异，并补齐爱人的意见，再推进选择。", ["E00005", "E00007", "E00009"], "推断"),
        "actions": [{"priority": "优先", "blocker_ref": "B1", "title": "安排两套房的实地对比", "owner": "置业顾问", "action": "先核实两套房同日有效报价，并安排同一时段实地查看。", "question": "比较过采光后，您最在意的差异是哪一项？", "materials": "同口径报价、户型图、可实地验证的采光记录。", "suggested_timing": "建议下次联系时确认看房时间。", "completion_criterion": "客户能说清两套房的主要差异与个人取舍。", "evidence_refs": ["E00007"]},
            {"priority": "随后", "blocker_ref": "B2", "title": "补齐共同决策人的需求", "owner": "置业顾问", "action": "邀请客户与爱人一起确认看房安排，了解双方底线。", "question": "您爱人最在意户型、楼层还是总价？什么时候方便一起看？", "materials": "两套房的简明对比资料。", "suggested_timing": "建议客户家庭商量后再确认，不默认已预约。", "completion_criterion": "双方主要需求与可参加的时间得到明确回复。", "evidence_refs": ["E00009", "E00012"]}]})
    return Draft(analysis=a, plan=p)


def build_demo(profile="standard") -> dict:
    path = ROOT / "examples" / "示例接访.txt"
    text, segments, warnings = read_file(path.read_bytes(), path.name)
    draft = demo_draft()
    report = {"meta": {"client_alias": "示例家庭", "project": "演示案场", "advisor": "", "visit_date": "", "profile": profile,
        "filename": path.name, "generated_at": "演示数据，非真实生成时间", "model": "none", "workflow_version": "1.0.0", "is_demo": True},
        **draft.model_dump(), "evidence": {s.id: s.model_dump() for s in segments},
        "quality": {"report_status": "complete", "audit_verdict": "synthetic_fixture", "warnings": ["人工构造的演示数据，未调用 LLM。"], "issues": [], "excluded": [], "has_timestamps": True, "basis": "本页仅用于展示结构和验证渲染，不代表真实客户分析。"}}
    return report


def main():
    folder = ROOT / "output" / "demo"
    folder.mkdir(parents=True, exist_ok=True)
    report = build_demo()
    atomic_json(folder / "report.json", report)
    render_report(report, folder / "report.html")
    render_report(build_demo("action"), folder / "action.html")
    print("统一双 Tab 演示看板已生成")


if __name__ == "__main__":
    main()
