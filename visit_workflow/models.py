from __future__ import annotations

from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator

Status = Literal["明确", "推断", "待验证", "冲突", "明确否定"]
Dimension = Literal["who", "what", "why", "when", "where", "how", "how_much", "other"]
STAGES = ["动机酝酿", "信息搜索", "需求发育", "聚焦收敛", "有效比较", "签约交付"]
DIMENSIONS = {"who": "谁参与决策", "what": "买什么产品", "why": "为什么买", "when": "何时购买入住", "where": "区域与配套", "how": "如何做决定", "how_much": "预算与资金"}


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Segment(StrictModel):
    id: str
    speaker: str = "未标注"
    text: str
    start_sec: float | None = None
    end_sec: float | None = None
    line_start: int
    line_end: int


class Claim(StrictModel):
    text: str = Field(min_length=1, max_length=1500)
    status: Status
    evidence_refs: list[str] = Field(max_length=12)

    @model_validator(mode="after")
    def require_evidence(self):
        if self.status != "待验证" and not self.evidence_refs:
            raise ValueError("明确、推断、冲突和否定结论必须有原文证据 ID")
        return self


class SpeakerRole(StrictModel):
    label: str
    role: Literal["销售", "客户", "陪同者", "其他", "未知"]
    description: Claim


class NumericFact(StrictModel):
    kind: Literal["总价", "首付", "贷款", "价差", "月供", "面积", "单价", "其他"]
    amount: float = Field(ge=0, allow_inf_nan=False)
    unit: str
    scenario: str = Field(description="只对有明确证据属于同一房源同一报价方案的数字使用相同 scenario，否则分别标记")


class Fact(Claim):
    id: str = ""
    subject: str
    dimension: Dimension
    source: Literal["客户陈述", "销售陈述", "说话人待确认"]
    requirement: Literal["必须", "偏好", "排除", "未分类"]
    numeric: NumericFact | None = None


class Excluded(StrictModel):
    evidence_refs: list[str] = Field(min_length=1)
    reason: str


class Extraction(StrictModel):
    speakers: list[SpeakerRole]
    facts: list[Fact] = Field(max_length=100)
    excluded: list[Excluded]
    unknowns: list[str]
    conflicts: list[Claim]
    possible_multiple_visits: bool


class Profile(StrictModel):
    motivations: list[Claim] = Field(max_length=6)
    household: list[Claim] = Field(max_length=6)
    decision_roles: list[Claim] = Field(max_length=6)
    communication: list[Claim] = Field(max_length=4)
    behavior_hypotheses: list[Claim] = Field(max_length=3)


class NeedRow(StrictModel):
    must: list[Claim] = Field(max_length=5)
    want: list[Claim] = Field(max_length=5)
    excluded: list[Claim] = Field(max_length=3)
    unknowns: list[str] = Field(max_length=4)


class Needs(StrictModel):
    who: NeedRow
    what: NeedRow
    why: NeedRow
    when: NeedRow
    where: NeedRow
    how: NeedRow
    how_much: NeedRow


class Maturity(StrictModel):
    name: str
    state: Literal["已稳定", "发育中", "待确认"]
    detail: Claim


class Moment(StrictModel):
    title: str
    before: Claim
    reaction: Claim
    significance: Claim


class Blocker(StrictModel):
    id: str
    title: str
    kind: Literal["需求型", "条件型"]
    detail: Claim


class Journey(StrictModel):
    stage: Literal["动机酝酿", "信息搜索", "需求发育", "聚焦收敛", "有效比较", "签约交付", "暂无法判断"]
    reasons: list[Claim] = Field(max_length=5)
    gaps: list[Claim] = Field(max_length=5)
    readiness: Claim
    blockers: list[Blocker] = Field(max_length=2)


class Property(StrictModel):
    name: str
    area: Claim
    price: Claim
    advantages: list[Claim] = Field(max_length=3)
    drawbacks: list[Claim] = Field(max_length=3)
    reaction: Claim


class Analysis(StrictModel):
    profile: Profile
    needs: Needs
    maturity: list[Maturity] = Field(max_length=10)
    core_tension: Claim | None
    key_moments: list[Moment] = Field(max_length=3)
    journey: Journey
    budget: Claim
    properties: list[Property] = Field(max_length=10)
    competitors: list[Claim] = Field(max_length=8)
    customer_next_steps: list[Claim] = Field(max_length=6)
    blind_spots: list[str] = Field(max_length=12)


class CustomerAnalysis(StrictModel):
    profile: Profile
    needs: Needs
    maturity: list[Maturity] = Field(max_length=10)
    core_tension: Claim | None
    blind_spots: list[str] = Field(max_length=12)


class VisitAnalysis(StrictModel):
    key_moments: list[Moment] = Field(max_length=3)
    budget: Claim
    properties: list[Property] = Field(max_length=10)
    competitors: list[Claim] = Field(max_length=8)
    customer_next_steps: list[Claim] = Field(max_length=6)


class DecisionAnalysis(StrictModel):
    journey: Journey


class Action(StrictModel):
    priority: Literal["优先", "随后"]
    blocker_ref: str
    title: str
    owner: str
    action: str
    question: str
    materials: str
    suggested_timing: str
    completion_criterion: str
    evidence_refs: list[str] = Field(min_length=1, max_length=8)


class Summary(Claim):
    text: str = Field(min_length=1, max_length=180, description="60-140字核心洞察，最多180字。正文不写E编号，只在evidence_refs引用。包含推断时标推断。")


class Plan(StrictModel):
    summary: Summary
    actions: list[Action] = Field(max_length=4)


class Draft(StrictModel):
    analysis: Analysis
    plan: Plan


class Issue(StrictModel):
    severity: Literal["blocker", "warning"]
    path: str
    problem: str
    evidence_refs: list[str]
    required_action: str


class Audit(StrictModel):
    verdict: Literal["pass", "pass_with_unknowns", "needs_review"]
    issues: list[Issue] = Field(max_length=30)

    @model_validator(mode="after")
    def consistent_verdict(self):
        if any(i.severity == "blocker" for i in self.issues):
            self.verdict = "needs_review"
        elif self.issues and self.verdict == "pass":
            self.verdict = "pass_with_unknowns"
        return self


class Meta(StrictModel):
    client_alias: str = Field(default="本次接访客户", min_length=1, max_length=80)
    project: str = Field(default="", max_length=100)
    advisor: str = Field(default="", max_length=80)
    visit_date: str = Field(default="", max_length=10, pattern=r"^(\d{4}-\d{2}-\d{2})?$")
    # Retained only to read historical jobs; rendering now uses one unified layout.
    profile: Literal["standard", "action"] = "standard"

    @model_validator(mode="after")
    def valid_date(self):
        if self.visit_date:
            from datetime import date
            date.fromisoformat(self.visit_date)
        return self
