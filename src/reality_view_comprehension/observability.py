"""可观察程度分级与单会话证据构建。

分级规则（保守优先，宁降勿升）：

L4-无证据
    会话没有任何可关联对象、交互或问卷。
L3-关联缺口
    存在记录但无法确认用户当时所见：渲染对象来源版本与显示/意图版本未在
    兼容清单登记（版本错配），或完全没有渲染对象却存在交互/问卷。
L2-仅行为可观察
    版本对齐，但曝光为 partial/late（无法确认事件发生时界面已呈现），或
    缺少已人工复核（coded）的问卷；只能报告功能出现与点击行为。
L1-可判定
    版本对齐、full 曝光、且存在 review_state='coded' 的未撤回问卷。

注意：晚到曝光（late）即使后续补齐记录，事件时点的可见性仍不可证实，
最高只能评 L2。误触是行为质量标记，不单独改变会话分级。
"""

from __future__ import annotations

import sqlite3

from .domain import (
    CodedIntent,
    ObservabilityLevel,
    SessionEvidence,
    is_version_aligned,
)


def _graded(
    *,
    aligned: bool,
    exposure_state: str,
    object_count: int,
    has_interaction: bool,
    has_response: bool,
    reviewed_response: bool,
) -> ObservabilityLevel:
    has_any_record = object_count > 0 or has_interaction or has_response
    if not has_any_record:
        return ObservabilityLevel.L4_NO_EVIDENCE
    # 有记录但无法关联到当时显示/计划
    if not aligned or object_count == 0:
        return ObservabilityLevel.L3_LINKAGE_GAP
    if exposure_state != "full":
        # partial / late：行为可观察，理解度在事件时点不可证实
        return ObservabilityLevel.L2_BEHAVIOR_ONLY
    if not reviewed_response:
        return ObservabilityLevel.L2_BEHAVIOR_ONLY
    return ObservabilityLevel.L1_CONFIRMED


def build_session_evidence(conn: sqlite3.Connection) -> list[SessionEvidence]:
    """从数据库重建每个会话的证据与分级。结果按 session_id 排序，确定性输出。"""
    sessions = conn.execute(
        "SELECT * FROM view_sessions ORDER BY session_id"
    ).fetchall()

    evidence: list[SessionEvidence] = []
    for s in sessions:
        sid = s["session_id"]

        objects = conn.execute(
            "SELECT * FROM rendered_objects WHERE session_id=? AND is_orphan=0",
            (sid,),
        ).fetchall()
        source_versions = frozenset(o["source_version"] for o in objects)
        bands = frozenset(o["confidence_band"] for o in objects)
        aligned = bool(objects) and all(
            is_version_aligned(s["ui_version"], o["source_version"],
                               s["route_intent_version"])
            for o in objects
        )

        interactions = conn.execute(
            "SELECT * FROM interactions WHERE session_id=?", (sid,)
        ).fetchall()
        tap = sum(1 for i in interactions if i["action"] == "tap")
        dismiss = sum(1 for i in interactions if i["action"] == "dismiss")
        none = sum(1 for i in interactions if i["action"] == "none")
        matched_tap = sum(
            1 for i in interactions
            if i["action"] in ("tap", "dismiss") and i["is_mis_tap"] == 0
        )
        mis_tap = sum(1 for i in interactions if i["is_mis_tap"] == 1)
        correct = sum(1 for i in interactions if i["task_outcome"] == "correct")
        incorrect = sum(1 for i in interactions if i["task_outcome"] == "incorrect")
        unknown = sum(1 for i in interactions if i["task_outcome"] == "unknown")

        responses = conn.execute(
            "SELECT * FROM study_responses WHERE session_id=? ORDER BY response_id",
            (sid,),
        ).fetchall()
        # 撤回的问卷不参与任何判定
        active_responses = [r for r in responses if r["review_state"] != "withdrawn"]
        reviewed = next(
            (r for r in active_responses if r["review_state"] == "coded"), None
        )
        has_response = bool(active_responses)
        reviewed_response = reviewed is not None
        coded_intent = reviewed["coded_intent"] if reviewed else None
        withdrawn = len(active_responses) != len(responses)

        grade = _graded(
            aligned=aligned,
            exposure_state=s["exposure_state"],
            object_count=len(objects),
            has_interaction=bool(interactions),
            has_response=has_response,
            reviewed_response=reviewed_response,
        )

        # 只有 L1 可判定会话的编码才能与当时显示/计划绑定；
        # L2 即使有 coded 问卷，理解度在事件时点仍不可证实，保持 None。
        comprehension: bool | None = None
        if reviewed_response and grade == ObservabilityLevel.L1_CONFIRMED:
            comprehension = coded_intent == CodedIntent.UNDERSTOOD.value

        evidence.append(SessionEvidence(
            session_id=sid,
            driver_key=s["driver_key"],
            ui_version=s["ui_version"],
            route_intent_version=s["route_intent_version"],
            task_type=s["task_type"],
            exposure_state=s["exposure_state"],
            started_at=s["started_at"],
            object_count=len(objects),
            object_source_versions=source_versions,
            confidence_bands=bands,
            has_orphan_object=False,  # 孤儿对象无法挂到会话，在分析报告中单独计数
            has_interaction=bool(interactions),
            tap_count=tap,
            dismiss_count=dismiss,
            none_count=none,
            matched_tap_count=matched_tap,
            mis_tap_count=mis_tap,
            correct_outcomes=correct,
            incorrect_outcomes=incorrect,
            unknown_outcomes=unknown,
            has_response=has_response,
            reviewed_response=reviewed_response,
            withdrawn_response=withdrawn,
            coded_intent=coded_intent,
            aligned=aligned,
            observability=grade.value,
            comprehension_correct=comprehension,
        ))
    return evidence


def orphan_object_count(conn: sqlite3.Connection) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM rendered_objects WHERE is_orphan=1"
    ).fetchone()[0]
