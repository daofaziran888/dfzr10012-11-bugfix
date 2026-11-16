"""检查点恢复业务规则：状态流转、字段校验与筛选口径都收在这里。

恢复状态时通过 StateService 读取同一份「状态后端配置版本数据」：
- 检查点缓存里的旧配置可能与刚启用的状态后端配置冲突，恢复写入侧只认版本表里
  唯一可核对的最新版本，不把旧状态大小写回当前状态后端记录；
- 读不到启用版本时不影响检查点本身的状态流转，只给出可读提示，CHEC 记录继续可恢复。
"""
from __future__ import annotations

from typing import Any

from app.services.state import StateService
from app.store import store

MODULE = "checkpoint"
STATE_MODULE = "state"
REQUIRED_FIELDS = ["记录编号", "流作业名", "检查点ID"]
STATUS_ORDER = ["已创建", "已恢复", "已过期", "已清理"]
ACTION_RULES = {"恢复状态": "已恢复", "清理记录": "已恢复", "归档记录": "已过期"}
NEGATIVE_ACTIONS = []


class CheckpointService:
    def __init__(self) -> None:
        self.state_service = StateService()

    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("记录编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def _resolve_state_entry_id(self, entry: dict[str, Any]) -> int | None:
        """按记录编号后缀匹配同序号的状态后端配置（CHEC-0002 -> STAT-0002）。"""
        code = str(entry.get("记录编号", ""))
        suffix = code.split("-", 1)[1] if "-" in code else ""
        if not suffix:
            return None
        target_code = f"STAT-{suffix}"
        for state_row in store.rows(STATE_MODULE):
            if state_row.get("配置编号") == target_code:
                return int(state_row["id"])
        return None

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"检查点记录 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于检查点恢复可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"

        message = f"检查点记录已{action}"
        if action == "恢复状态":
            message = self._restore_with_state_config(entry, message)

        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, message

    def _restore_with_state_config(
        self,
        entry: dict[str, Any],
        fallback_message: str,
    ) -> str:
        """恢复时读取状态后端配置版本数据，并把可核对版本挂到检查点记录上。"""
        state_entry_id = self._resolve_state_entry_id(entry)
        if state_entry_id is None:
            return fallback_message
        version = self.state_service.latest_version(state_entry_id)
        if version is None:
            # 状态后端尚未启用：不阻断恢复，检查点记录继续可恢复。
            return f"{fallback_message}，暂无可核对的状态后端配置版本"

        # 只从版本表读取，绝不回写状态后端记录，避免缓存旧值覆盖当前配置。
        config = dict(version.get("config", {}))
        entry["状态后端配置版本ID"] = version.get("id")
        entry["状态后端配置版本号"] = version.get("version")
        entry["状态后端配置编号"] = config.get("配置编号")
        # 恢复侧后续处理统一使用版本快照里的状态大小，而非检查点缓存的旧值。
        entry["恢复读取状态大小"] = config.get("状态大小")
        entry["恢复读取快照频率"] = config.get("快照频率")
        entry["恢复读取清理间隔"] = config.get("清理间隔")
        return (
            f"{fallback_message}，已按状态后端配置版本 "
            f"v{version.get('version')} 读取完整配置"
        )
