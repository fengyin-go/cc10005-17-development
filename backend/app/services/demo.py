"""本地演示页面：一个可观察的“报销单填报”网页，供检查点续做台取证据。

演示页有自己的页面状态和审计日志：
- 填表（fill）只改变表单回显，属于可观察但通常可安全的动作；
- 确认（confirm）每执行一次就生成一张新回执，是典型的副作用动作；
- 作废（cancel）每执行一次都会累加作废次数，所以重复回滚会再次改变页面；
- 登录（login）签发会话令牌。

执行器动作是否送达、页面是否已经接受，都以这里的审计事件和页面快照为准。
"""
from __future__ import annotations

import copy
import threading
import uuid
from datetime import datetime
from typing import Any


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _page_code(page_id: str) -> str:
    """demo-page-1 -> D1，用于生成可读的回执编号。"""
    tail = page_id.rsplit("-", 1)[-1]
    return f"D{tail}" if tail.isdigit() else "DX"


def _event(
    event_id: str,
    at: str,
    action: str,
    request_id: str,
    note: str,
    accepted: bool = True,
) -> dict[str, Any]:
    return {
        "event_id": event_id,
        "at": at,
        "action": action,
        "request_id": request_id,
        "accepted": accepted,
        "note": note,
    }


EXTRACT_KEYS = (
    "会话令牌",
    "登录账号",
    "申请人",
    "金额",
    "事由",
    "表单摘要",
    "回执编号",
    "回执状态",
    "提交次数",
    "作废次数",
)


def snapshot_value(snapshot: dict[str, Any], key: str) -> str:
    """从页面快照里按“结果提取”字段取值，供检查和证据核对复用。"""
    form = snapshot.get("表单") or {}
    receipt = snapshot.get("回执") or {}
    login = snapshot.get("登录") or {}
    candidates: dict[str, Any] = {
        "会话令牌": login.get("会话令牌"),
        "登录账号": login.get("账号"),
        "申请人": form.get("申请人"),
        "金额": form.get("金额"),
        "事由": form.get("事由"),
        "表单摘要": f"{form.get('申请人') or '—'} / {form.get('金额') or '—'} / {form.get('事由') or '—'}",
        "回执编号": receipt.get("编号"),
        "回执状态": receipt.get("状态"),
        "提交次数": snapshot.get("提交次数"),
        "作废次数": snapshot.get("作废次数"),
    }
    value = candidates.get(key)
    return "—" if value in (None, "") else str(value)


class DemoService:
    """内存演示页。真实项目里这里是一张真实网页 + 浏览器驱动。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._pages: dict[str, dict[str, Any]] = {}
        self._seed()

    # ---------- 初始化 ----------
    def _seed(self) -> None:
        self._pages = {
            "demo-page-1": {
                "page_id": "demo-page-1",
                "title": "报销单填报（本地演示页）",
                "form": {"申请人": "陈晓", "金额": "1280.00", "事由": "差旅报销"},
                "login": {"状态": "已登录", "账号": "fin@example.com", "会话令牌": "SESSION-D1-9F2A"},
                "receipt": {"编号": "RCPT-D1-001", "状态": "已受理"},
                "提交次数": 1,
                "作废次数": 0,
                "audit": [
                    _event("EVT-D1-001", "2026-11-13 09:41:22", "login", "REQ-1001", "表单登录成功，签发会话令牌 SESSION-D1-9F2A"),
                    _event("EVT-D1-002", "2026-11-13 09:42:18", "fill", "REQ-1002", "表单字段已回显：申请人 陈晓，金额 1280.00"),
                    _event("EVT-D1-003", "2026-11-13 09:43:05", "confirm", "REQ-1003", "受理成功，生成回执 RCPT-D1-001"),
                ],
                "updated_at": "2026-11-13 09:43:05",
            },
            "demo-page-2": {
                "page_id": "demo-page-2",
                "title": "报销单填报（本地演示页）",
                "form": {"申请人": "李昊", "金额": "560.00", "事由": "市内交通"},
                "login": {"状态": "已登录", "账号": "audit@example.com", "会话令牌": "SESSION-D2-7B41"},
                "receipt": {"编号": None, "状态": "未提交"},
                "提交次数": 0,
                "作废次数": 0,
                "audit": [
                    _event("EVT-D2-001", "2026-11-13 10:15:40", "login", "REQ-2001", "表单登录成功，签发会话令牌 SESSION-D2-7B41"),
                    _event("EVT-D2-002", "2026-11-13 10:16:22", "fill", "REQ-2002", "表单字段已回显：申请人 李昊，金额 560.00"),
                ],
                "updated_at": "2026-11-13 10:16:22",
            },
            "demo-page-3": {
                "page_id": "demo-page-3",
                "title": "报销单填报（本地演示页）",
                "form": {"申请人": "王岚", "金额": "3200.00", "事由": "差旅费"},
                "login": {"状态": "已登录", "账号": "wanglan@example.com", "会话令牌": "SESSION-D3-7C21"},
                "receipt": {"编号": "RCPT-D3-001", "状态": "已受理"},
                "提交次数": 1,
                "作废次数": 0,
                "audit": [
                    _event("EVT-D3-001", "2026-11-13 10:58:11", "login", "REQ-DEMO-3001", "表单登录成功，签发会话令牌 SESSION-D3-7C21"),
                    _event("EVT-D3-002", "2026-11-13 10:59:02", "fill", "REQ-DEMO-3002", "表单字段已回显：申请人 王岚，金额 3200.00"),
                    _event("EVT-D3-003", "2026-11-13 11:00:37", "confirm", "REQ-DEMO-3003", "受理成功，生成回执 RCPT-D3-001"),
                ],
                "updated_at": "2026-11-13 11:00:37",
            },
            "demo-page-4": {
                "page_id": "demo-page-4",
                "title": "报销单填报（本地演示页）",
                "form": {"申请人": "周宁", "金额": "880.00", "事由": "客户拜访"},
                "login": {"状态": "已登录", "账号": "ops@example.com", "会话令牌": "SESSION-D4-3E11"},
                "receipt": {"编号": "RCPT-D4-001", "状态": "已受理"},
                "提交次数": 1,
                "作废次数": 0,
                "audit": [
                    _event("EVT-D4-001", "2026-11-13 11:06:40", "login", "REQ-CP0004-01-01-58C0", "表单登录成功，签发会话令牌 SESSION-D4-3E11"),
                    _event("EVT-D4-002", "2026-11-13 11:06:58", "fill", "REQ-CP0004-01-02-9A31", "表单字段已回显：申请人 周宁，金额 880.00"),
                    _event("EVT-D4-003", "2026-11-13 11:07:22", "confirm", "REQ-CP0004-02-01-2C9B", "受理成功，生成回执 RCPT-D4-001"),
                ],
                "updated_at": "2026-11-13 11:07:22",
            },
        }

    def reset(self) -> None:
        with self._lock:
            self._seed()

    # ---------- 查询 ----------
    def has_page(self, page_id: str) -> bool:
        return page_id in self._pages

    def state(self, page_id: str) -> dict[str, Any] | None:
        with self._lock:
            page = self._pages.get(page_id)
            return copy.deepcopy(page) if page else None

    def page_view(self, page_id: str) -> dict[str, Any] | None:
        """演示页的对外结构：中文键名，前端与证据区共用同一套字段。"""
        with self._lock:
            page = self._pages.get(page_id)
            if page is None:
                return None
            return {
                "页面编号": page["page_id"],
                "标题": page["title"],
                "表单": copy.deepcopy(page["form"]),
                "登录": copy.deepcopy(page["login"]),
                "回执": copy.deepcopy(page["receipt"]),
                "提交次数": page["提交次数"],
                "作废次数": page["作废次数"],
                "审计": copy.deepcopy(page["audit"]),
                "更新时间": page["updated_at"],
            }

    def snapshot(self, page_id: str) -> dict[str, Any]:
        """给检查点留档用的页面快照，去掉审计日志避免重复膨胀。"""
        with self._lock:
            page = self._pages.get(page_id)
            if page is None:
                return {}
            return {
                "页面编号": page["page_id"],
                "表单": copy.deepcopy(page["form"]),
                "登录": copy.deepcopy(page["login"]),
                "回执": copy.deepcopy(page["receipt"]),
                "提交次数": page["提交次数"],
                "作废次数": page["作废次数"],
            }

    def audit_tail(self, page_id: str, limit: int = 5) -> list[dict[str, Any]]:
        with self._lock:
            page = self._pages.get(page_id)
            if page is None:
                return []
            return copy.deepcopy(page["audit"][-limit:])

    def matched_events(self, page_id: str, request_id: str) -> list[dict[str, Any]]:
        with self._lock:
            page = self._pages.get(page_id)
            if page is None:
                return []
            return [
                copy.deepcopy(event)
                for event in page["audit"]
                if event.get("request_id") == request_id
            ]

    def extract(self, page_id: str, key: str) -> str:
        """按步骤卡里的“结果提取”定义，从页面当前状态里取值。"""
        with self._lock:
            page = self._pages.get(page_id)
            if page is None:
                return "—"
            return snapshot_value(self.snapshot(page_id), key)

    # ---------- 页面动作 ----------
    def perform(
        self,
        page_id: str,
        action: str,
        *,
        request_id: str,
        values: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        """在演示页上执行一个可观察动作；每次调用都会留下审计事件。"""
        values = values or {}
        with self._lock:
            page = self._pages.get(page_id)
            if page is None:
                return None, f"演示页面 {page_id} 不存在，动作未送达"
            if not request_id:
                request_id = f"REQ-{uuid.uuid4().hex[:8].upper()}"

            event_id = f"EVT-{_page_code(page_id)}-{len(page['audit']) + 1:03d}"
            at = now_text()

            if action == "login":
                account = str(values.get("account") or values.get("账号标识") or "demo@example.com")
                token = f"SESSION-{_page_code(page_id)}-{uuid.uuid4().hex[:6].upper()}"
                page["login"] = {"状态": "已登录", "账号": account, "会话令牌": token}
                event = _event(event_id, at, "login", request_id, f"表单登录成功，签发会话令牌 {token}")
                message = "演示页已接受登录动作"
            elif action == "fill":
                for field in ("申请人", "金额", "事由"):
                    if values.get(field) is not None:
                        page["form"][field] = str(values.get(field))
                summary = "，".join(f"{key} {page['form'][key]}" for key in ("申请人", "金额") if page["form"].get(key))
                event = _event(event_id, at, "fill", request_id, f"表单字段已回显：{summary or '空表单'}")
                message = "演示页已接受填表动作"
            elif action == "confirm":
                page["提交次数"] += 1
                code = _page_code(page_id)
                receipt_id = f"RCPT-{code}-{page['提交次数']:03d}"
                page["receipt"] = {"编号": receipt_id, "状态": "已受理"}
                duplicate = "（重复提交，生成新回执）" if page["提交次数"] > 1 else ""
                event = _event(event_id, at, "confirm", request_id, f"受理成功，生成回执 {receipt_id}{duplicate}")
                message = "演示页已接受确认动作并生成回执"
            elif action == "cancel":
                page["作废次数"] += 1
                previous = page["receipt"]
                page["receipt"] = {"编号": previous.get("编号"), "状态": "已作废"}
                note = (
                    f"第 {page['作废次数']} 次执行作废，回执 {previous.get('编号') or '（无）'} 状态改为已作废"
                )
                if page["作废次数"] > 1:
                    note += "；页面已被再次改变"
                event = _event(event_id, at, "cancel", request_id, note)
                message = "演示页已接受作废动作"
            elif action == "observe":
                event = _event(event_id, at, "observe", request_id, "只读取页面状态，未改变页面")
                message = "已读取演示页当前状态"
            else:
                return None, f"演示页不认识动作「{action}」"

            page["audit"].append(event)
            page["updated_at"] = at
            return {
                "ok": True,
                "message": message,
                "event": copy.deepcopy(event),
                "snapshot": self.snapshot(page_id),
            }, message


demo_service = DemoService()
