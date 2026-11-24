"""证据驱动的开放决策建议。

负责人可选动作只有三类，且每条建议都必须回链具体指标与口径：

- ``follow_up_study`` 补做任务研究：L1 证据量不足，理解率没有可信分母；
- ``restricted_trial`` 限制试用人群：证据达标但存在版本/行为风险，
  仅向 L1 证据充分的驾驶员群体先行开放；
- ``hold_rollout`` 暂缓开放：已复核理解率低于门槛，或关键分层完全无 L1 证据。

新闻报道没有独立验证理解效果，因此任何"功能已发布"的信息都不能替代
L1 证据；本模块只依据数据库中的可关联证据给建议，不做外部假设。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final

from .analysis import AnalysisReport
from .domain import ObservabilityLevel

L1 = ObservabilityLevel.L1_CONFIRMED.value


@dataclass(frozen=True)
class DecisionThresholds:
    """决策门槛。可由研究负责人在配置中覆盖，默认值偏保守。"""

    min_l1_sessions: int = 8            # 每个显示版本至少 L1 会话数
    min_l1_per_task: int = 2            # 每个任务类型至少 L1 会话数
    min_comprehension_rate: float = 0.75
    max_mis_tap_rate: float = 0.15
    min_questionnaire_coverage: float = 0.60


@dataclass(frozen=True)
class Recommendation:
    action: str                  # follow_up_study | restricted_trial | hold_rollout
    scope: str                   # 适用范围（显示版本/任务/驾驶员群体）
    reason: str                  # 证据依据（含具体数值与口径）
    evidence_refs: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class DriverCohort:
    driver_key: str
    sessions: int
    l1_sessions: int
    reviewed_sessions: int
    understood: int
    comprehension_rate: float | None
    mis_tap_sessions: int
    eligible_for_early_trial: bool
    eligibility_reason: str


def _l1_metrics(report: AnalysisReport, ui_version: str,
                task_type: str | None = None):
    rows = [
        m for m in report.group_metrics
        if m.ui_version == ui_version and m.observability == L1
        and (task_type is None or m.task_type == task_type)
    ]
    sessions = sum(m.sessions for m in rows)
    reviewed = sum(m.reviewed_sessions for m in rows)
    understood = sum(m.understood for m in rows)
    tap = sum(m.tap_sessions for m in rows)
    mis = sum(m.mis_tap_sessions for m in rows)
    rate = understood / reviewed if reviewed else None
    mis_rate = mis / tap if tap else None
    return sessions, reviewed, understood, rate, mis_rate


def recommend_for_ui(report: AnalysisReport, ui_version: str,
                     thresholds: DecisionThresholds) -> list[Recommendation]:
    recs: list[Recommendation] = []
    l1_sessions, reviewed, understood, rate, mis_rate = _l1_metrics(report, ui_version)

    # 规则 1：L1 总量不足 -> 暂缓或补做
    if l1_sessions < thresholds.min_l1_sessions or reviewed < thresholds.min_l1_sessions:
        action = "hold_rollout" if l1_sessions == 0 else "follow_up_study"
        recs.append(Recommendation(
            action=action,
            scope=f"{ui_version} 全任务",
            reason=(
                f"L1 可判定会话仅 {l1_sessions} 个（门槛 {thresholds.min_l1_sessions}），"
                f"已复核编码 {reviewed} 份；理解率缺少可信分母，"
                "新闻发布信息不能替代独立验证。"
            ),
            evidence_refs=("ui_rollups", "session_evidence"),
        ))

    # 规则 2：任务级 L1 缺口
    task_types = sorted({m.task_type for m in report.group_metrics
                         if m.ui_version == ui_version})
    for task in task_types:
        task_l1, task_reviewed, _u, _r, _m = _l1_metrics(report, ui_version, task)
        if task_l1 < thresholds.min_l1_per_task:
            recs.append(Recommendation(
                action="follow_up_study",
                scope=f"{ui_version} / {task}",
                reason=(
                    f"任务类型 {task} 的 L1 会话仅 {task_l1} 个"
                    f"（门槛 {thresholds.min_l1_per_task}），"
                    "需补做该任务的受控任务研究后再评估。"
                ),
                evidence_refs=("group_metrics",),
            ))

    # 规则 3：理解率不达标 -> 暂缓
    if rate is not None and rate < thresholds.min_comprehension_rate:
        recs.append(Recommendation(
            action="hold_rollout",
            scope=f"{ui_version} 全任务",
            reason=(
                f"已复核子样本理解率 {rate:.1%} 低于门槛 "
                f"{thresholds.min_comprehension_rate:.0%}（分母 {reviewed}，"
                "该子样本非随机抽样，仅作下限参考）。"
            ),
            evidence_refs=("ui_rollups",),
        ))

    # 规则 4：误触率过高 -> 限制试用
    if mis_rate is not None and mis_rate > thresholds.max_mis_tap_rate:
        recs.append(Recommendation(
            action="restricted_trial",
            scope=f"{ui_version} 全任务",
            reason=(
                f"L1 会话误触率 {mis_rate:.1%} 高于门槛 "
                f"{thresholds.max_mis_tap_rate:.0%}；点击不等于理解，"
                "试用应限于无误触记录且 L1 证据充分的驾驶员。"
            ),
            evidence_refs=("ui_rollups", "session_evidence"),
        ))

    # 规则 5：问卷覆盖不足 -> 补做（即使理解率表观达标）
    coverage = report.coverage["questionnaire_coverage"]
    if coverage is not None and coverage < thresholds.min_questionnaire_coverage:
        recs.append(Recommendation(
            action="follow_up_study",
            scope="全部显示版本",
            reason=(
                f"问卷覆盖率 {coverage:.1%} 低于门槛 "
                f"{thresholds.min_questionnaire_coverage:.0%}，"
                "无问卷会话的理解度不可观察，且应答者非随机样本。"
            ),
            evidence_refs=("analysis_report.coverage",),
        ))

    # 规则 6：无负面规则触发 -> 可限制试用（不直接建议全量开放）
    if not recs and rate is not None:
        recs.append(Recommendation(
            action="restricted_trial",
            scope=f"{ui_version} 全任务",
            reason=(
                f"L1 会话 {l1_sessions} 个、理解率 {rate:.1%} 达标，"
                "但证据来自合成/有限样本，建议先面向 L1 证据充分的驾驶员群体限量试用，"
                "继续收集复核问卷后再评估全量开放。"
            ),
            evidence_refs=("ui_rollups",),
        ))
    return recs


def build_driver_cohorts(report: AnalysisReport,
                         thresholds: DecisionThresholds) -> list[DriverCohort]:
    """按驾驶员汇总证据并给出早期试用资格。

    资格判定只使用该驾驶员自身的可关联证据，不借用群体均值。
    """
    by_driver: dict[str, list] = {}
    for e in report.session_evidence:
        by_driver.setdefault(e.driver_key, []).append(e)

    cohorts: list[DriverCohort] = []
    for driver in sorted(by_driver):
        rows = by_driver[driver]
        sessions = len(rows)
        l1 = [r for r in rows if r.observability == L1]
        # 个人理解率与全局口径一致：只承认 L1 会话的已复核编码，
        # L2 即使有 coded 问卷也不与当时显示绑定，不计入分子分母。
        l1_reviewed = [r for r in l1 if r.reviewed_response]
        understood = sum(1 for r in l1_reviewed if r.comprehension_correct is True)
        mis = sum(1 for r in rows if r.mis_tap_count > 0)
        rate = understood / len(l1_reviewed) if l1_reviewed else None

        if mis:
            eligible = False
            reason = f"存在 {mis} 个误触会话，点击可靠性不足"
        elif len(l1) < thresholds.min_l1_per_task:
            eligible = False
            reason = f"L1 可判定会话 {len(l1)} 个，低于试用门槛 {thresholds.min_l1_per_task}"
        elif rate is None:
            eligible = False
            reason = "无已复核编码，理解度不可证实"
        elif rate < thresholds.min_comprehension_rate:
            eligible = False
            reason = f"个人理解率 {rate:.1%} 低于门槛 {thresholds.min_comprehension_rate:.0%}"
        else:
            eligible = True
            reason = (
                f"L1 会话 {len(l1)} 个、理解率 {rate:.1%}、无误触，"
                "可进入早期试用名单"
            )
        cohorts.append(DriverCohort(
            driver_key=driver,
            sessions=sessions,
            l1_sessions=len(l1),
            reviewed_sessions=len(l1_reviewed),
            understood=understood,
            comprehension_rate=rate,
            mis_tap_sessions=mis,
            eligible_for_early_trial=eligible,
            eligibility_reason=reason,
        ))
    return cohorts


def decide(report: AnalysisReport,
           thresholds: DecisionThresholds | None = None) -> dict[str, object]:
    thresholds = thresholds or DecisionThresholds()
    ui_versions = sorted({m.ui_version for m in report.group_metrics})
    recommendations = [
        rec
        for ui in ui_versions
        for rec in recommend_for_ui(report, ui, thresholds)
    ]
    # 同一范围去重（规则 5 是全局建议，可能重复）
    seen: set[tuple[str, str]] = set()
    unique: list[Recommendation] = []
    for rec in recommendations:
        key = (rec.action, rec.scope)
        if key not in seen:
            seen.add(key)
            unique.append(rec)

    action_order: Final = {"hold_rollout": 0, "follow_up_study": 1, "restricted_trial": 2}
    unique.sort(key=lambda r: (action_order[r.action], r.scope))

    cohorts = build_driver_cohorts(report, thresholds)
    eligible = [c.driver_key for c in cohorts if c.eligible_for_early_trial]
    return {
        "thresholds": thresholds.__dict__,
        "recommendations": [rec.__dict__ for rec in unique],
        "driver_cohorts": [
            {
                **c.__dict__,
                "comprehension_rate": (
                    None if c.comprehension_rate is None else round(c.comprehension_rate, 4)
                ),
            }
            for c in cohorts
        ],
        "eligible_early_trial_drivers": eligible,
        "headline": unique[0].action if unique else "insufficient_evidence",
    }
