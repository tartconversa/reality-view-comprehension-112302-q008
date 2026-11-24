"""合成数据 -> 导入 -> 分级 -> 分析 -> 决策 -> 计划 的全链路测试。

直接在进程内构建临时数据库，不依赖已安装的包；测试只读自身生成的
合成资料与仓库基线，不访问网络。
"""

from __future__ import annotations

import csv
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from reality_view_comprehension.analysis import run_analysis, export_report
from reality_view_comprehension.decision import (
    DecisionThresholds,
    decide,
)
from reality_view_comprehension.domain import (
    ObservabilityLevel,
    is_version_aligned,
)
from reality_view_comprehension.ingest import import_directory, open_database
from reality_view_comprehension.observability import build_session_evidence
from reality_view_comprehension import planning
from reality_view_comprehension.synthetic import generate_dataset

SEED = 20261124
N_SESSIONS = 30


class SyntheticPipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.paths = generate_dataset(self.tmp / "data", seed=SEED, n_sessions=N_SESSIONS)
        self.db = self.tmp / "rvc.db"
        self.conn = open_database(self.db)
        self.first_report = import_directory(
            self.conn, self.paths["baseline"], note="test 首批"
        )

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    # ---- 合成数据本身 --------------------------------------------------
    def test_synthetic_dataset_is_deterministic(self) -> None:
        other = tempfile.TemporaryDirectory()
        try:
            p1 = generate_dataset(Path(other.name) / "a", seed=SEED, n_sessions=N_SESSIONS)
            p2 = generate_dataset(Path(other.name) / "b", seed=SEED, n_sessions=N_SESSIONS)
            for name in ("baseline", "late"):
                for csv_name in ("view_sessions.csv", "rendered_objects.csv",
                                 "interactions.csv", "study_responses.csv"):
                    self.assertEqual(
                        (p1[name] / csv_name).read_text(encoding="utf-8-sig"),
                        (p2[name] / csv_name).read_text(encoding="utf-8-sig"),
                    )
        finally:
            other.cleanup()

    def test_synthetic_contains_edge_cases(self) -> None:
        sessions = list(csv.DictReader(
            (self.paths["baseline"] / "view_sessions.csv").open(encoding="utf-8-sig")
        ))
        states = {row["exposure_state"] for row in sessions}
        self.assertEqual(states, {"full", "partial", "late"})
        objects = list(csv.DictReader(
            (self.paths["baseline"] / "rendered_objects.csv").open(encoding="utf-8-sig")
        ))
        self.assertIn("bogus-band", {row["confidence_band"] for row in objects})
        orphan_sessions = {row["session_id"] for row in objects} - {
            row["session_id"] for row in sessions
        }
        self.assertTrue(orphan_sessions)

    # ---- 导入 ----------------------------------------------------------
    def test_import_quarantines_invalid_row_and_marks_orphans(self) -> None:
        quarantined = self.conn.execute(
            "SELECT reason FROM quarantine_rows"
        ).fetchall()
        self.assertTrue(any("confidence_band" in row["reason"] for row in quarantined))
        orphans = self.conn.execute(
            "SELECT COUNT(*) FROM rendered_objects WHERE is_orphan=1"
        ).fetchone()[0]
        self.assertGreaterEqual(orphans, 3)
        self.assertEqual(self.first_report.orphan_objects, orphans)

    def test_mis_tap_detection(self) -> None:
        mis = self.conn.execute(
            "SELECT COUNT(*) FROM interactions WHERE is_mis_tap=1"
        ).fetchone()[0]
        self.assertGreaterEqual(mis, 1)
        # none 行为不判误触
        none_mis = self.conn.execute(
            "SELECT COUNT(*) FROM interactions WHERE action='none' AND is_mis_tap=1"
        ).fetchone()[0]
        self.assertEqual(none_mis, 0)

    def test_reimport_is_idempotent_and_logs_conflicts(self) -> None:
        before = self.conn.execute("SELECT COUNT(*) FROM view_sessions").fetchone()[0]
        second = import_directory(self.conn, self.paths["baseline"], note="重复导入")
        after = self.conn.execute("SELECT COUNT(*) FROM view_sessions").fetchone()[0]
        self.assertEqual(before, after)
        self.assertEqual(second.inserted.get("view_sessions.csv", 0), 0)
        self.assertGreaterEqual(second.duplicated.get("view_sessions.csv", 0), before)
        conflicts = self.conn.execute(
            "SELECT DISTINCT reason FROM import_conflicts"
        ).fetchall()
        self.assertTrue(conflicts)

    def test_late_batch_import_does_not_duplicate(self) -> None:
        before = self.conn.execute("SELECT COUNT(*) FROM view_sessions").fetchone()[0]
        import_directory(self.conn, self.paths["late"], note="晚到批次")
        after = self.conn.execute("SELECT COUNT(*) FROM view_sessions").fetchone()[0]
        self.assertEqual(before, after)

    # ---- 分级 ----------------------------------------------------------
    def test_version_alignment_lookup(self) -> None:
        self.assertTrue(is_version_aligned("UI-A", "UI-0", "NAV-0"))
        self.assertTrue(is_version_aligned("UI-B", "UI-2", "NAV-3"))
        self.assertFalse(is_version_aligned("UI-A", "UI-2", "NAV-1"))
        self.assertFalse(is_version_aligned("UI-B", "UI-0", "NAV-0"))
        self.assertFalse(is_version_aligned("UI-X", "UI-0", "NAV-0"))

    def test_observability_grading_invariants(self) -> None:
        evidence = build_session_evidence(self.conn)
        grades = {e.observability for e in evidence}
        # 合成集应同时出现 L1/L2/L3
        self.assertIn(ObservabilityLevel.L1_CONFIRMED.value, grades)
        self.assertIn(ObservabilityLevel.L2_BEHAVIOR_ONLY.value, grades)
        self.assertIn(ObservabilityLevel.L3_LINKAGE_GAP.value, grades)

        for e in evidence:
            # 晚到/部分曝光永远不能评 L1
            if e.exposure_state in ("late", "partial"):
                self.assertNotEqual(e.observability,
                                    ObservabilityLevel.L1_CONFIRMED.value)
            # 只有 L1 才有确定的理解度判定
            if e.observability != ObservabilityLevel.L1_CONFIRMED.value:
                self.assertIsNone(e.comprehension_correct)
            # 错配会话必须降级 L3
            if not e.aligned and e.object_count > 0:
                self.assertEqual(e.observability,
                                 ObservabilityLevel.L3_LINKAGE_GAP.value)
            # L1 必须同时满足对齐、full、已复核编码
            if e.observability == ObservabilityLevel.L1_CONFIRMED.value:
                self.assertTrue(e.aligned)
                self.assertEqual(e.exposure_state, "full")
                self.assertTrue(e.reviewed_response)

    def test_withdrawn_responses_excluded(self) -> None:
        evidence = build_session_evidence(self.conn)
        withdrawn_sessions = {
            row["session_id"] for row in self.conn.execute(
                "SELECT DISTINCT session_id FROM study_responses "
                "WHERE review_state='withdrawn'"
            ).fetchall()
        }
        for e in evidence:
            if e.session_id in withdrawn_sessions:
                self.assertFalse(e.reviewed_response)

    # ---- 分析 ----------------------------------------------------------
    def test_analysis_grouping_and_gap_disclosure(self) -> None:
        report = run_analysis(self.conn)
        self.assertEqual(report.total_sessions, N_SESSIONS)
        # 每个输出分层的分母自洽
        for m in report.group_metrics:
            self.assertGreaterEqual(m.sessions, 1)
            self.assertLessEqual(m.reviewed_sessions, m.sessions)
            self.assertLessEqual(m.understood, m.reviewed_sessions)
            if m.comprehension_rate is not None:
                self.assertGreaterEqual(m.comprehension_rate, 0.0)
                self.assertLessEqual(m.comprehension_rate, 1.0)
        # 覆盖统计与缺口披露
        self.assertIn("by_observability", report.coverage)
        self.assertTrue(report.gaps)
        joined_gaps = "\n".join(report.gaps)
        self.assertIn("问卷", joined_gaps)
        self.assertIn("L3", joined_gaps)

    def test_export_writes_all_artifacts(self) -> None:
        report = run_analysis(self.conn)
        out = self.tmp / "report"
        paths = export_report(report, out)
        for path in paths.values():
            self.assertTrue(path.exists())
            self.assertGreater(path.stat().st_size, 0)
        payload = json.loads(paths["analysis_report"].read_text(encoding="utf-8"))
        self.assertEqual(payload["total_sessions"], N_SESSIONS)
        markdown = paths["markdown"].read_text(encoding="utf-8")
        self.assertIn("理解度边界分析报告", markdown)

    # ---- 决策 ----------------------------------------------------------
    def test_decisions_traceable_and_conservative(self) -> None:
        report = run_analysis(self.conn)
        result = decide(report, DecisionThresholds())
        self.assertIn(result["headline"],
                      {"hold_rollout", "follow_up_study", "restricted_trial"})
        for rec in result["recommendations"]:
            self.assertIn(rec["action"],
                          {"hold_rollout", "follow_up_study", "restricted_trial"})
            self.assertTrue(rec["reason"])
            self.assertTrue(rec["evidence_refs"])
        # 试用资格与个人证据一致：有误触的驾驶员不合格
        for cohort in result["driver_cohorts"]:
            if cohort["mis_tap_sessions"] > 0:
                self.assertFalse(cohort["eligible_for_early_trial"])
        # 合格名单中的驾驶员确实都满足资格字段
        eligible = set(result["eligible_early_trial_drivers"])
        for cohort in result["driver_cohorts"]:
            self.assertEqual(
                cohort["eligible_for_early_trial"],
                cohort["driver_key"] in eligible,
            )

    def test_low_rate_threshold_forces_hold(self) -> None:
        report = run_analysis(self.conn)
        # L1 样本本身稀少 -> 先验证证据不足触发 follow_up_study
        weak = DecisionThresholds(
            min_l1_sessions=99, min_l1_per_task=99,
            min_questionnaire_coverage=0.0,
        )
        weak_result = decide(report, weak)
        self.assertIn("follow_up_study",
                      {rec["action"] for rec in weak_result["recommendations"]})

    def test_low_comprehension_rule_holds_on_constructed_report(self) -> None:
        # 直接构造“L1 证据量充足但理解率不达标”的最小报告，验证规则 3
        from reality_view_comprehension.analysis import AnalysisReport
        from reality_view_comprehension.domain import GroupMetrics
        from reality_view_comprehension.decision import recommend_for_ui

        weak_l1 = GroupMetrics(
            ui_version="UI-A", task_type="turn",
            observability=ObservabilityLevel.L1_CONFIRMED.value,
            sessions=10, feature_present_sessions=10, tap_sessions=10,
            matched_tap_sessions=8, mis_tap_sessions=0,
            responded_sessions=10, reviewed_sessions=10,
            understood=5, misread=4, unclear=1,
            comprehension_rate=0.5, behavior_accuracy=0.8, mis_tap_rate=0.0,
        )
        constructed = AnalysisReport(
            total_sessions=10, orphan_objects=0, quarantined_rows=0,
            group_metrics=[weak_l1], ui_rollups=[weak_l1],
            session_evidence=[],
            coverage={"questionnaire_coverage": 1.0, "by_observability": {}},
            gaps=[],
        )
        thresholds = DecisionThresholds(
            min_l1_sessions=8, min_l1_per_task=2,
            min_comprehension_rate=0.75, max_mis_tap_rate=0.15,
            min_questionnaire_coverage=0.6,
        )
        recs = recommend_for_ui(constructed, "UI-A", thresholds)
        self.assertIn("hold_rollout", {r.action for r in recs})
        # 建议必须回链证据来源
        hold = next(r for r in recs if r.action == "hold_rollout")
        self.assertTrue(hold.evidence_refs)
        self.assertIn("50.0%", hold.reason)


class BaselineImportTests(unittest.TestCase):
    """仓库基线 data/ 必须能被应用直接消费。"""

    def test_baseline_pipeline(self) -> None:
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            conn = open_database(Path(tmp) / "base.db")
            try:
                report = import_directory(conn, root / "data")
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM view_sessions").fetchone()[0], 50
                )
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM interactions").fetchone()[0], 44
                )
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM study_responses").fetchone()[0], 23
                )
                self.assertEqual(report.orphan_objects, 10)
                analysis = run_analysis(conn)
                self.assertEqual(analysis.total_sessions, 50)
                # 基线是混合场景：部分会话版本对齐，部分因未登记组合错配降级 L3
                aligned = [e for e in analysis.session_evidence if e.aligned]
                misaligned = [
                    e for e in analysis.session_evidence
                    if e.object_count > 0 and not e.aligned
                ]
                self.assertTrue(aligned)
                self.assertTrue(misaligned)
                self.assertTrue(all(
                    e.observability == ObservabilityLevel.L3_LINKAGE_GAP.value
                    for e in misaligned
                ))
                # 只有 full 曝光、对齐且有 coded 问卷的会话才能评 L1
                self.assertTrue(all(
                    e.exposure_state == "full" and e.reviewed_response
                    for e in analysis.session_evidence
                    if e.observability == ObservabilityLevel.L1_CONFIRMED.value
                ))
            finally:
                conn.close()


class PlanningTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.conn = open_database(Path(self._tmp.name) / "plan.db")
        planning.ensure_planning_tables(self.conn)
        planning.register_device(self.conn, "DEV-1", "rig")
        planning.register_route(self.conn, "RT-1", "路线1", "turn", "UI-B")
        planning.grant_authorization(self.conn, "D-1", "trial")

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def test_full_task_lifecycle_and_freeze(self) -> None:
        planning.create_task(
            self.conn, "T-1", "UI-B", "NAV-1", "turn",
            route_id="RT-1", device_id="DEV-1", driver_key="D-1",
        )
        for status in ("scheduled", "active", "completed"):
            planning.transition_task(self.conn, "T-1", status)
        row = self.conn.execute(
            "SELECT * FROM study_tasks WHERE task_id='T-1'"
        ).fetchone()
        self.assertEqual(row["status"], "completed")
        self.assertEqual(row["frozen_ui_version"], "UI-B")
        self.assertEqual(row["frozen_intent_version"], "NAV-1")
        # 终态不可再迁移
        with self.assertRaises(planning.PlanningError):
            planning.transition_task(self.conn, "T-1", "superseded")
        # 设备在完成后释放
        self.assertEqual(
            self.conn.execute(
                "SELECT status FROM devices WHERE device_id='DEV-1'"
            ).fetchone()["status"],
            "available",
        )

    def test_config_change_supersedes_open_tasks_but_keeps_completed(self) -> None:
        planning.create_task(self.conn, "T-OPEN", "UI-B", "NAV-1", "turn",
                             route_id="RT-1", driver_key="D-1")
        planning.create_task(self.conn, "T-DONE", "UI-B", "NAV-3", "turn",
                             route_id="RT-1", driver_key="D-1")
        planning.transition_task(self.conn, "T-DONE", "scheduled")
        planning.transition_task(self.conn, "T-DONE", "active")
        planning.transition_task(self.conn, "T-DONE", "completed")

        superseded = planning.apply_config_change(
            self.conn, "EPOCH-9", "UI-C", "界面升级"
        )
        self.assertEqual(superseded, ["T-OPEN"])
        open_row = self.conn.execute(
            "SELECT status FROM study_tasks WHERE task_id='T-OPEN'"
        ).fetchone()
        done_row = self.conn.execute(
            "SELECT status, frozen_ui_version FROM study_tasks WHERE task_id='T-DONE'"
        ).fetchone()
        self.assertEqual(open_row["status"], "superseded")
        self.assertEqual(done_row["status"], "completed")
        self.assertEqual(done_row["frozen_ui_version"], "UI-B")
        # 纪元只追加
        with self.assertRaises(planning.PlanningError):
            planning.register_config_epoch(self.conn, "EPOCH-9", "UI-C", "重复")

    def test_authorization_guard(self) -> None:
        planning.withdraw_authorization(self.conn, "D-1", "参与者退出")
        with self.assertRaises(planning.PlanningError):
            planning.create_task(
                self.conn, "T-X", "UI-B", "NAV-1", "turn",
                route_id="RT-1", driver_key="D-1",
            )
        with self.assertRaises(planning.PlanningError):
            planning.create_task(
                self.conn, "T-Y", "UI-B", "NAV-1", "turn",
                route_id="RT-1", driver_key="D-NOBODY",
            )

    def test_route_ui_mismatch_rejected(self) -> None:
        with self.assertRaises(planning.PlanningError):
            planning.create_task(
                self.conn, "T-Z", "UI-A", "NAV-0", "turn",
                route_id="RT-1", driver_key="D-1",
            )


if __name__ == "__main__":
    unittest.main()
