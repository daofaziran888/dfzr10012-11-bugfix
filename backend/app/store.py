"""内存数据仓库：给每个业务模块准备一份可筛选、可流转的示例数据。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。

状态后端配置版本数据（state_config_versions）也持久化在仓库里：
启用动作一次写齐完整配置快照，写入后再次查询仍保持一致，历史版本全部保留可追溯。
"""
from __future__ import annotations

import threading
from typing import Any

from app.seed import SEED_ROWS

# 状态后端配置版本数据表名：state 模块与 checkpoint 消费侧共用同一份版本数据。
STATE_VERSION_TABLE = "state_config_versions"
# 内部表：只做配置版本持久化，不算业务模块，不出现在概览/健康检查的模块清单里。
INTERNAL_TABLES = frozenset({STATE_VERSION_TABLE})


class Store:
    def __init__(self) -> None:
        self._tables: dict[str, list[dict[str, Any]]] = {
            name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
        }
        # 版本表不在种子数据里，启用第一份配置时才开始累积版本。
        self._tables.setdefault(STATE_VERSION_TABLE, [])
        # 仓库级锁：保护表内写入与版本号分配。
        self._lock = threading.RLock()
        # 每条状态后端配置一把串行锁，保证并发启用只接受一份配置快照。
        self._key_locks: dict[str, threading.RLock] = {}

    def module_names(self) -> list[str]:
        return sorted(name for name in self._tables if name not in INTERNAL_TABLES)

    def rows(self, module: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(module, [])

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    def key_lock(self, module: str, entry_id: int) -> threading.RLock:
        """取某条记录的串行锁，同一条记录的并发动作排队执行。"""
        key = f"{module}:{entry_id}"
        with self._lock:
            lock = self._key_locks.get(key)
            if lock is None:
                lock = threading.RLock()
                self._key_locks[key] = lock
            return lock

    def list_versions(self, module: str, entry_id: int) -> list[dict[str, Any]]:
        """按版本号倒序返回某条记录的全部配置版本，历史配置保持可追溯。"""
        with self._lock:
            rows = [
                dict(row)
                for row in self.rows(STATE_VERSION_TABLE)
                if row.get("module") == module and int(row.get("entry_id", 0)) == entry_id
            ]
        return sorted(rows, key=lambda row: int(row.get("version", 0)), reverse=True)

    def latest_version(self, module: str, entry_id: int) -> dict[str, Any] | None:
        """读取当前可核对的最新一份完整配置版本数据。"""
        versions = self.list_versions(module, entry_id)
        return versions[0] if versions else None

    def save_version(self, module: str, entry_id: int, snapshot: dict[str, Any]) -> dict[str, Any]:
        """持久化一份配置版本快照。

        快照先在调用侧组装完整再写入：版本号在锁内分配，落库失败时抛出异常，
        已有的旧版本数据原样保留，检查点恢复仍可读取上一份完整配置。
        """
        version_row = {
            "id": None,
            "module": module,
            "entry_id": entry_id,
            "version": 0,
            "enabled": True,
            "config": dict(snapshot),
        }
        with self._lock:
            table = self.rows(STATE_VERSION_TABLE)
            next_id = max((int(row.get("id", 0)) for row in table), default=0) + 1
            next_version = max(
                (
                    int(row.get("version", 0))
                    for row in table
                    if row.get("module") == module and int(row.get("entry_id", 0)) == entry_id
                ),
                default=0,
            ) + 1
            version_row["id"] = next_id
            version_row["version"] = next_version
            table.append(version_row)
            # 返回深拷贝，避免消费侧拿到引用后误改已持久化的版本数据。
            return dict(version_row)

    def overview(self) -> dict[str, object]:
        modules: list[dict[str, object]] = []
        for name in self.module_names():
            rows = self.rows(name)
            modules.append({
                "name": name,
                "created": len(rows),
                "pending": sum(1 for row in rows if row.get("pending")),
                "abnormal": sum(1 for row in rows if row.get("abnormal")),
            })
        cards = [
            {"label": "业务模块", "value": len(modules)},
            {"label": "今日新增", "value": sum(int(item["created"]) for item in modules)},
            {"label": "待处理", "value": sum(int(item["pending"]) for item in modules)},
            {"label": "异常量", "value": sum(int(item["abnormal"]) for item in modules)},
        ]
        return {"cards": cards, "modules": modules}


store = Store()
