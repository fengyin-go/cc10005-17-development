"""内存数据仓库：给每个业务模块准备一份可筛选、可流转的示例数据。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。
"""
from __future__ import annotations

import copy
import threading
from typing import Any

from app.seed import SEED_ROWS

# 检查点台内部表不进运营概览；检查点本身按业务模块展示
OVERVIEW_NAMES = {
    "site": "站点管理",
    "template": "抽取模板",
    "proxy": "代理池管理",
    "spider": "采集任务",
    "schedule": "调度策略",
    "antibot": "反爬对抗",
    "ratelimit": "限速控制",
    "retry": "失败重试",
    "extractor": "字段映射",
    "pipeline": "采集流水线",
    "validator": "结果校验",
    "storage": "数据落盘",
    "monitor": "采集监控",
    "cleanse": "数据清洗",
    "delta": "增量采集",
    "export": "数据分发",
    "rpaflow": "RPA流程",
    "rpa_checkpoint": "RPA检查点",
    "auth": "登录凭证",
}
OVERVIEW_SKIP = {"rpa_lease", "rpa_stale_receipt"}


class Store:
    def __init__(self) -> None:
        self._tables: dict[str, list[dict[str, Any]]] = {
            name: [copy.deepcopy(row) for row in rows] for name, rows in SEED_ROWS.items()
        }
        self._counters: dict[str, int] = {}
        self._lock = threading.RLock()

    def lock(self) -> threading.RLock:
        """检查点续做要求“多步之间没有别的请求插进来”，统一用同一把可重入锁。"""
        return self._lock

    def counter(self, name: str) -> int:
        return int(self._counters.get(name, 0))

    def set_counter(self, name: str, value: int) -> None:
        self._counters[name] = int(value)

    def module_names(self) -> list[str]:
        return sorted(self._tables)

    def rows(self, module: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(module, [])

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    def overview(self) -> dict[str, object]:
        modules: list[dict[str, object]] = []
        for name in self.module_names():
            if name in OVERVIEW_SKIP:
                continue
            rows = self.rows(name)
            modules.append({
                "name": OVERVIEW_NAMES.get(name, name),
                "key": name,
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
