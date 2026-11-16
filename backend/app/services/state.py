"""状态后端管理业务规则：状态流转、字段校验与筛选口径都收在这里。

启用后端时必须一次写齐状态大小、快照频率、清理间隔，并把完整配置值固化成
「状态后端配置版本数据」（store.STATE_VERSION_TABLE）：
- 启用字段（enabled）与配置值同口径落库，页面列表与详情抽屉读取同一份记录；
- 同一条配置串行处理并发启用，配置未变化时复用存量版本，只保留一个结果；
- 版本快照持久化后可反复查询且保持一致，历史版本全部保留、可追溯；
- 落库失败时不改动当前记录，检查点恢复仍能读取上一份完整配置。
"""
from __future__ import annotations

from typing import Any

from app.store import STATE_VERSION_TABLE, store

MODULE = "state"
REQUIRED_FIELDS = ["配置编号", "后端类型", "存储路径"]
STATUS_ORDER = ["草稿", "已启用", "已暂停", "已关闭"]
ACTION_RULES = {"启用后端": "已启用", "暂停后端": "已暂停", "关闭后端": "已关闭"}
NEGATIVE_ACTIONS = []

ENABLE_ACTION = "启用后端"
# 启用动作必须一次写齐的三项配置。
ENABLE_REQUIRED_FIELDS = ["状态大小", "快照频率", "清理间隔"]
# 版本快照保留的完整配置值，存储路径从当前记录读取，其余随启用动作提交。
CONFIG_FIELDS = [
    "配置编号",
    "后端类型",
    "存储路径",
    "状态大小",
    "过期策略",
    "清理间隔",
    "快照频率",
]


class StateService:
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
            rows = [row for row in rows if keyword in str(row.get("配置编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def list_versions(self, entry_id: int) -> list[dict[str, Any]]:
        """返回某条后端配置的全部版本数据（版本号倒序），供详情抽屉追溯。"""
        return store.list_versions(MODULE, entry_id)

    def latest_version(self, entry_id: int) -> dict[str, Any] | None:
        """读取当前唯一可核对的启用版本，检查点恢复侧也走这个口径。"""
        return store.latest_version(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["enabled"] = False
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(
        self,
        entry_id: int,
        action: str,
        values: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"后端配置 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于状态后端管理可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"

        if action == ENABLE_ACTION:
            return self._enable(entry, values or {})

        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        if action == "关闭后端":
            entry["enabled"] = False
        return entry, f"后端配置已{action}"

    def _enable(
        self,
        entry: dict[str, Any],
        values: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, str]:
        """启用后端：校验启用字段 -> 组装完整快照 -> 幂等持久化版本 -> 回写当前记录。"""
        values = {key: val for key, val in values.items() if key != "action"}
        missing = [
            field
            for field in ENABLE_REQUIRED_FIELDS
            if not str(values.get(field) or "").strip()
        ]
        if missing:
            return None, f"启用后端缺少必填配置：{'、'.join(missing)}"

        entry_id = int(entry["id"])
        # 同一条配置的并发启用排队进入，临界区内只可能落一份配置快照。
        with store.key_lock(MODULE, entry_id):
            snapshot = self._build_snapshot(entry, values)
            latest = store.latest_version(MODULE, entry_id)
            if latest is not None and latest.get("config") == snapshot:
                # 重复启用且配置未变化：沿用存量版本，不再生成新版本，只生效一次。
                version_row = latest
                message = "后端配置已启用，配置未变化，沿用存量配置版本"
            else:
                try:
                    version_row = store.save_version(MODULE, entry_id, snapshot)
                except Exception:  # pragma: no cover - 落库失败时保留上一份完整配置
                    return None, "状态后端配置版本落库失败，已保留上一份完整配置"
                message = "后端配置已启用，配置版本已固化"

            # 版本持久化成功后才回写主记录，保证读取口径与版本数据一致。
            for field, value in snapshot.items():
                entry[field] = value
            entry["enabled"] = True
            entry["status"] = STATUS_ORDER[1]
            entry["pending"] = True
            entry["abnormal"] = False
            entry["配置版本号"] = int(version_row["version"])
        return entry, message

    @staticmethod
    def _build_snapshot(entry: dict[str, Any], values: dict[str, Any]) -> dict[str, Any]:
        """按统一字段口径组装一份完整配置快照，缺失项保留当前记录的存量值。"""
        snapshot: dict[str, Any] = {}
        for field in CONFIG_FIELDS:
            submitted = values.get(field)
            if submitted is not None and str(submitted).strip():
                snapshot[field] = submitted
            else:
                snapshot[field] = entry.get(field)
        return snapshot
