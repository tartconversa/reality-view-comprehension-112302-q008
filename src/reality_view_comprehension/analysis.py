"""理解度边界分析。

输出严格区分三类观察，并在每一层披露分母与缺口：

- 功能出现：以可关联渲染对象为准（孤儿对象不计入任何会话）；
- 点击行为：tap/dismiss 是行为观察，误触单独标记，``none`` 与 ``unknown``
  不按成功或失败处理；
- 理解度：只承认 ``review_state='coded'`` 的人工编码，且只有 L1 会话的
  理解度可与“当时显示 + 当时计划”绑定解释。

问卷完成者不是曝光总体的随机样本，因此理解率只描述“已复核编码子样本”，
不外推为全体驾驶员的理解水平；L2/L3/L4 的会话数本身就是证据缺口。
"""

from __future__ import annotations

import csv
import json
import sqlite3
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path

from .domain import GroupMetrics, ObservabilityLevel, SessionEvidence
from .observability import build_session_evidence, orphan_object_count


def _rate(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _aggregate(rows: list[SessionEvidence], ui_version: str, task_type: str,
               observability: str) -> GroupMetrics:
    sessions = len(rows)
    feature_present = sum(1 for r in rows if r.object_count > 0)
    tap_rows = [r for r in rows if r.tap_count + r.dismiss_count > 0]
    matched_rows = [r for r in tap_rows if r.matched_tap_count > 0]
    mis_rows = [r for r in tap_rows if r.mis_tap_count > 0]
    responded = sum(1 for r in rows if r.has_response)
    # 理解度口径：只有 L1 可判定会话的已复核编码才能与当时显示/计划绑定。
    # L2 即使存在 coded 问卷，也不计入理解率分子分母，只在会话级证据中保留。
    l1_rows = [r for r in rows if r.observability == ObservabilityLevel.L1_CONFIRMED.value]
    reviewed_rows = [r for r in l1_rows if r.reviewed_response]
    understood = sum(
        1 for r in reviewed_rows if r.comprehension_correct is True
    )
    misread = sum(1 for r in reviewed_rows if r.coded_intent == "misread")
    unclear = sum(1 for r in reviewed_rows if r.coded_intent == "unclear")

    behavior_decided = [
        r for r in rows
        if r.correct_outcomes + r.incorrect_outcomes > 0
    ]
    behavior_correct = sum(1 for r in behavior_decided if r.correct_outcomes > 0)

    return GroupMetrics(
        ui_version=ui_version,
        task_type=task_type,
        observability=observability,
        sessions=sessions,
        feature_present_sessions=feature_present,
        tap_sessions=len(tap_rows),
        matched_tap_sessions=len(matched_rows),
        mis_tap_sessions=len(mis_rows),
        responded_sessions=responded,
        reviewed_sessions=len(reviewed_rows),
        understood=understood,
        misread=misread,
        unclear=unclear,
        comprehension_rate=_rate(understood, len(reviewed_rows)),
        behavior_accuracy=_rate(behavior_correct, len(behavior_decided)),
        mis_tap_rate=_rate(len(mis_rows), len(tap_rows)),
    )


@dataclass
class AnalysisReport:
    total_sessions: int
    orphan_objects: int
    quarantined_rows: int
    group_metrics: list[GroupMetrics]
    ui_rollups: list[GroupMetrics]
    session_evidence: list[SessionEvidence]
    coverage: dict[str, object] = field(default_factory=dict)
    gaps: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "total_sessions": self.total_sessions,
            "orphan_objects": self.orphan_objects,
            "quarantined_rows": self.quarantined_rows,
            "coverage": self.coverage,
            "gaps": self.gaps,
            "group_metrics": [m.as_row() for m in self.group_metrics],
            "ui_rollups": [m.as_row() for m in self.ui_rollups],
        }


def _coverage_and_gaps(evidence: list[SessionEvidence],
                       orphan_count: int, quarantined: int) -> tuple[dict, list[str]]:
    total = len(evidence)
    by_grade: dict[str, int] = {g.value: 0 for g in ObservabilityLevel}
    for e in evidence:
        by_grade[e.observability] += 1
    responded = sum(1 for e in evidence if e.has_response)
    reviewed = sum(1 for e in evidence if e.reviewed_response)
    late = sum(1 for e in evidence if e.exposure_state == "late")
    partial = sum(1 for e in evidence if e.exposure_state == "partial")
    misaligned = sum(1 for e in evidence if not e.aligned and e.object_count > 0)
    mis_taps = sum(1 for e in evidence if e.mis_tap_count > 0)
    no_response = total - responded

    coverage = {
        "sessions_total": total,
        "by_observability": by_grade,
        "questionnaire_coverage": _rate(responded, total),
        "reviewed_coverage": _rate(reviewed, total),
        "late_exposure_sessions": late,
        "partial_exposure_sessions": partial,
        "version_misaligned_sessions": misaligned,
        "mis_tap_sessions": mis_taps,
        "no_questionnaire_sessions": no_response,
        "orphan_objects_excluded": orphan_count,
        "quarantined_rows_excluded": quarantined,
    }

    gaps: list[str] = []
    if no_response:
        gaps.append(
            f"{no_response} 个会话无有效问卷，理解度对这些会话不可观察；"
            "问卷完成者非随机样本，理解率不得外推为全体曝光者水平。"
        )
    pending = sum(1 for e in evidence if e.has_response and not e.reviewed_response)
    if pending:
        gaps.append(
            f"{pending} 个会话仅有待复核/修订中问卷，未计入理解度分子分母。"
        )
    if misaligned:
        gaps.append(
            f"{misaligned} 个会话的渲染对象来源版本未在兼容清单登记，"
            "无法确认所见内容与当时路线计划一致，相关会话降为 L3。"
        )
    if late:
        gaps.append(
            f"{late} 个会话为晚到曝光（late），事件时点界面可见性不可证实，"
            "即使后续补齐记录，最高只评 L2。"
        )
    if partial:
        gaps.append(f"{partial} 个会话为部分曝光（partial），对象呈现不完整，最高只评 L2。")
    if orphan_count:
        gaps.append(
            f"{orphan_count} 条渲染对象找不到会话（孤儿），已保留但不进入任何会话指标。"
        )
    if quarantined:
        gaps.append(f"{quarantined} 行资料未通过校验进入隔离区，未参与分析。")
    if mis_taps:
        gaps.append(f"{mis_taps} 个会话存在误触点击，点击不等于正确理解，已单列误触率。")
    return coverage, gaps


def run_analysis(conn: sqlite3.Connection) -> AnalysisReport:
    evidence = build_session_evidence(conn)
    orphan_count = orphan_object_count(conn)
    quarantined = conn.execute("SELECT COUNT(*) FROM quarantine_rows").fetchone()[0]

    # 完整三维分层（保留零样本层，便于披露边界）
    groups: "OrderedDict[tuple[str, str, str], list[SessionEvidence]]" = OrderedDict()
    ui_versions = sorted({e.ui_version for e in evidence})
    task_types = sorted({e.task_type for e in evidence})
    grades = [g.value for g in ObservabilityLevel]
    for ui in ui_versions:
        for task in task_types:
            for grade in grades:
                groups[(ui, task, grade)] = []
    for e in evidence:
        groups[(e.ui_version, e.task_type, e.observability)].append(e)
    group_metrics = [
        _aggregate(rows, ui, task, grade)
        for (ui, task, grade), rows in groups.items()
        if rows  # 零样本层不输出行，但其存在性在覆盖统计中体现
    ]

    ui_rollups: list[GroupMetrics] = []
    for ui in ui_versions:
        rows = [e for e in evidence if e.ui_version == ui]
        for grade in grades:
            sub = [e for e in rows if e.observability == grade]
            if sub:
                ui_rollups.append(_aggregate(sub, ui, "ALL_TASKS", grade))

    coverage, gaps = _coverage_and_gaps(evidence, orphan_count, quarantined)
    return AnalysisReport(
        total_sessions=len(evidence),
        orphan_objects=orphan_count,
        quarantined_rows=quarantined,
        group_metrics=group_metrics,
        ui_rollups=ui_rollups,
        session_evidence=evidence,
        coverage=coverage,
        gaps=gaps,
    )


# ---------------------------------------------------------------------------
# 导出
# ---------------------------------------------------------------------------
def _write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def _evidence_row(e: SessionEvidence) -> dict[str, object]:
    return {
        "session_id": e.session_id,
        "driver_key": e.driver_key,
        "ui_version": e.ui_version,
        "task_type": e.task_type,
        "route_intent_version": e.route_intent_version,
        "exposure_state": e.exposure_state,
        "version_aligned": int(e.aligned),
        "observability": e.observability,
        "object_count": e.object_count,
        "object_source_versions": "|".join(sorted(e.object_source_versions)),
        "tap_count": e.tap_count,
        "dismiss_count": e.dismiss_count,
        "none_count": e.none_count,
        "matched_tap_count": e.matched_tap_count,
        "mis_tap_count": e.mis_tap_count,
        "correct_outcomes": e.correct_outcomes,
        "incorrect_outcomes": e.incorrect_outcomes,
        "unknown_outcomes": e.unknown_outcomes,
        "has_response": int(e.has_response),
        "reviewed_response": int(e.reviewed_response),
        "withdrawn_response": int(e.withdrawn_response),
        "coded_intent": e.coded_intent or "",
        "comprehension_correct": (
            "" if e.comprehension_correct is None else int(e.comprehension_correct)
        ),
    }


def export_report(report: AnalysisReport, out_dir: str | Path) -> dict[str, Path]:
    """导出 CSV + JSON + Markdown 四件产物，返回文件路径映射。"""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    paths = {
        "group_metrics": out / "group_metrics.csv",
        "ui_rollups": out / "ui_rollups.csv",
        "session_evidence": out / "session_evidence.csv",
        "analysis_report": out / "analysis_report.json",
        "markdown": out / "理解度分析报告.md",
    }
    _write_csv(paths["group_metrics"], [m.as_row() for m in report.group_metrics])
    _write_csv(paths["ui_rollups"], [m.as_row() for m in report.ui_rollups])
    _write_csv(paths["session_evidence"], [_evidence_row(e) for e in report.session_evidence])
    paths["analysis_report"].write_text(
        json.dumps(report.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    paths["markdown"].write_text(_render_markdown(report), encoding="utf-8")
    return paths


def _fmt_rate(value: float | None) -> str:
    return "—（无可用分母）" if value is None else f"{value:.1%}"


def _render_markdown(report: AnalysisReport) -> str:
    lines: list[str] = []
    lines.append("# Reality View 理解度边界分析报告")
    lines.append("")
    lines.append(
        "本报告基于本地合成资料。新闻报道未提供对理解效果的独立验证，"
        "下列比率仅描述资料内可观察样本，不构成真实驾驶员结论。"
    )
    lines.append("")
    lines.append("## 1. 覆盖与证据边界")
    lines.append("")
    lines.append(f"- 会话总数：{report.total_sessions}")
    for grade, count in report.coverage["by_observability"].items():  # type: ignore[index]
        lines.append(f"- {grade}：{count} 个会话")
    lines.append(
        f"- 问卷覆盖率：{_fmt_rate(report.coverage['questionnaire_coverage'])}；"  # type: ignore[index]
        f"已复核编码覆盖率：{_fmt_rate(report.coverage['reviewed_coverage'])}"
    )
    lines.append(
        f"- 晚到曝光 {report.coverage['late_exposure_sessions']} 个、"  # type: ignore[index]
        f"部分曝光 {report.coverage['partial_exposure_sessions']} 个、"
        f"版本错配 {report.coverage['version_misaligned_sessions']} 个、"
        f"误触会话 {report.coverage['mis_tap_sessions']} 个"
    )
    lines.append(
        f"- 排除项：孤儿渲染对象 {report.orphan_objects} 条、"
        f"隔离区资料 {report.quarantined_rows} 行"
    )
    lines.append("")
    lines.append("## 2. 显示版本 × 任务类型 × 可观察程度 分层指标")
    lines.append("")
    header = (
        "| 显示版本 | 任务 | 可观察程度 | 会话 | 功能出现 | 点击会话 | 误触会话 | "
        "已复核 | 理解 | 误读 | 不明确 | 理解率 | 行为正确率 | 误触率 |"
    )
    lines.append(header)
    lines.append("| " + " | ".join(["---"] * 13) + " |")
    for m in report.group_metrics:
        lines.append(
            "| {ui} | {task} | {grade} | {sessions} | {fp} | {tap} | {mis} | {rev} | "
            "{ok} | {mr} | {un} | {cr} | {ba} | {mtr} |".format(
                ui=m.ui_version, task=m.task_type, grade=m.observability,
                sessions=m.sessions, fp=m.feature_present_sessions, tap=m.tap_sessions,
                mis=m.mis_tap_sessions, rev=m.reviewed_sessions, ok=m.understood,
                mr=m.misread, un=m.unclear, cr=_fmt_rate(m.comprehension_rate),
                ba=_fmt_rate(m.behavior_accuracy), mtr=_fmt_rate(m.mis_tap_rate),
            )
        )
    lines.append("")
    lines.append("## 3. 显示版本汇总（跨任务）")
    lines.append("")
    lines.append(header)
    lines.append("| " + " | ".join(["---"] * 13) + " |")
    for m in report.ui_rollups:
        lines.append(
            "| {ui} | {task} | {grade} | {sessions} | {fp} | {tap} | {mis} | {rev} | "
            "{ok} | {mr} | {un} | {cr} | {ba} | {mtr} |".format(
                ui=m.ui_version, task=m.task_type, grade=m.observability,
                sessions=m.sessions, fp=m.feature_present_sessions, tap=m.tap_sessions,
                mis=m.mis_tap_sessions, rev=m.reviewed_sessions, ok=m.understood,
                mr=m.misread, un=m.unclear, cr=_fmt_rate(m.comprehension_rate),
                ba=_fmt_rate(m.behavior_accuracy), mtr=_fmt_rate(m.mis_tap_rate),
            )
        )
    lines.append("")
    lines.append("## 4. 无法判定与无问卷范围")
    lines.append("")
    if report.gaps:
        lines.extend(f"- {g}" for g in report.gaps)
    else:
        lines.append("- 当前批次未发现关联缺口。")
    lines.append("")
    lines.append("## 5. 口径说明")
    lines.append("")
    lines.extend([
        "- 功能出现、点击行为、正确解释是三类独立观察，不互相替代。",
        "- 理解率分母仅为 L1 可判定会话中 review_state='coded' 的记录；"
        "L2 的 coded 问卷无法与当时显示/计划绑定，不计入；"
        "pending/revised/withdrawn 一律不计入。",
        "- 行为正确率排除 task_outcome='unknown'；误触率分母为发生 tap/dismiss 的会话。",
        "- L1 之外的理解编码无法与当时显示/计划绑定，只能作为子样本描述。",
        "- 版本对齐以 domain.VERSION_ALIGNMENT 登记组合为准，未登记组合降为 L3。",
        "",
    ])
    return "\n".join(lines)
