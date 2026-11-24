"""资料导入：把四类 CSV 持久化到 SQLite，并保留来源边界。

设计要点
--------
- 可持续/重复导入：按主键幂等，重复导入不覆盖已封存的版本字段，冲突登记到
  ``import_conflicts``，返回导入报告而不是静默改写。
- 校验隔离：枚举值非法、外键缺失、字段空缺的行写入 ``quarantine_rows``，
  有效行正常入库；缺失覆盖、待复核、撤回等是有效业务事实，不进隔离区。
- 关联标记：渲染对象找不到会话 -> 孤儿对象；点击目标与本会话渲染对象
  不匹配 -> 误触。两者都保留原始行，只加标记列。
- 晚到曝光：``exposure_state='late'`` 原样保留，分析层据此限制可观察程度，
  导入顺序不影响结果（按会话重算标记）。
"""

from __future__ import annotations

import csv
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .domain import (
    Action,
    ConfidenceBand,
    CodedIntent,
    ExposureState,
    ReviewState,
    TaskOutcome,
)

SCHEMA_VERSION = 1

SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS import_runs (
    run_id          TEXT PRIMARY KEY,
    imported_at     TEXT NOT NULL,
    data_dir        TEXT NOT NULL,
    note            TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS import_conflicts (
    conflict_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          TEXT NOT NULL REFERENCES import_runs(run_id),
    source_file     TEXT NOT NULL,
    primary_key     TEXT NOT NULL,
    reason          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS quarantine_rows (
    quarantine_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          TEXT NOT NULL REFERENCES import_runs(run_id),
    source_file     TEXT NOT NULL,
    primary_key     TEXT NOT NULL,
    reason          TEXT NOT NULL,
    raw_row         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS view_sessions (
    session_id            TEXT PRIMARY KEY,
    driver_key            TEXT NOT NULL,
    ui_version            TEXT NOT NULL,
    started_at            TEXT NOT NULL,
    exposure_state        TEXT NOT NULL,
    route_intent_version  TEXT NOT NULL,
    task_type             TEXT NOT NULL,
    first_seen_run        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rendered_objects (
    object_id        TEXT NOT NULL,
    session_id       TEXT,            -- 孤儿对象允许为空关联
    object_type      TEXT NOT NULL,
    frame_time       TEXT NOT NULL,
    source_version   TEXT NOT NULL,
    confidence_band  TEXT NOT NULL,
    is_orphan        INTEGER NOT NULL DEFAULT 0,
    first_seen_run   TEXT NOT NULL,
    PRIMARY KEY (object_id, session_id, frame_time)
);

CREATE TABLE IF NOT EXISTS interactions (
    interaction_id   TEXT PRIMARY KEY,
    session_id       TEXT NOT NULL,
    target_id        TEXT NOT NULL,
    action           TEXT NOT NULL,
    action_time      TEXT NOT NULL,
    task_outcome     TEXT NOT NULL,
    is_mis_tap       INTEGER NOT NULL DEFAULT 0,
    first_seen_run   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS study_responses (
    response_id      TEXT PRIMARY KEY,
    session_id       TEXT NOT NULL,
    questionnaire    TEXT NOT NULL,
    submitted_at     TEXT NOT NULL,
    coded_intent     TEXT NOT NULL,
    review_state     TEXT NOT NULL,
    first_seen_run   TEXT NOT NULL
);

-- 计划开始后界面配置变化：每个会话固定其显示与意图版本，
-- 分析始终读取会话自身记录，不被后续配置纪元影响。
CREATE TABLE IF NOT EXISTS config_epochs (
    epoch_id         TEXT PRIMARY KEY,
    effective_at     TEXT NOT NULL,
    ui_version       TEXT NOT NULL,
    note             TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_objects_session ON rendered_objects(session_id);
CREATE INDEX IF NOT EXISTS idx_interactions_session ON interactions(session_id);
CREATE INDEX IF NOT EXISTS idx_responses_session ON study_responses(session_id);
"""

_FILES = (
    "view_sessions.csv",
    "rendered_objects.csv",
    "interactions.csv",
    "study_responses.csv",
)

_EXPOSURE = {s.value for s in ExposureState}
_ACTIONS = {a.value for a in Action}
_OUTCOMES = {o.value for o in TaskOutcome}
_REVIEW = {r.value for r in ReviewState}
_INTENT = {i.value for i in CodedIntent}
_BANDS = {b.value for b in ConfidenceBand}


@dataclass
class ImportReport:
    run_id: str
    data_dir: str
    inserted: dict[str, int] = field(default_factory=dict)
    duplicated: dict[str, int] = field(default_factory=dict)
    quarantined: dict[str, int] = field(default_factory=dict)
    orphan_objects: int = 0
    mis_taps: int = 0
    errors: list[str] = field(default_factory=list)

    def summary_lines(self) -> list[str]:
        lines = [
            f"导入批次 {self.run_id}（来源目录 {self.data_dir}）",
            "  文件            新增  重复  隔离",
        ]
        for name in _FILES:
            lines.append(
                f"  {name:<18} {self.inserted.get(name, 0):>4}  "
                f"{self.duplicated.get(name, 0):>4}  {self.quarantined.get(name, 0):>4}"
            )
        lines.append(f"  孤儿渲染对象：{self.orphan_objects}")
        lines.append(f"  误触点击：{self.mis_taps}")
        if self.errors:
            lines.append("  错误：")
            lines.extend(f"    - {e}" for e in self.errors)
        return lines


def open_database(db_path: str | Path) -> sqlite3.Connection:
    """打开（必要时创建）分析数据库。"""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA_SQL)
    return conn


def _read_csv(data_dir: Path, filename: str) -> tuple[list[dict[str, str]], list[str]]:
    """读取 UTF-8 BOM CSV；返回 (数据行, 字段名)。文件缺失时给出明确错误。"""
    path = data_dir / filename
    if not path.exists():
        raise FileNotFoundError(f"缺少资料文件：{path}")
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        fieldnames = reader.fieldnames or []
        rows = [dict(row) for row in reader]
    return rows, list(fieldnames)


def _row_identity(filename: str, row: dict[str, str]) -> str:
    if filename == "view_sessions.csv":
        return row.get("session_id", "")
    if filename == "rendered_objects.csv":
        return "|".join((row.get("object_id", ""), row.get("session_id", ""), row.get("frame_time", "")))
    if filename == "interactions.csv":
        return row.get("interaction_id", "")
    return row.get("response_id", "")


def _validate(filename: str, row: dict[str, str]) -> str | None:
    """返回隔离原因；None 表示有效。空字段与非法枚举都隔离。"""
    if any(value is None or value == "" for value in row.values()):
        return "存在空字段"
    if filename == "view_sessions.csv":
        if row["exposure_state"] not in _EXPOSURE:
            return f"未知 exposure_state: {row['exposure_state']}"
    elif filename == "rendered_objects.csv":
        if row["confidence_band"] not in _BANDS:
            return f"未知 confidence_band: {row['confidence_band']}"
    elif filename == "interactions.csv":
        if row["action"] not in _ACTIONS:
            return f"未知 action: {row['action']}"
        if row["task_outcome"] not in _OUTCOMES:
            return f"未知 task_outcome: {row['task_outcome']}"
    elif filename == "study_responses.csv":
        if row["coded_intent"] not in _INTENT:
            return f"未知 coded_intent: {row['coded_intent']}"
        if row["review_state"] not in _REVIEW:
            return f"未知 review_state: {row['review_state']}"
    return None


def _quarantine(conn: sqlite3.Connection, report: ImportReport, run_id: str,
                filename: str, row: dict[str, str], reason: str) -> None:
    import json
    conn.execute(
        "INSERT INTO quarantine_rows(run_id, source_file, primary_key, reason, raw_row) "
        "VALUES (?, ?, ?, ?, ?)",
        (run_id, filename, _row_identity(filename, row), reason, json.dumps(row, ensure_ascii=False)),
    )
    report.quarantined[filename] = report.quarantined.get(filename, 0) + 1


def _insert_once(conn: sqlite3.Connection, report: ImportReport, run_id: str,
                 filename: str, row: dict[str, str]) -> bool:
    """幂等插入。返回 True 表示新增，False 表示重复（登记冲突后跳过）。"""
    ident = _row_identity(filename, row)
    if filename == "view_sessions.csv":
        exists = conn.execute(
            "SELECT 1 FROM view_sessions WHERE session_id=?", (row["session_id"],)
        ).fetchone()
        if exists:
            conn.execute(
                "INSERT INTO import_conflicts(run_id, source_file, primary_key, reason) "
                "VALUES (?, ?, ?, ?)",
                (run_id, filename, ident, "重复会话：保留首次导入的显示与意图版本，未覆盖"),
            )
            report.duplicated[filename] = report.duplicated.get(filename, 0) + 1
            return False
        conn.execute(
            "INSERT INTO view_sessions(session_id, driver_key, ui_version, started_at, "
            "exposure_state, route_intent_version, task_type, first_seen_run) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (row["session_id"], row["driver_key"], row["ui_version"], row["started_at"],
             row["exposure_state"], row["route_intent_version"], row["task_type"], run_id),
        )
    elif filename == "rendered_objects.csv":
        exists = conn.execute(
            "SELECT 1 FROM rendered_objects WHERE object_id=? AND session_id=? AND frame_time=?",
            (row["object_id"], row["session_id"], row["frame_time"]),
        ).fetchone()
        if exists:
            conn.execute(
                "INSERT INTO import_conflicts(run_id, source_file, primary_key, reason) "
                "VALUES (?, ?, ?, ?)",
                (run_id, filename, ident, "重复渲染对象，跳过"),
            )
            report.duplicated[filename] = report.duplicated.get(filename, 0) + 1
            return False
        conn.execute(
            "INSERT INTO rendered_objects(object_id, session_id, object_type, frame_time, "
            "source_version, confidence_band, first_seen_run) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (row["object_id"], row["session_id"], row["object_type"], row["frame_time"],
             row["source_version"], row["confidence_band"], run_id),
        )
    elif filename == "interactions.csv":
        exists = conn.execute(
            "SELECT 1 FROM interactions WHERE interaction_id=?", (row["interaction_id"],)
        ).fetchone()
        if exists:
            conn.execute(
                "INSERT INTO import_conflicts(run_id, source_file, primary_key, reason) "
                "VALUES (?, ?, ?, ?)",
                (run_id, filename, ident, "重复交互记录，跳过"),
            )
            report.duplicated[filename] = report.duplicated.get(filename, 0) + 1
            return False
        conn.execute(
            "INSERT INTO interactions(interaction_id, session_id, target_id, action, "
            "action_time, task_outcome, first_seen_run) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (row["interaction_id"], row["session_id"], row["target_id"], row["action"],
             row["action_time"], row["task_outcome"], run_id),
        )
    else:  # study_responses.csv
        exists = conn.execute(
            "SELECT 1 FROM study_responses WHERE response_id=?", (row["response_id"],)
        ).fetchone()
        if exists:
            conn.execute(
                "INSERT INTO import_conflicts(run_id, source_file, primary_key, reason) "
                "VALUES (?, ?, ?, ?)",
                (run_id, filename, ident, "重复问卷记录，跳过"),
            )
            report.duplicated[filename] = report.duplicated.get(filename, 0) + 1
            return False
        conn.execute(
            "INSERT INTO study_responses(response_id, session_id, questionnaire, submitted_at, "
            "coded_intent, review_state, first_seen_run) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (row["response_id"], row["session_id"], row["questionnaire"], row["submitted_at"],
             row["coded_intent"], row["review_state"], run_id),
        )
    report.inserted[filename] = report.inserted.get(filename, 0) + 1
    return True


def _rebuild_linkage(conn: sqlite3.Connection) -> tuple[int, int]:
    """全量重算孤儿对象与误触标记（与导入顺序、晚到曝光无关）。

    返回 (孤儿对象数, 误触交互数)。
    """
    conn.execute("UPDATE rendered_objects SET is_orphan = 0")
    conn.execute(
        "UPDATE rendered_objects SET is_orphan = 1 "
        "WHERE session_id NOT IN (SELECT session_id FROM view_sessions)"
    )
    orphan_count = conn.execute(
        "SELECT COUNT(*) FROM rendered_objects WHERE is_orphan = 1"
    ).fetchone()[0]

    # 误触：发生了真实点击（tap/dismiss），但目标对象不在该会话的渲染对象集合中。
    conn.execute("UPDATE interactions SET is_mis_tap = 0")
    conn.execute(
        """
        UPDATE interactions
           SET is_mis_tap = 1
         WHERE action IN ('tap', 'dismiss')
           AND NOT EXISTS (
               SELECT 1
                 FROM rendered_objects o
                WHERE o.session_id = interactions.session_id
                  AND o.object_id  = interactions.target_id
                  AND o.is_orphan  = 0
           )
        """
    )
    mis_tap_count = conn.execute(
        "SELECT COUNT(*) FROM interactions WHERE is_mis_tap = 1"
    ).fetchone()[0]
    return orphan_count, mis_tap_count


def import_directory(conn: sqlite3.Connection, data_dir: str | Path,
                     note: str = "", run_id: str | None = None) -> ImportReport:
    """把一个资料目录导入数据库，返回结构化导入报告。

    目录中四类 CSV 应同时代表同一批次；个别文件缺失会整体报错，避免在
    不完整批次上产生误导性结论。晚到曝光可通过再次导入同目录补齐（幂等）。
    """
    data_dir = Path(data_dir)
    run_id = run_id or f"RUN-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')}"
    report = ImportReport(run_id=run_id, data_dir=str(data_dir))

    conn.execute(
        "INSERT INTO import_runs(run_id, imported_at, data_dir, note) VALUES (?, ?, ?, ?)",
        (run_id, datetime.now(timezone.utc).isoformat(), str(data_dir), note),
    )
    try:
        for filename in _FILES:
            rows, _fields = _read_csv(data_dir, filename)
            for row in rows:
                reason = _validate(filename, row)
                if reason is not None:
                    _quarantine(conn, report, run_id, filename, row, reason)
                    continue
                _insert_once(conn, report, run_id, filename, row)
        report.orphan_objects, report.mis_taps = _rebuild_linkage(conn)
        conn.commit()
    except Exception as exc:  # 失败回滚，数据库回到导入前状态
        conn.rollback()
        raise
    return report
