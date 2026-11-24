"""研究与试行计划维护。

运营对象
--------
- 设备 devices：研究用车机/测试设备及其状态；
- 模拟路线 simulated_routes：任务使用的虚构路线与适用显示版本；
- 参与者授权 participant_authorizations：授权范围与有效期，撤回可审计；
- 研究任务 study_tasks：任务按状态机推进，完成后冻结；
- 配置纪元 config_epochs：界面配置变化登记。

版本封存规则（硬约束）
----------------------
计划开始后界面配置变化：

1. 新配置写入 config_epochs，永不回改历史；
2. 受影响的进行中任务标记为 ``superseded``（不再适用），其已完成回答
   仍保留首次导入时的显示版本与路线意图版本（见 ingest 幂等冲突表）；
3. view_sessions / study_responses 的版本字段没有任何更新路径，
   分析始终按会话自身版本解释。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum

PLANNING_SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
    device_id     TEXT PRIMARY KEY,
    device_model  TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'available',
    note          TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS simulated_routes (
    route_id       TEXT PRIMARY KEY,
    route_name     TEXT NOT NULL,
    task_type      TEXT NOT NULL,
    ui_version     TEXT NOT NULL,
    difficulty     TEXT NOT NULL DEFAULT 'medium',
    created_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS participant_authorizations (
    driver_key       TEXT PRIMARY KEY,
    scope            TEXT NOT NULL,
    granted_at       TEXT NOT NULL,
    expires_at       TEXT,
    status           TEXT NOT NULL DEFAULT 'granted',  -- granted | withdrawn | expired
    withdrawal_note  TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS study_tasks (
    task_id           TEXT PRIMARY KEY,
    ui_version        TEXT NOT NULL,
    route_intent_version TEXT NOT NULL,
    task_type         TEXT NOT NULL,
    route_id          TEXT REFERENCES simulated_routes(route_id),
    device_id         TEXT REFERENCES devices(device_id),
    driver_key        TEXT,
    status            TEXT NOT NULL DEFAULT 'planned',
    config_epoch_id   TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    completed_at      TEXT,
    frozen_ui_version TEXT,
    frozen_intent_version TEXT,
    note              TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS task_events (
    event_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id       TEXT NOT NULL REFERENCES study_tasks(task_id),
    event_type    TEXT NOT NULL,
    event_at      TEXT NOT NULL,
    detail        TEXT NOT NULL DEFAULT ''
);
"""


class TaskStatus(str, Enum):
    PLANNED = "planned"
    SCHEDULED = "scheduled"
    ACTIVE = "active"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    SUPERSEDED = "superseded"


# 允许的状态迁移；completed/superseded/cancelled 是终态。
_ALLOWED_TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.PLANNED: frozenset(
        {TaskStatus.SCHEDULED, TaskStatus.CANCELLED, TaskStatus.SUPERSEDED}
    ),
    TaskStatus.SCHEDULED: frozenset(
        {TaskStatus.ACTIVE, TaskStatus.CANCELLED, TaskStatus.SUPERSEDED}
    ),
    TaskStatus.ACTIVE: frozenset(
        {TaskStatus.COMPLETED, TaskStatus.CANCELLED, TaskStatus.SUPERSEDED}
    ),
    TaskStatus.COMPLETED: frozenset(),
    TaskStatus.CANCELLED: frozenset(),
    TaskStatus.SUPERSEDED: frozenset(),
}


class PlanningError(RuntimeError):
    """计划维护规则被违反时抛出，消息面向运营人员（中文）。"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_planning_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(PLANNING_SCHEMA)
    conn.commit()


def _log_event(conn: sqlite3.Connection, task_id: str, event_type: str, detail: str = "") -> None:
    conn.execute(
        "INSERT INTO task_events(task_id, event_type, event_at, detail) VALUES (?, ?, ?, ?)",
        (task_id, event_type, _now(), detail),
    )


# ---------------------------------------------------------------------------
# 设备 / 路线 / 授权
# ---------------------------------------------------------------------------
def register_device(conn: sqlite3.Connection, device_id: str, device_model: str,
                    note: str = "") -> None:
    ensure_planning_tables(conn)
    conn.execute(
        "INSERT INTO devices(device_id, device_model, status, note, created_at) "
        "VALUES (?, ?, 'available', ?, ?)",
        (device_id, device_model, note, _now()),
    )
    conn.commit()


def register_route(conn: sqlite3.Connection, route_id: str, route_name: str,
                   task_type: str, ui_version: str, difficulty: str = "medium") -> None:
    ensure_planning_tables(conn)
    conn.execute(
        "INSERT INTO simulated_routes(route_id, route_name, task_type, ui_version, "
        "difficulty, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (route_id, route_name, task_type, ui_version, difficulty, _now()),
    )
    conn.commit()


def grant_authorization(conn: sqlite3.Connection, driver_key: str, scope: str,
                        expires_at: str | None = None) -> None:
    ensure_planning_tables(conn)
    row = conn.execute(
        "SELECT status FROM participant_authorizations WHERE driver_key=?", (driver_key,)
    ).fetchone()
    if row and row["status"] != "withdrawn":
        raise PlanningError(f"参与者 {driver_key} 已存在授权（{row['status']}），无需重复登记")
    conn.execute(
        "INSERT INTO participant_authorizations(driver_key, scope, granted_at, expires_at, status) "
        "VALUES (?, ?, ?, ?, 'granted')",
        (driver_key, scope, _now(), expires_at),
    )
    conn.commit()


def withdraw_authorization(conn: sqlite3.Connection, driver_key: str, note: str) -> None:
    ensure_planning_tables(conn)
    row = conn.execute(
        "SELECT status FROM participant_authorizations WHERE driver_key=?", (driver_key,)
    ).fetchone()
    if not row:
        raise PlanningError(f"参与者 {driver_key} 没有授权记录，无法撤回")
    conn.execute(
        "UPDATE participant_authorizations SET status='withdrawn', withdrawal_note=? "
        "WHERE driver_key=?",
        (note, driver_key),
    )
    conn.commit()


def _assert_authorized(conn: sqlite3.Connection, driver_key: str) -> None:
    row = conn.execute(
        "SELECT status, expires_at FROM participant_authorizations WHERE driver_key=?",
        (driver_key,),
    ).fetchone()
    if not row:
        raise PlanningError(f"参与者 {driver_key} 缺少授权，不能安排任务")
    if row["status"] == "withdrawn":
        raise PlanningError(f"参与者 {driver_key} 已撤回授权，不能安排任务")
    if row["expires_at"] and row["expires_at"] < _now():
        raise PlanningError(f"参与者 {driver_key} 授权已过期，请先续期")


# ---------------------------------------------------------------------------
# 配置纪元与任务
# ---------------------------------------------------------------------------
def register_config_epoch(conn: sqlite3.Connection, epoch_id: str, ui_version: str,
                          note: str = "") -> str:
    """登记一次界面配置变化。只追加，不修改历史纪元。"""
    ensure_planning_tables(conn)
    try:
        conn.execute(
            "INSERT INTO config_epochs(epoch_id, effective_at, ui_version, note) "
            "VALUES (?, ?, ?, ?)",
            (epoch_id, _now(), ui_version, note),
        )
        conn.commit()
    except sqlite3.IntegrityError as exc:
        raise PlanningError(f"配置纪元 {epoch_id} 已存在，纪元不可重复登记") from exc
    return epoch_id


def create_task(conn: sqlite3.Connection, task_id: str, ui_version: str,
                route_intent_version: str, task_type: str,
                route_id: str | None = None, device_id: str | None = None,
                driver_key: str | None = None, config_epoch_id: str | None = None,
                note: str = "") -> None:
    ensure_planning_tables(conn)
    if route_id is not None:
        route = conn.execute(
            "SELECT ui_version FROM simulated_routes WHERE route_id=?", (route_id,)
        ).fetchone()
        if not route:
            raise PlanningError(f"模拟路线 {route_id} 不存在")
        if route["ui_version"] != ui_version:
            raise PlanningError(
                f"路线 {route_id} 适用 {route['ui_version']}，与任务显示版本 {ui_version} 不一致"
            )
    if device_id is not None:
        device = conn.execute(
            "SELECT status FROM devices WHERE device_id=?", (device_id,)
        ).fetchone()
        if not device:
            raise PlanningError(f"设备 {device_id} 不存在")
        if device["status"] != "available":
            raise PlanningError(f"设备 {device_id} 当前状态 {device['status']}，不可安排")
    if driver_key is not None:
        _assert_authorized(conn, driver_key)
    if config_epoch_id is not None:
        epoch = conn.execute(
            "SELECT 1 FROM config_epochs WHERE epoch_id=?", (config_epoch_id,)
        ).fetchone()
        if not epoch:
            raise PlanningError(f"配置纪元 {config_epoch_id} 不存在")
    now = _now()
    conn.execute(
        "INSERT INTO study_tasks(task_id, ui_version, route_intent_version, task_type, "
        "route_id, device_id, driver_key, status, config_epoch_id, created_at, updated_at, note) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 'planned', ?, ?, ?, ?)",
        (task_id, ui_version, route_intent_version, task_type, route_id, device_id,
         driver_key, config_epoch_id, now, now, note),
    )
    _log_event(conn, task_id, "created", f"显示版本 {ui_version} / 意图版本 {route_intent_version}")
    conn.commit()


def transition_task(conn: sqlite3.Connection, task_id: str, new_status: str) -> None:
    """按状态机推进任务；终态任务拒绝任何后续迁移。"""
    ensure_planning_tables(conn)
    row = conn.execute("SELECT status FROM study_tasks WHERE task_id=?", (task_id,)).fetchone()
    if not row:
        raise PlanningError(f"任务 {task_id} 不存在")
    current = TaskStatus(row["status"])
    target = TaskStatus(new_status)
    if target not in _ALLOWED_TRANSITIONS[current]:
        raise PlanningError(
            f"任务 {task_id} 不能从 {current.value} 迁移到 {target.value}；"
            f"终态任务（completed/superseded/cancelled）不可变更"
        )
    now = _now()
    if target == TaskStatus.COMPLETED:
        # 完成即冻结当时显示与意图版本，此后配置变化与重复导入都不能改写
        conn.execute(
            "UPDATE study_tasks SET status=?, updated_at=?, completed_at=?, "
            "frozen_ui_version=ui_version, frozen_intent_version=route_intent_version "
            "WHERE task_id=?",
            (target.value, now, now, task_id),
        )
        device_row = conn.execute(
            "SELECT device_id FROM study_tasks WHERE task_id=?", (task_id,)
        ).fetchone()
        if device_row and device_row["device_id"]:
            conn.execute(
                "UPDATE devices SET status='available' WHERE device_id=?",
                (device_row["device_id"],),
            )
    else:
        conn.execute(
            "UPDATE study_tasks SET status=?, updated_at=? WHERE task_id=?",
            (target.value, now, task_id),
        )
    _log_event(conn, task_id, f"transition->{target.value}")
    conn.commit()


def apply_config_change(conn: sqlite3.Connection, epoch_id: str, ui_version: str,
                        note: str) -> list[str]:
    """登记新配置纪元，并把受影响的未完成任务置为 superseded。

    已完成任务保持 completed，其冻结版本不变；返回被替代的任务 ID 列表。
    """
    ensure_planning_tables(conn)
    register_config_epoch(conn, epoch_id, ui_version, note)
    active_rows = conn.execute(
        "SELECT task_id FROM study_tasks "
        "WHERE status IN ('planned', 'scheduled', 'active') AND ui_version != ?",
        (ui_version,),
    ).fetchall()
    superseded = [r["task_id"] for r in active_rows]
    now = _now()
    for task_id in superseded:
        conn.execute(
            "UPDATE study_tasks SET status='superseded', updated_at=?, config_epoch_id=? "
            "WHERE task_id=?",
            (now, epoch_id, task_id),
        )
        _log_event(
            conn, task_id, "superseded-by-config",
            f"配置纪元 {epoch_id} 将显示版本切换为 {ui_version}，原任务不再适用；"
            "已完成回答的显示/意图版本保持首次导入记录",
        )
    conn.commit()
    return superseded


def list_tasks(conn: sqlite3.Connection, status: str | None = None) -> list[sqlite3.Row]:
    ensure_planning_tables(conn)
    if status:
        return list(conn.execute(
            "SELECT * FROM study_tasks WHERE status=? ORDER BY task_id", (status,)
        ))
    return list(conn.execute("SELECT * FROM study_tasks ORDER BY task_id"))


def list_planning_overview(conn: sqlite3.Connection) -> dict[str, list[dict]]:
    ensure_planning_tables(conn)
    def rows(table: str) -> list[dict]:
        return [dict(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY 1")]
    return {
        "devices": rows("devices"),
        "simulated_routes": rows("simulated_routes"),
        "authorizations": rows("participant_authorizations"),
        "tasks": rows("study_tasks"),
        "config_epochs": rows("config_epochs"),
        "task_events": rows("task_events"),
    }
