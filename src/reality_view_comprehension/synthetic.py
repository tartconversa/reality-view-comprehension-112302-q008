"""可复现合成数据生成器（仅虚构数据，供本地流程与测试使用）。

固定种子 -> 逐字节可复现。生成的资料刻意覆盖分析必须处理的边界：

- 三种曝光状态 full / partial / late（晚到记录单独输出，可二次导入）；
- 版本对齐与错配（未在兼容清单登记的来源/意图组合）；
- 点击中的误触（目标不属于本会话渲染对象）；
- 无问卷会话、待复核/撤回问卷（问卷样本非随机）；
- 找不到会话的孤儿渲染对象；
- 一个字段非法的隔离区样例行。

生成目录结构::

    <out>/baseline/   首批资料（四类 CSV，UTF-8 BOM）
    <out>/late/       晚到补充批次（会话与对象，演示重复导入的幂等补齐）
"""

from __future__ import annotations

import csv
import random
from pathlib import Path

from .domain import (
    OBJECT_TYPES,
    TASK_TYPES,
    UI_VERSIONS,
    VERSION_ALIGNMENT,
)

_FILENAMES = (
    "view_sessions.csv",
    "rendered_objects.csv",
    "interactions.csv",
    "study_responses.csv",
)

_SESSION_COLUMNS = [
    "session_id", "driver_key", "ui_version", "started_at",
    "exposure_state", "route_intent_version", "task_type",
]
_OBJECT_COLUMNS = [
    "session_id", "object_id", "object_type", "frame_time",
    "source_version", "confidence_band",
]
_INTERACTION_COLUMNS = [
    "interaction_id", "session_id", "target_id", "action",
    "action_time", "task_outcome",
]
_RESPONSE_COLUMNS = [
    "response_id", "session_id", "questionnaire", "submitted_at",
    "coded_intent", "review_state",
]


def _write_csv(path: Path, columns: list[str], rows: list[list[object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(columns)
        writer.writerows(rows)


def _aligned_triple(rng: random.Random) -> tuple[str, str, str]:
    ui = rng.choice(UI_VERSIONS)
    source = rng.choice(sorted(VERSION_ALIGNMENT[ui]))
    intent = rng.choice(sorted(VERSION_ALIGNMENT[ui][source]))
    return ui, source, intent


def _misaligned_triple(rng: random.Random) -> tuple[str, str, str]:
    """刻意构造未登记组合：UI-B 会话配 UI-0 来源、UI-A 会话配 UI-2 来源。"""
    ui = rng.choice(UI_VERSIONS)
    if ui == "UI-A":
        return ui, "UI-2", rng.choice(["NAV-1", "NAV-3"])
    return ui, "UI-0", rng.choice(["NAV-0", "NAV-2"])


def generate_dataset(out_dir: str | Path, *, seed: int = 20261124,
                     n_sessions: int = 30, include_edge_cases: bool = True) -> dict[str, Path]:
    """生成合成资料，返回 {'baseline': 路径, 'late': 路径}。"""
    if n_sessions < 12:
        raise ValueError("n_sessions 至少为 12，才能容纳各类边界场景")
    rng = random.Random(seed)
    root = Path(out_dir)
    baseline = root / "baseline"
    late_dir = root / "late"

    exposure_cycle = ["full", "partial", "late"]
    sessions: list[list[object]] = []
    objects: list[list[object]] = []
    interactions: list[list[object]] = []
    responses: list[list[object]] = []

    # 晚到批次中再次出现的会话（late 状态），用于演示幂等补齐
    late_sessions: list[list[object]] = []
    late_objects: list[list[object]] = []

    seq = 0
    for i in range(1, n_sessions + 1):
        seq += 1
        sid = f"S-{seq:03d}"
        driver = f"D-{seq % 10:02d}"
        exposure = exposure_cycle[(i - 1) % 3]
        task_type = TASK_TYPES[(i - 1) % len(TASK_TYPES)]
        started = f"2026-11-{((i - 1) % 28) + 1:02d}T{(i % 24):02d}:00Z"

        # 每 6 个会话放一个版本错配样本
        if include_edge_cases and i % 6 == 0:
            ui, source, intent = _misaligned_triple(rng)
        else:
            ui, source, intent = _aligned_triple(rng)

        sessions.append([sid, driver, ui, started, exposure, intent, task_type])

        obj_id = f"OBJ-{seq % 15:02d}"
        obj_type = OBJECT_TYPES[(i - 1) % len(OBJECT_TYPES)]
        band = rng.choice(["high", "mid", "low"])
        objects.append([sid, obj_id, obj_type, started, source, band])

        # 部分会话给第二个对象，制造一对多关联
        if i % 4 == 0:
            objects.append([
                sid, f"OBJ-{(seq + 7) % 15:02d}",
                rng.choice(OBJECT_TYPES), started, source, band,
            ])

        # 行为：tap / dismiss / none 循环；none 的结果固定 unknown
        action = ("tap", "dismiss", "none")[(i - 1) % 3]
        if action == "none":
            outcome = "unknown"
            target = obj_id
        else:
            outcome = "correct" if action == "tap" else "incorrect"
            target = obj_id
        # 每 7 个会话在真实点击上制造一次误触：
        # 目标不属于本会话渲染对象（7 与动作周期 3 互质，不会只落在 none 上）
        if include_edge_cases and action in ("tap", "dismiss") and i % 7 == 0:
            target = f"OBJ-99{rng.randint(1, 9)}"
        action_time = started.replace(":00Z", ":01Z")
        interactions.append([
            f"I-{seq:03d}", sid, target, action, action_time, outcome,
        ])

        # 问卷：约 2/3 会话有问卷；其中状态轮转 coded/pending/revised/withdrawn
        if i % 3 != 0:
            review_state = ("coded", "pending", "revised", "withdrawn")[(i - 1) % 4]
            if review_state == "coded":
                coded = "understood" if action == "tap" else rng.choice(
                    ["understood", "misread", "unclear"]
                )
            else:
                coded = rng.choice(["understood", "misread", "unclear"])
            responses.append([
                f"R-{seq:03d}", sid, f"FORM-{seq % 4}",
                started.replace(":00Z", ":05Z"), coded, review_state,
            ])

        # late 会话同时进入晚到批次（含其对象），演示重复导入不产生重复行
        if exposure == "late":
            late_sessions.append([sid, driver, ui, started, exposure, intent, task_type])
            late_objects.append([sid, obj_id, obj_type, started, source, band])

    # 孤儿渲染对象：会话号超出已生成范围
    if include_edge_cases:
        for k in range(1, 4):
            orphan_sid = f"S-{n_sessions + k:03d}"
            objects.append([
                orphan_sid, f"OBJ-8{k}", rng.choice(OBJECT_TYPES),
                f"2026-11-{k:02d}T08:00Z", f"UI-{k % 3}", rng.choice(["high", "mid", "low"]),
            ])
        # 隔离区样例：confidence_band 非法
        objects.append([
            "S-001", "OBJ-77", "lane", "2026-11-01T09:00Z", "UI-0", "bogus-band",
        ])

    for path, cols, rows in [
        (baseline / "view_sessions.csv", _SESSION_COLUMNS, sessions),
        (baseline / "rendered_objects.csv", _OBJECT_COLUMNS, objects),
        (baseline / "interactions.csv", _INTERACTION_COLUMNS, interactions),
        (baseline / "study_responses.csv", _RESPONSE_COLUMNS, responses),
    ]:
        _write_csv(path, cols, rows)

    # 晚到批次只含会话与对象（交互/问卷可能更晚到达，这里留空文件保持契约形状）
    _write_csv(late_dir / "view_sessions.csv", _SESSION_COLUMNS, late_sessions)
    _write_csv(late_dir / "rendered_objects.csv", _OBJECT_COLUMNS, late_objects)
    _write_csv(late_dir / "interactions.csv", _INTERACTION_COLUMNS, [])
    _write_csv(late_dir / "study_responses.csv", _RESPONSE_COLUMNS, [])

    return {"baseline": baseline, "late": late_dir}


def dataset_file_paths(directory: str | Path) -> dict[str, Path]:
    base = Path(directory)
    return {name: base / name for name in _FILENAMES}
