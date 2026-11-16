"""状态后端配置版本修复的验收脚本（只读项目源码，不修改任何项目文件）。

覆盖：
1. 启用字段落库与读取口径一致（列表/详情同一份记录，刷新不丢）
2. 启用必须一次写齐 状态大小/快照频率/清理间隔，缺字段拒绝且保留旧配置
3. 状态大小、存储路径、快照频率、清理间隔完整固化在配置版本数据里
4. 重复启用配置不变只生效一次（沿用存量版本）；配置变化才新增版本，历史可追溯
5. 并发启用只接受一份配置快照
6. 落库失败时检查点恢复继续读取上一份完整配置
7. 检查点恢复消费同一份版本数据，不把缓存旧状态大小写回状态后端记录
8. 验收样例：STAT-0002 size=1.8GB snapshot=5m cleanup=30m；CHEC-0002 可恢复
"""
from __future__ import annotations

import sys
import threading
import traceback
from pathlib import Path

BACKEND = Path("/workspace/backend")
sys.path.insert(0, str(BACKEND))

from app.services.state import StateService  # noqa: E402
from app.services.checkpoint import CheckpointService  # noqa: E402
from app.store import store, STATE_VERSION_TABLE  # noqa: E402

PASS = 0
FAIL = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {detail}")


def fresh_services() -> tuple[StateService, CheckpointService]:
    """每个大场景重建一份内存仓库，避免互相污染。"""
    store._tables = {
        name: [dict(row) for row in rows]
        for name, rows in __import__("app.seed", fromlist=["SEED_ROWS"]).SEED_ROWS.items()
    }
    store._tables.setdefault(STATE_VERSION_TABLE, [])
    store._key_locks = {}
    return StateService(), CheckpointService()


print("场景1：启用字段落库与读取口径（STAT-0002 验收样例）")
state_svc, cp_svc = fresh_services()
seed1, seed2, seed3 = state_svc.get_entry(1), state_svc.get_entry(2), state_svc.get_entry(3)
check("种子启用字段与状态口径一致(草稿=False)", seed1.get("enabled") is False)
check("种子启用字段与状态口径一致(已启用=True)", seed2.get("enabled") is True)
check("种子启用字段与状态口径一致(已暂停=False)", seed3.get("enabled") is False)
check("启用前尚未固化配置版本", state_svc.latest_version(2) is None)
payload = {"action": "启用后端", "状态大小": "1.8GB", "快照频率": "5m", "清理间隔": "30m"}
entry, msg = state_svc.run_action(2, "启用后端", payload)
check("启用成功", entry is not None, msg)
check("enabled 字段落库为 True", entry and entry.get("enabled") is True)
check("status 落库为 已启用", entry and entry.get("status") == "已启用")
check("状态大小回写一致", entry and entry.get("状态大小") == "1.8GB")
check("快照频率回写一致", entry and entry.get("快照频率") == "5m")
check("清理间隔回写一致", entry and entry.get("清理间隔") == "30m")
check("存储路径保留", entry and bool(entry.get("存储路径")))
again = state_svc.get_entry(2)  # 模拟刷新/重新进入，重新取数
check("重新查询记录仍保持一致(状态大小)", again.get("状态大小") == "1.8GB")
check("重新查询记录仍保持一致(快照频率)", again.get("快照频率") == "5m")
check("重新查询记录仍保持一致(清理间隔)", again.get("清理间隔") == "30m")
check("重新查询 enabled 仍为 True", again.get("enabled") is True)

print("场景2：配置版本数据保留完整配置值")
versions = state_svc.list_versions(2)
check("生成 1 个配置版本", len(versions) == 1, str(len(versions)))
v1 = versions[0]
check("版本号为 1", v1.get("version") == 1)
check("版本 enabled=True", v1.get("enabled") is True)
cfg = v1.get("config", {})
check("版本快照含状态大小", cfg.get("状态大小") == "1.8GB")
check("版本快照含快照频率", cfg.get("快照频率") == "5m")
check("版本快照含清理间隔", cfg.get("清理间隔") == "30m")
check("版本快照含存储路径", bool(cfg.get("存储路径")))
check("版本快照含配置编号", cfg.get("配置编号") == "STAT-0002")
check("版本快照含后端类型", bool(cfg.get("后端类型")))
check("版本快照含过期策略", "过期策略" in cfg)
active = state_svc.latest_version(2)
check("latest_version 与版本表是同一份数据", active and active.get("id") == v1.get("id"))

print("场景3：缺字段启用被拒绝，且不产生版本、不污染当前记录")
state_svc, cp_svc = fresh_services()
before = state_svc.get_entry(1)
before_size = before.get("状态大小")
entry, msg = state_svc.run_action(1, "启用后端", {"action": "启用后端", "状态大小": "2GB"})
check("缺快照频率/清理间隔被拒绝", entry is None and "快照频率" in msg and "清理间隔" in msg, msg)
check("拒绝后不产生任何版本", len(state_svc.list_versions(1)) == 0)
after = state_svc.get_entry(1)
check("拒绝后原记录状态大小未被改写", after.get("状态大小") == before_size)
check("拒绝后原记录 status 未变", after.get("status") == before.get("status"))
check("拒绝后 enabled 未被置位", after.get("enabled") is not True)
entry, msg = state_svc.run_action(1, "启用后端", {
    "action": "启用后端", "状态大小": "  ", "快照频率": "5m", "清理间隔": "30m",
})
check("空白状态大小同样被拒绝", entry is None and "状态大小" in msg, msg)

print("场景4：重复启用只生效一次；配置变更才产生新版本，历史可追溯")
state_svc, cp_svc = fresh_services()
base = {"状态大小": "1.8GB", "快照频率": "5m", "清理间隔": "30m"}
state_svc.run_action(2, "启用后端", {"action": "启用后端", **base})
e2, m2 = state_svc.run_action(2, "启用后端", {"action": "启用后端", **base})
check("重复启用仍返回成功", e2 is not None, m2)
check("重复启用沿用存量版本(仍只有1个版本)", len(state_svc.list_versions(2)) == 1)
check("重复启用提示沿用存量版本", "沿用存量配置版本" in m2, m2)
e3, m3 = state_svc.run_action(2, "启用后端", {
    "action": "启用后端", "状态大小": "2.4GB", "快照频率": "5m", "清理间隔": "30m",
})
check("配置变更后启用成功", e3 is not None and e3.get("状态大小") == "2.4GB", m3)
versions = state_svc.list_versions(2)
check("配置变更产生第2个版本", len(versions) == 2, str(len(versions)))
check("当前版本号更新为2", state_svc.get_entry(2).get("配置版本号") == 2)
check("历史版本v1仍保留旧值", versions[1].get("config", {}).get("状态大小") == "1.8GB")
check("最新版本v2为新值", versions[0].get("config", {}).get("状态大小") == "2.4GB")
check("版本列表按版本号倒序", [v.get("version") for v in versions] == [2, 1])

print("场景5：并发启用只接受一份配置快照")
state_svc, cp_svc = fresh_services()
results: list[object] = []
errors: list[str] = []


def concurrent_enable(size: str) -> None:
    try:
        entry, msg = state_svc.run_action(2, "启用后端", {
            "action": "启用后端", "状态大小": size, "快照频率": "5m", "清理间隔": "30m",
        })
        results.append((size, entry.get("状态大小") if entry else None, msg))
    except Exception:  # noqa: BLE001
        errors.append(traceback.format_exc())


threads = [
    threading.Thread(target=concurrent_enable, args=("1.8GB",)),
    threading.Thread(target=concurrent_enable, args=("3.6GB",)),
    threading.Thread(target=concurrent_enable, args=("1.8GB",)),
]
for t in threads:
    t.start()
for t in threads:
    t.join()
check("并发启用无异常", not errors)
versions = state_svc.list_versions(2)
check("并发后版本数不超过不同配置数(1~3)", 1 <= len(versions) <= 3, str(len(versions)))
current = state_svc.get_entry(2)
winner = versions[0]["config"]["状态大小"]
check("当前记录与最新版本口径一致", current.get("状态大小") == winner)
# 并发全部结束后，任何新的读取只能拿到同一个权威结果
fresh1 = state_svc.get_entry(2).get("状态大小")
fresh2 = state_svc.latest_version(2)["config"]["状态大小"]
fresh3 = state_svc.list_entries(keyword="STAT-0002")[0][0].get("状态大小")
check("并发后所有新读取只返回一个权威结果", fresh1 == fresh2 == fresh3 == winner,
      f"{fresh1}/{fresh2}/{fresh3}/{winner}")
# 相同配置的并发请求必须去重
state_svc, cp_svc = fresh_services()


def same_enable() -> None:
    state_svc.run_action(
        2, "启用后端",
        {"action": "启用后端", "状态大小": "1.8GB", "快照频率": "5m", "清理间隔": "30m"},
    )


threads = [threading.Thread(target=same_enable) for _ in range(5)]
for t in threads:
    t.start()
for t in threads:
    t.join()
check("5个相同配置并发只保留1个版本", len(state_svc.list_versions(2)) == 1,
      str(len(state_svc.list_versions(2))))

print("场景6：落库失败时保留上一份完整配置，检查点恢复继续可读")
state_svc, cp_svc = fresh_services()
state_svc.run_action(2, "启用后端", {"action": "启用后端", **base})
good_version = state_svc.latest_version(2)
original_save = store.save_version


def broken_save(module: str, entry_id: int, snapshot: dict):  # noqa: ANN001
    raise RuntimeError("模拟配置版本落库失败")


store.save_version = broken_save  # type: ignore[method-assign]
entry, msg = state_svc.run_action(2, "启用后端", {
    "action": "启用后端", "状态大小": "9.9GB", "快照频率": "1m", "清理间隔": "5m",
})
store.save_version = original_save  # type: ignore[method-assign]
check("落库失败时启用返回失败", entry is None)
check("失败信息提示保留上一份配置", entry is None and "上一份完整配置" in msg, msg)
check("失败后仍只有1个版本", len(state_svc.list_versions(2)) == 1)
check("上一份完整配置未被覆盖", state_svc.latest_version(2).get("id") == good_version.get("id"))
current = state_svc.get_entry(2)
check("当前记录保留旧状态大小(未写入9.9GB)", current.get("状态大小") == "1.8GB")
check("当前记录仍为已启用", current.get("status") == "已启用" and current.get("enabled") is True)

print("场景7：检查点恢复消费同一份版本数据，且不回写旧状态大小")
state_svc, cp_svc = fresh_services()
state_svc.run_action(2, "启用后端", {"action": "启用后端", **base})
# 检查点缓存里残留旧的状态大小（与刚启用的配置冲突）
cp_entry = cp_svc.get_entry(2)
cp_entry["状态大小"] = "旧缓存512MB"
restored, rmsg = cp_svc.run_action(2, "恢复状态")
check("CHEC-0002 恢复成功", restored is not None and restored.get("status") == "已恢复", rmsg)
check("恢复消息引用了配置版本", "v1" in rmsg, rmsg)
check("恢复侧记录版本号", restored and restored.get("状态后端配置版本号") == 1)
check("恢复侧读取版本快照的状态大小(非缓存旧值)",
      restored and restored.get("恢复读取状态大小") == "1.8GB")
check("恢复侧读取快照频率", restored and restored.get("恢复读取快照频率") == "5m")
check("恢复侧读取清理间隔", restored and restored.get("恢复读取清理间隔") == "30m")
# 关键：状态后端当前记录没有被检查点缓存旧值覆盖
state_now = state_svc.get_entry(2)
check("状态后端记录未被旧缓存覆盖", state_now.get("状态大小") == "1.8GB")
check("状态后端记录不含旧缓存值", state_now.get("状态大小") != "旧缓存512MB")
# 再启用一份新配置后，恢复侧只能读到唯一可核对的新版本
state_svc.run_action(2, "启用后端", {
    "action": "启用后端", "状态大小": "2.4GB", "快照频率": "10m", "清理间隔": "20m",
})
restored2, rmsg2 = cp_svc.run_action(2, "恢复状态")
check("再次恢复读到最新版本v2", restored2 and restored2.get("状态后端配置版本号") == 2, rmsg2)
check("再次恢复读到新状态大小", restored2 and restored2.get("恢复读取状态大小") == "2.4GB")
check("状态后端记录仍为新值", state_svc.get_entry(2).get("状态大小") == "2.4GB")

print("场景8：状态后端未启用时检查点仍可恢复（CHEC 不被阻断）")
state_svc, cp_svc = fresh_services()
restored, rmsg = cp_svc.run_action(2, "恢复状态")
check("无启用版本时恢复仍成功", restored is not None and restored.get("status") == "已恢复", rmsg)
check("无启用版本时给出可读提示", "暂无可核对" in rmsg, rmsg)

print("场景9：暂停/关闭状态流转与 enabled 口径")
state_svc, cp_svc = fresh_services()
state_svc.run_action(2, "启用后端", {"action": "启用后端", **base})
paused, _ = state_svc.run_action(2, "暂停后端", {})
check("暂停成功", paused and paused.get("status") == "已暂停")
check("暂停保留配置值不丢", paused and paused.get("状态大小") == "1.8GB")
closed, _ = state_svc.run_action(2, "关闭后端", {})
check("关闭成功且 enabled 落库为 False", closed and closed.get("status") == "已关闭"
      and closed.get("enabled") is False)
check("关闭后版本历史仍可追溯", len(state_svc.list_versions(2)) == 1)

print("场景10：持久化后跨查询一致（模拟页面刷新/重新进入的取数逻辑）")
state_svc, cp_svc = fresh_services()
state_svc.run_action(2, "启用后端", {"action": "启用后端", **base})
items, total = state_svc.list_entries(keyword="STAT-0002")
check("列表取数能取到记录", total == 1 and items[0].get("状态大小") == "1.8GB")
detail = state_svc.get_entry(2)
check("详情取数与列表是同一条记录同口径", detail is items[0])
versions_again = state_svc.list_versions(2)
check("版本数据再次查询保持完整",
      versions_again[0]["config"]["状态大小"] == "1.8GB"
      and versions_again[0]["config"]["快照频率"] == "5m"
      and versions_again[0]["config"]["清理间隔"] == "30m")

print()
print(f"==== 验收结果：通过 {PASS} 项，失败 {FAIL} 项 ====")
sys.exit(1 if FAIL else 0)
