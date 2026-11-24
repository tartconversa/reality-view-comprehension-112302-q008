"""命令行入口：导入、分析、决策、计划维护与合成数据。

示例：
    python -m reality_view_comprehension import --db work/rvc.db --data-dir data
    python -m reality_view_comprehension analyze --db work/rvc.db --out work/report
    python -m reality_view_comprehension decide --db work/rvc.db
    python -m reality_view_comprehension synthetic --out work/synthetic
    python -m reality_view_comprehension demo --workdir work/demo
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from . import planning
from .analysis import export_report, run_analysis
from .decision import DecisionThresholds, decide
from .domain import TASK_TYPES, UI_VERSIONS
from .ingest import import_directory, open_database
from .synthetic import generate_dataset


def _connect(args: argparse.Namespace) -> sqlite3.Connection:
    return open_database(args.db)


def cmd_import(args: argparse.Namespace) -> int:
    conn = _connect(args)
    try:
        report = import_directory(conn, args.data_dir, note=args.note)
        for line in report.summary_lines():
            print(line)
        conflicts = conn.execute("SELECT COUNT(*) FROM import_conflicts").fetchone()[0]
        print(f"  累计冲突登记：{conflicts}")
        return 0
    finally:
        conn.close()


def cmd_analyze(args: argparse.Namespace) -> int:
    conn = _connect(args)
    try:
        report = run_analysis(conn)
        paths = export_report(report, args.out)
        print(f"分析完成：会话 {report.total_sessions} 个，孤儿对象 {report.orphan_objects} 条")
        print("证据缺口：")
        for gap in report.gaps:
            print(f"  - {gap}")
        print("导出文件：")
        for kind, path in paths.items():
            print(f"  - {kind}: {path}")
        return 0
    finally:
        conn.close()


def cmd_decide(args: argparse.Namespace) -> int:
    conn = _connect(args)
    try:
        report = run_analysis(conn)
        thresholds = DecisionThresholds(
            min_l1_sessions=args.min_l1_sessions,
            min_l1_per_task=args.min_l1_per_task,
            min_comprehension_rate=args.min_comprehension_rate,
            max_mis_tap_rate=args.max_mis_tap_rate,
            min_questionnaire_coverage=args.min_questionnaire_coverage,
        )
        result = decide(report, thresholds)
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        print(f"总体建议（最优先）：{result['headline']}")
        print("建议清单：")
        for rec in result["recommendations"]:
            print(f"  [{rec['action']}] {rec['scope']}")
            print(f"      依据：{rec['reason']}")
            print(f"      回链：{', '.join(rec['evidence_refs'])}")
        print("早期试用驾驶员资格：")
        for cohort in result["driver_cohorts"]:
            mark = "✓" if cohort["eligible_for_early_trial"] else "✗"
            rate = cohort["comprehension_rate"]
            rate_text = "—" if rate is None else f"{rate:.0%}"
            print(
                f"  {mark} {cohort['driver_key']}：会话 {cohort['sessions']}，"
                f"L1 {cohort['l1_sessions']}，理解率 {rate_text} —— "
                f"{cohort['eligibility_reason']}"
            )
        print(f"可进入早期试用名单：{', '.join(result['eligible_early_trial_drivers']) or '（无）'}")
        return 0
    finally:
        conn.close()


def cmd_synthetic(args: argparse.Namespace) -> int:
    paths = generate_dataset(
        args.out, seed=args.seed, n_sessions=args.n_sessions,
        include_edge_cases=not args.no_edge_cases,
    )
    print("合成数据已生成（固定种子，可复现）：")
    for kind, path in paths.items():
        print(f"  - {kind}: {path}")
    return 0


# ---------------------------------------------------------------------------
# 计划维护子命令
# ---------------------------------------------------------------------------
def cmd_plan_device(args: argparse.Namespace) -> int:
    conn = _connect(args)
    try:
        planning.register_device(conn, args.device_id, args.model, args.note)
        print(f"设备 {args.device_id} 已登记")
        return 0
    finally:
        conn.close()


def cmd_plan_route(args: argparse.Namespace) -> int:
    conn = _connect(args)
    try:
        planning.register_route(
            conn, args.route_id, args.name, args.task_type, args.ui_version, args.difficulty
        )
        print(f"模拟路线 {args.route_id} 已登记")
        return 0
    finally:
        conn.close()


def cmd_plan_grant(args: argparse.Namespace) -> int:
    conn = _connect(args)
    try:
        planning.grant_authorization(conn, args.driver_key, args.scope, args.expires_at)
        print(f"参与者 {args.driver_key} 授权已登记（范围：{args.scope}）")
        return 0
    finally:
        conn.close()


def cmd_plan_withdraw(args: argparse.Namespace) -> int:
    conn = _connect(args)
    try:
        planning.withdraw_authorization(conn, args.driver_key, args.note)
        print(f"参与者 {args.driver_key} 授权已撤回")
        return 0
    finally:
        conn.close()


def cmd_plan_create(args: argparse.Namespace) -> int:
    conn = _connect(args)
    try:
        planning.create_task(
            conn, args.task_id, args.ui_version, args.intent_version, args.task_type,
            route_id=args.route_id, device_id=args.device_id, driver_key=args.driver_key,
            config_epoch_id=args.epoch, note=args.note,
        )
        print(f"任务 {args.task_id} 已创建（planned）")
        return 0
    finally:
        conn.close()


def cmd_plan_transition(args: argparse.Namespace) -> int:
    conn = _connect(args)
    try:
        planning.transition_task(conn, args.task_id, args.status)
        print(f"任务 {args.task_id} 已迁移到 {args.status}")
        return 0
    finally:
        conn.close()


def cmd_plan_epoch(args: argparse.Namespace) -> int:
    conn = _connect(args)
    try:
        superseded = planning.apply_config_change(conn, args.epoch_id, args.ui_version, args.note)
        print(f"配置纪元 {args.epoch_id} 已登记（显示版本 {args.ui_version}）")
        if superseded:
            print("被替代的未完成任务（已完成回答的版本封存不变）：")
            for task_id in superseded:
                print(f"  - {task_id}")
        else:
            print("没有受影响的未完成任务")
        return 0
    finally:
        conn.close()


def cmd_plan_list(args: argparse.Namespace) -> int:
    conn = _connect(args)
    try:
        overview = planning.list_planning_overview(conn)
        print(json.dumps(overview, ensure_ascii=False, indent=2))
        return 0
    finally:
        conn.close()


def cmd_demo(args: argparse.Namespace) -> int:
    """端到端演示：合成 -> 导入（含晚到批次）-> 分析 -> 决策 -> 计划维护。"""
    workdir = Path(args.workdir)
    db_path = workdir / "rvc.db"
    if db_path.exists() and not args.force:
        print(f"{db_path} 已存在；如需重新演示请加 --force")
        return 1
    if db_path.exists():
        db_path.unlink()
    synthetic_dir = workdir / "synthetic"
    report_dir = workdir / "report"

    print("== 1. 生成合成数据 ==")
    paths = generate_dataset(synthetic_dir, seed=args.seed, n_sessions=args.n_sessions)
    print(f"   baseline: {paths['baseline']}")
    print(f"   late:     {paths['late']}")

    print("== 2. 首批导入 ==")
    conn = open_database(db_path)
    first = import_directory(conn, paths["baseline"], note="demo 首批")
    for line in first.summary_lines():
        print("   " + line)

    print("== 3. 晚到批次重复导入（幂等，冲突登记） ==")
    second = import_directory(conn, paths["late"], note="demo 晚到补齐")
    for line in second.summary_lines():
        print("   " + line)
    conn.close()

    print("== 4. 分析与导出 ==")
    conn = open_database(db_path)
    report = run_analysis(conn)
    exported = export_report(report, report_dir)
    print(f"   报告目录：{report_dir}")
    for kind in ("markdown", "group_metrics", "session_evidence"):
        print(f"   - {kind}: {exported[kind]}")

    print("== 5. 开放决策 ==")
    result = decide(report)
    print(f"   最优先建议：{result['headline']}")
    for rec in result["recommendations"][:5]:
        print(f"   [{rec['action']}] {rec['scope']} — {rec['reason']}")

    print("== 6. 计划维护：设备/路线/授权/任务/配置变更 ==")
    planning.ensure_planning_tables(conn)
    planning.register_device(conn, "DEV-01", "sim-rig-A")
    planning.register_route(conn, "RT-01", "合成绕城路线", "turn", "UI-B")
    planning.grant_authorization(conn, "D-01", "simulated-trial")
    planning.create_task(
        conn, "TASK-001", "UI-B", "NAV-1", "turn",
        route_id="RT-01", device_id="DEV-01", driver_key="D-01",
    )
    planning.transition_task(conn, "TASK-001", "scheduled")
    planning.transition_task(conn, "TASK-001", "active")
    planning.transition_task(conn, "TASK-001", "completed")
    planning.register_route(conn, "RT-02", "合成急刹路线", "hazard", "UI-A")
    # 演示路线/显示版本一致性校验：错误组合应被拒绝
    try:
        planning.create_task(
            conn, "TASK-002", "UI-A", "NAV-2", "hazard",
            route_id="RT-01", driver_key="D-01",
        )
    except planning.PlanningError as exc:
        print(f"   规则拦截（符合预期）：{exc}")
    planning.create_task(
        conn, "TASK-002", "UI-A", "NAV-2", "hazard",
        route_id="RT-02", driver_key="D-01",
    )
    superseded = planning.apply_config_change(
        conn, "EPOCH-02", "UI-C", "演示：界面配置升级，旧显示版本任务不再适用"
    )
    print(f"   配置变更后被替代任务：{superseded or '（无）'}")
    frozen = conn.execute(
        "SELECT task_id, status, frozen_ui_version, frozen_intent_version "
        "FROM study_tasks WHERE task_id='TASK-001'"
    ).fetchone()
    print(
        f"   已完成任务封存：{frozen['task_id']} / {frozen['status']} / "
        f"{frozen['frozen_ui_version']} / {frozen['frozen_intent_version']}"
    )
    conn.close()
    print("演示完成。")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="reality_view_comprehension",
        description="Reality View 理解度证据分析与研究/试行计划维护（本地合成资料）",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_import = sub.add_parser("import", help="导入一个资料目录到 SQLite")
    p_import.add_argument("--db", required=True)
    p_import.add_argument("--data-dir", required=True)
    p_import.add_argument("--note", default="")
    p_import.set_defaults(func=cmd_import)

    p_analyze = sub.add_parser("analyze", help="运行理解度边界分析并导出报告")
    p_analyze.add_argument("--db", required=True)
    p_analyze.add_argument("--out", required=True)
    p_analyze.set_defaults(func=cmd_analyze)

    p_decide = sub.add_parser("decide", help="输出补做/限制/暂缓建议与试用名单")
    p_decide.add_argument("--db", required=True)
    p_decide.add_argument("--json", action="store_true", help="输出 JSON 而非中文文本")
    p_decide.add_argument("--min-l1-sessions", type=int, default=8)
    p_decide.add_argument("--min-l1-per-task", type=int, default=2)
    p_decide.add_argument("--min-comprehension-rate", type=float, default=0.75)
    p_decide.add_argument("--max-mis-tap-rate", type=float, default=0.15)
    p_decide.add_argument("--min-questionnaire-coverage", type=float, default=0.60)
    p_decide.set_defaults(func=cmd_decide)

    p_syn = sub.add_parser("synthetic", help="生成可复现合成数据")
    p_syn.add_argument("--out", required=True)
    p_syn.add_argument("--seed", type=int, default=20261124)
    p_syn.add_argument("--n-sessions", type=int, default=30)
    p_syn.add_argument("--no-edge-cases", action="store_true")
    p_syn.set_defaults(func=cmd_synthetic)

    p_demo = sub.add_parser("demo", help="端到端演示")
    p_demo.add_argument("--workdir", required=True)
    p_demo.add_argument("--seed", type=int, default=20261124)
    p_demo.add_argument("--n-sessions", type=int, default=30)
    p_demo.add_argument("--force", action="store_true")
    p_demo.set_defaults(func=cmd_demo)

    # --- 计划维护 ---
    p_dev = sub.add_parser("plan-device", help="登记研究设备")
    p_dev.add_argument("--db", required=True)
    p_dev.add_argument("--device-id", required=True)
    p_dev.add_argument("--model", required=True)
    p_dev.add_argument("--note", default="")
    p_dev.set_defaults(func=cmd_plan_device)

    p_route = sub.add_parser("plan-route", help="登记模拟路线")
    p_route.add_argument("--db", required=True)
    p_route.add_argument("--route-id", required=True)
    p_route.add_argument("--name", required=True)
    p_route.add_argument("--task-type", required=True, choices=TASK_TYPES)
    p_route.add_argument("--ui-version", required=True, choices=UI_VERSIONS)
    p_route.add_argument("--difficulty", default="medium")
    p_route.set_defaults(func=cmd_plan_route)

    p_grant = sub.add_parser("plan-grant", help="登记参与者授权")
    p_grant.add_argument("--db", required=True)
    p_grant.add_argument("--driver-key", required=True)
    p_grant.add_argument("--scope", required=True)
    p_grant.add_argument("--expires-at", default=None)
    p_grant.set_defaults(func=cmd_plan_grant)

    p_withdraw = sub.add_parser("plan-withdraw", help="撤回参与者授权")
    p_withdraw.add_argument("--db", required=True)
    p_withdraw.add_argument("--driver-key", required=True)
    p_withdraw.add_argument("--note", required=True)
    p_withdraw.set_defaults(func=cmd_plan_withdraw)

    p_create = sub.add_parser("plan-create", help="创建研究任务")
    p_create.add_argument("--db", required=True)
    p_create.add_argument("--task-id", required=True)
    p_create.add_argument("--ui-version", required=True)
    p_create.add_argument("--intent-version", required=True)
    p_create.add_argument("--task-type", required=True)
    p_create.add_argument("--route-id", default=None)
    p_create.add_argument("--device-id", default=None)
    p_create.add_argument("--driver-key", default=None)
    p_create.add_argument("--epoch", default=None)
    p_create.add_argument("--note", default="")
    p_create.set_defaults(func=cmd_plan_create)

    p_trans = sub.add_parser("plan-transition", help="推进任务状态")
    p_trans.add_argument("--db", required=True)
    p_trans.add_argument("--task-id", required=True)
    p_trans.add_argument("--status", required=True,
                         choices=[s.value for s in planning.TaskStatus])
    p_trans.set_defaults(func=cmd_plan_transition)

    p_epoch = sub.add_parser("plan-epoch", help="登记配置纪元并替代旧任务")
    p_epoch.add_argument("--db", required=True)
    p_epoch.add_argument("--epoch-id", required=True)
    p_epoch.add_argument("--ui-version", required=True)
    p_epoch.add_argument("--note", default="")
    p_epoch.set_defaults(func=cmd_plan_epoch)

    p_list = sub.add_parser("plan-list", help="输出全部计划对象（JSON）")
    p_list.add_argument("--db", required=True)
    p_list.set_defaults(func=cmd_plan_list)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except planning.PlanningError as exc:
        print(f"计划规则拦截：{exc}", file=sys.stderr)
        return 2
    except FileNotFoundError as exc:
        print(f"资料错误：{exc}", file=sys.stderr)
        return 2
    except sqlite3.Error as exc:
        print(f"数据库错误：{exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
