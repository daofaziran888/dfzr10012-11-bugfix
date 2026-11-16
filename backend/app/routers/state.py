"""状态后端管理接口：维护后端配置，覆盖启用后端、暂停后端、关闭后端等动作。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.state import StateService

router = APIRouter(prefix="/api/state", tags=["状态后端管理"])

service = StateService()

LIST_FIELDS = ["配置编号", "后端类型", "存储路径", "状态大小", "过期策略", "清理间隔", "快照频率", "配置状态"]
STATUSES = ["草稿", "已启用", "已暂停", "已关闭"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按配置编号检索"),
    status: str | None = Query(default=None, description="草稿、已启用、已暂停、已关闭"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按配置编号与状态过滤状态后端管理列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出状态后端管理清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "state", "total": total, "items": items}


@router.get("/{entry_id}/versions")
def list_versions(entry_id: int) -> dict[str, Any]:
    """读取某条后端配置的全部配置版本数据，历史配置保持可追溯。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"后端配置 {entry_id} 不存在或已归档")
    versions = service.list_versions(entry_id)
    return {"module": "state", "entry_id": entry_id, "total": len(versions), "items": versions}


@router.get("/{entry_id}/active-config")
def get_active_config(entry_id: int) -> dict[str, Any]:
    """读取当前唯一可核对的状态后端配置版本数据（检查点恢复消费侧同走此口径）。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"后端配置 {entry_id} 不存在或已归档")
    version = service.latest_version(entry_id)
    if version is None:
        raise HTTPException(status_code=409, detail=f"后端配置 {entry_id} 尚未启用，无可核对的配置版本")
    return version


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条后端配置明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"后端配置 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条后端配置，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="后端配置已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条后端配置执行启用后端、暂停后端、关闭后端；不允许的动作会被拦下并说明原因。

    启用后端随动作提交状态大小、快照频率、清理间隔，服务层校验启用字段后一次写齐，
    并固化成状态后端配置版本数据；并发启用只接受一份配置快照。
    """
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action, payload.values)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
