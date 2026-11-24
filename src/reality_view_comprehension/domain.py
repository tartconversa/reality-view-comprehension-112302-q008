"""领域模型与常量。

本模块只包含纯数据定义，不读文件、不访问数据库，方便单测与复核。

三类观察彼此独立，不可互相替代：

1. 功能是否出现（rendered_objects 中是否有可关联的对象）；
2. 用户是否点击目标（interactions 中的 tap / dismiss 行为，可能是误触）；
3. 用户能否正确解释系统意图（study_responses 中经人工复核的编码）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Final


# ---------------------------------------------------------------------------
# 枚举：取值以各行原始记录为准，未知值不静默改写，导入时进入隔离区。
# ---------------------------------------------------------------------------
class ExposureState(str, Enum):
    FULL = "full"        # 曝光记录与驾驶事件同步齐备
    PARTIAL = "partial"  # 部分曝光（帧/对象不完整）
    LATE = "late"        # 曝光记录晚于驾驶事件抵达


class Action(str, Enum):
    TAP = "tap"
    DISMISS = "dismiss"
    NONE = "none"        # 未发生点击，是有效业务事实，不等于零事件


class TaskOutcome(str, Enum):
    CORRECT = "correct"
    INCORRECT = "incorrect"
    UNKNOWN = "unknown"  # 无法判定，不能按成功或失败处理


class ReviewState(str, Enum):
    CODED = "coded"      # 已人工复核编码，可用于理解度判定
    PENDING = "pending"  # 待复核
    REVISED = "revised"  # 已修订但仍需确认
    WITHDRAWN = "withdrawn"  # 参与者撤回，不进入分析


class CodedIntent(str, Enum):
    UNDERSTOOD = "understood"
    MISREAD = "misread"
    UNCLEAR = "unclear"


class ConfidenceBand(str, Enum):
    HIGH = "high"
    MID = "mid"
    LOW = "low"


class ObservabilityLevel(str, Enum):
    """可观察程度分级（用于分析分层与边界披露）。"""

    L1_CONFIRMED = "L1-可判定"
    """显示版本、对象来源、路线意图三者对齐，且有已复核编码问卷。"""

    L2_BEHAVIOR_ONLY = "L2-仅行为可观察"
    """曝光/对齐有缺口，或缺少已复核问卷，只能观察功能出现与点击。"""

    L3_LINKAGE_GAP = "L3-关联缺口"
    """存在记录但无法关联到当时显示或计划（孤儿对象、版本错配等）。"""

    L4_NO_EVIDENCE = "L4-无证据"
    """无任何可关联记录。"""


# ---------------------------------------------------------------------------
# 版本兼容清单
#
# 显示版本（ui_version）、对象来源版本（source_version）、路线意图版本
# （route_intent_version）是三套独立版本号，不能按字符串前缀猜测对应关系。
# 只有在本清单显式登记的组合，才允许判断“用户看见的内容与当时计划一致”。
# 清单由研究运营维护，配置变更通过 planning 的配置纪元登记，不回改历史。
# ---------------------------------------------------------------------------
VERSION_ALIGNMENT: Final[dict[str, dict[str, frozenset[str]]]] = {
    # 显示版本 -> 对象来源版本集合 -> 可对齐的路线意图版本集合
    "UI-A": {
        "UI-0": frozenset({"NAV-0", "NAV-2"}),
        "UI-1": frozenset({"NAV-0", "NAV-2"}),
    },
    "UI-B": {
        "UI-1": frozenset({"NAV-1", "NAV-3"}),
        "UI-2": frozenset({"NAV-1", "NAV-3"}),
    },
}

# 合成数据生成器与演示使用的取值域（与基线 CSV 一致）。
UI_VERSIONS: Final = ("UI-A", "UI-B")
TASK_TYPES: Final = ("turn", "hazard", "destination")
OBJECT_TYPES: Final = ("route-arrow", "lane", "vehicle", "hazard")
QUESTIONNAIRES: Final = ("FORM-0", "FORM-1", "FORM-2", "FORM-3")


def is_version_aligned(ui_version: str, source_version: str, route_intent_version: str) -> bool:
    """判断三套版本是否构成已登记的对齐组合。

    未登记的组合一律返回 False（保守处理），调用方应据此把会话降级为
    L3 关联缺口，而不是猜测用户实际所见。
    """
    sources = VERSION_ALIGNMENT.get(ui_version)
    if not sources:
        return False
    intents = sources.get(source_version)
    if not intents:
        return False
    return route_intent_version in intents


# ---------------------------------------------------------------------------
# 分析期数据载体
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class SessionEvidence:
    """一次曝光会话在分析时点的全部可关联证据。"""

    session_id: str
    driver_key: str
    ui_version: str
    route_intent_version: str
    task_type: str
    exposure_state: str
    started_at: str
    # 对象证据
    object_count: int = 0
    object_source_versions: frozenset[str] = field(default_factory=frozenset)
    confidence_bands: frozenset[str] = field(default_factory=frozenset)
    has_orphan_object: bool = False
    # 行为证据
    has_interaction: bool = False
    tap_count: int = 0
    dismiss_count: int = 0
    none_count: int = 0
    matched_tap_count: int = 0       # 命中本会话真实渲染对象的点击
    mis_tap_count: int = 0          # 误触（目标与本会话渲染对象不匹配）
    correct_outcomes: int = 0
    incorrect_outcomes: int = 0
    unknown_outcomes: int = 0
    # 问卷证据
    has_response: bool = False
    reviewed_response: bool = False  # review_state == coded
    withdrawn_response: bool = False
    coded_intent: str | None = None
    # 判定结果
    aligned: bool = False
    observability: str = ObservabilityLevel.L4_NO_EVIDENCE.value
    comprehension_correct: bool | None = None  # 仅 L1 为 True/False，其余为 None


@dataclass(frozen=True)
class GroupMetrics:
    """一个分析分层（显示版本 × 任务类型 × 可观察程度）的汇总指标。"""

    ui_version: str
    task_type: str
    observability: str
    sessions: int
    # 功能出现
    feature_present_sessions: int
    # 行为
    tap_sessions: int
    matched_tap_sessions: int
    mis_tap_sessions: int
    # 问卷
    responded_sessions: int
    reviewed_sessions: int
    understood: int
    misread: int
    unclear: int
    # 理解度（分母仅为已复核编码会话）
    comprehension_rate: float | None
    # 行为正确率（分母为有明确 correct/incorrect 结果的会话）
    behavior_accuracy: float | None
    mis_tap_rate: float | None  # 分母为发生点击（tap/dismiss）的会话

    def as_row(self) -> dict[str, object]:
        return {
            "ui_version": self.ui_version,
            "task_type": self.task_type,
            "observability": self.observability,
            "sessions": self.sessions,
            "feature_present_sessions": self.feature_present_sessions,
            "tap_sessions": self.tap_sessions,
            "matched_tap_sessions": self.matched_tap_sessions,
            "mis_tap_sessions": self.mis_tap_sessions,
            "responded_sessions": self.responded_sessions,
            "reviewed_sessions": self.reviewed_sessions,
            "understood": self.understood,
            "misread": self.misread,
            "unclear": self.unclear,
            "comprehension_rate": (
                "" if self.comprehension_rate is None else f"{self.comprehension_rate:.4f}"
            ),
            "behavior_accuracy": (
                "" if self.behavior_accuracy is None else f"{self.behavior_accuracy:.4f}"
            ),
            "mis_tap_rate": (
                "" if self.mis_tap_rate is None else f"{self.mis_tap_rate:.4f}"
            ),
        }
