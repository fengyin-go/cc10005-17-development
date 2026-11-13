"""RPA检查点续做台业务规则。

围绕“动作已发出但副作用未知”组织排障：

- 执行失败先进入证据核对区，不直接重试；
- 用户判断页面已接受 → 补记结果并推进到下一卡；
- 判断未接受 → 才允许重试同一张卡；
- 无法判明 → 流程停在当前卡并解释原因；
- 中断保留未决卡，恢复时既不自动从第一步重放，也不越过未决卡；
- 登录凭证在认证卡被消费；过期凭证阻止领取下一张认证卡；重新绑定不解除副作用核对要求；
- 并行续做只颁发一份执行权，失权执行者的迟到回执进入旧批次证据区（不推进步骤、不生成新检查点）；
- 编辑流程形成新版本，在途卡继续按旧版的等待条件与回滚定义执行；
- 回滚本身也是副作用动作，回滚失败同样要核对是否已改变演示页，避免重试回滚再次改变页面。

状态只在 service 里流转；路由层只负责组装出入参。
"""
from __future__ import annotations

import copy
import threading
import uuid
from datetime import date, datetime, timedelta
from typing import Any

from app.services.demo import demo_service, snapshot_value
from app.store import store

MODULE = "rpaflow"
CHECKPOINT_MODULE = "rpa_checkpoint"
LEASE_MODULE = "rpa_lease"
STALE_RECEIPT_MODULE = "rpa_stale_receipt"
AUTH_MODULE = "auth"

CARD_KINDS = ["认证", "填表", "确认", "观察"]
UNRESOLVED = {"待核对", "待补记", "已阻断"}
DONE = {"已完成", "已跳过", "已回滚"}

VERDICTS = {
    "已接受": "已接受",
    "页面已接受": "已接受",
    "未接受": "未接受",
    "页面未接受": "未接受",
    "无法判明": "无法判明",
}
LEASE_TTL_SECONDS = 300


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _lock() -> threading.RLock:
    return store.lock()


def _batch_code(checkpoint: dict[str, Any]) -> str:
    return str(checkpoint.get("检查点编号") or f"CP-{checkpoint['id']}").replace("-", "")


def _new_request_id(checkpoint: dict[str, Any], step: dict[str, Any]) -> str:
    attempt = int(step.get("执行次数", 0)) + 1
    return f"REQ-{_batch_code(checkpoint)}-{int(step['seq']) + 1:02d}-{attempt:02d}-{uuid.uuid4().hex[:4].upper()}"


def _log(checkpoint: dict[str, Any], actor: str, action: str, message: str) -> None:
    checkpoint.setdefault("事件", []).append(
        {"at": now_text(), "actor": actor, "action": action, "message": message}
    )


def _find_credential(reference: str) -> dict[str, Any] | None:
    for row in store.rows(AUTH_MODULE):
        if str(row.get("凭证编号")) == reference or str(row.get("id")) == reference:
            return row
    return None


def _credential_status(row: dict[str, Any]) -> str:
    return str(row.get("status") or "")


def _parse_date(text: Any) -> date | None:
    try:
        return date.fromisoformat(str(text or "").strip())
    except ValueError:
        return None


def _snapshot_text(snapshot: dict[str, Any]) -> str:
    """把页面快照里的可读文本拼起来，供“文本包含”等待条件使用。"""
    parts: list[str] = []
    for group in ("表单", "回执", "登录"):
        block = snapshot.get(group)
        if isinstance(block, dict):
            parts.extend(str(value) for value in block.values() if value not in (None, ""))
    return " ".join(parts)


def _wait_check(snapshot: dict[str, Any], wait: dict[str, Any]) -> tuple[bool, str]:
    """按等待条件判断页面是否已经到达预期状态；演示页即时返回，不做真实轮询。"""
    mode = str(wait.get("mode") or "元素出现")
    expr = str(wait.get("expr") or "")
    timeout = wait.get("timeout")
    if mode == "固定等待":
        return True, f"固定等待 {timeout or '—'} 秒后继续（演示页即时返回，未真实等待）"
    if mode == "回执编号生成":
        receipt = (snapshot or {}).get("回执") or {}
        if receipt.get("编号"):
            return True, f"回执编号 {receipt['编号']} 已生成"
        return False, "页面上还没有回执编号"
    value = snapshot_value(snapshot or {}, expr) if expr else "—"
    if mode == "文本包含":
        text = _snapshot_text(snapshot or {})
        ok = bool(expr) and expr in text
        return ok, (f"页面文本包含「{expr}」" if ok else f"页面文本未包含「{expr}」")
    if mode == "元素出现":
        ok = value not in ("", "—")
        return ok, (f"等待条件「{expr}」已出现，当前值 {value}" if ok else f"等待条件「{expr}」尚未出现")
    return False, f"演示页不认识等待模式「{mode}」"


def _card_public(step: dict[str, Any]) -> dict[str, Any]:
    """把步骤卡整理成前端可直接渲染的结构，避免前端理解内部字段。"""
    card = copy.deepcopy(step)
    card["能否执行"] = step.get("step_status") in {"未开始"} and not step.get("回滚", {}).get("状态")
    card["需要核对"] = step.get("step_status") in {"待核对", "已阻断", "待补记"}
    card["回滚"] = copy.deepcopy(step.get("回滚") or {})
    return card


class RpaflowService:
    # ================= 原有 RPA 流程列表 =================
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
            rows = [row for row in rows if keyword in str(row.get("流程编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        required = ["流程编号", "目标页面", "操作序列"]
        missing = [field for field in required if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in required})
        entry["status"] = "草稿"
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"自动化流程 {entry_id} 不存在或已归档"
        rules = {"提交执行": "待校验", "启动流程": "执行中", "终止流程": "已失败", "归档结果": "已完成"}
        if action not in rules:
            return None, f"动作「{action}」不属于RPA流程可执行范围"
        target = rules[action]
        entry["status"] = target
        entry["pending"] = target != "已完成"
        entry["abnormal"] = action == "终止流程"
        return entry, f"自动化流程已{action}"

    # ================= 检查点台：概览 =================
    def desk(self) -> dict[str, Any]:
        """续做台总览：检查点计数、有效执行权与旧批次回执量。"""
        with _lock():
            checkpoints = store.rows(CHECKPOINT_MODULE)
            steps = [step for checkpoint in checkpoints for step in checkpoint.get("steps") or []]
            cards = [
                {"label": "检查点", "value": len(checkpoints)},
                {"label": "待核对卡", "value": sum(1 for step in steps if step.get("step_status") == "待核对")},
                {"label": "阻断卡", "value": sum(1 for step in steps if step.get("step_status") == "已阻断")},
                {"label": "旧批次回执", "value": len(store.rows(STALE_RECEIPT_MODULE))},
            ]
            leases: list[dict[str, Any]] = []
            for checkpoint in checkpoints:
                lease = self._active_lease(int(checkpoint["id"]), touch=False)
                if lease is not None:
                    leases.append(
                        {
                            "checkpoint_id": int(checkpoint["id"]),
                            "检查点编号": checkpoint.get("检查点编号"),
                            "holder": lease.get("holder"),
                            "batch": lease.get("batch"),
                            "expires_at": lease.get("expires_at"),
                        }
                    )
            return {"cards": cards, "leases": copy.deepcopy(leases), "checkpoints": copy.deepcopy(checkpoints)}

    # ================= 检查点台：查询 =================
    def list_checkpoints(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        with _lock():
            rows = store.rows(CHECKPOINT_MODULE)
            if keyword:
                rows = [
                    row
                    for row in rows
                    if keyword in str(row.get("检查点编号", "")) or keyword in str(row.get("检查点名", ""))
                ]
            if status:
                rows = [row for row in rows if row.get("续做状态") == status]
            public = [self._summarize(row) for row in rows]
            return public, len(public)

    def _summarize(self, checkpoint: dict[str, Any]) -> dict[str, Any]:
        steps = checkpoint.get("steps") or []
        current = None
        current_seq = int(checkpoint.get("当前卡序", 0))
        if 0 <= current_seq < len(steps):
            current = steps[current_seq]
        lease = self._active_lease(int(checkpoint["id"]), touch=False)
        return {
            "id": checkpoint["id"],
            "检查点编号": checkpoint.get("检查点编号"),
            "检查点名": checkpoint.get("检查点名"),
            "流程编号": checkpoint.get("流程编号"),
            "页面编号": checkpoint.get("页面编号"),
            "续做状态": checkpoint.get("续做状态"),
            "当前版本": checkpoint.get("当前版本"),
            "执行批次": checkpoint.get("执行批次"),
            "当前卡序": checkpoint.get("当前卡序"),
            "当前卡": {
                "seq": current.get("seq"),
                "卡名": current.get("卡名"),
                "动作类型": current.get("动作类型"),
                "step_status": current.get("step_status"),
            } if current else None,
            "卡片总数": len(steps),
            "未决卡数": sum(1 for step in steps if step.get("step_status") in UNRESOLVED),
            "执行权": {
                "holder": lease.get("holder"),
                "batch": lease.get("batch"),
                "expires_at": lease.get("expires_at"),
            } if lease else None,
            "issue": checkpoint.get("issue"),
        }

    def _active_lease(self, checkpoint_id: int, *, touch: bool = True) -> dict[str, Any] | None:
        now = now_text()
        active = None
        for lease in store.rows(LEASE_MODULE):
            if int(lease.get("checkpoint_id", 0)) != checkpoint_id:
                continue
            if lease.get("state") != "执行权有效":
                continue
            if str(lease.get("expires_at") or "") <= now:
                if touch:
                    lease["state"] = "已失效"
                    lease["released_at"] = now
                    lease["release_reason"] = "执行权到期未续领"
                continue
            active = lease
        return active

    def get_checkpoint_detail(self, checkpoint_id: int) -> tuple[dict[str, Any] | None, str]:
        with _lock():
            checkpoint = store.find(CHECKPOINT_MODULE, checkpoint_id)
            if checkpoint is None:
                return None, f"检查点 {checkpoint_id} 不存在"
            page_id = checkpoint.get("页面编号")
            lease = self._active_lease(checkpoint_id, touch=False)
            steps = [_card_public(step) for step in checkpoint.get("steps") or []]
            current = None
            seq = int(checkpoint.get("当前卡序", 0))
            if 0 <= seq < len(steps):
                current = steps[seq]
            return {
                "checkpoint": copy.deepcopy(checkpoint),
                "cards": steps,
                "current_card": current,
                "lease": copy.deepcopy(lease) if lease else None,
                "page": {
                    "页面编号": page_id,
                    "快照": demo_service.snapshot(page_id) if page_id else {},
                    "审计": demo_service.audit_tail(page_id, 8) if page_id else [],
                },
                "revisions": copy.deepcopy(checkpoint.get("revisions") or []),
                "stale_receipts": [
                    copy.deepcopy(row)
                    for row in store.rows(STALE_RECEIPT_MODULE)
                    if int(row.get("checkpoint_id", 0)) == checkpoint_id
                ],
            }, ""

    def list_revisions(self, checkpoint_id: int) -> tuple[list[dict[str, Any]] | None, str]:
        checkpoint = store.find(CHECKPOINT_MODULE, checkpoint_id)
        if checkpoint is None:
            return None, f"检查点 {checkpoint_id} 不存在"
        return copy.deepcopy(checkpoint.get("revisions") or []), ""

    def list_stale_receipts(self, checkpoint_id: int | None = None) -> list[dict[str, Any]]:
        with _lock():
            rows = store.rows(STALE_RECEIPT_MODULE)
            if checkpoint_id is not None:
                rows = [row for row in rows if int(row.get("checkpoint_id", 0)) == checkpoint_id]
            return copy.deepcopy(rows)

    # ================= 执行权 =================
    def _issue_lease(self, checkpoint: dict[str, Any], holder: str) -> dict[str, Any]:
        number = int(checkpoint.get("批次序号", 0)) + 1
        checkpoint["批次序号"] = number
        batch = f"BATCH-{_batch_code(checkpoint)}-B{number}"
        checkpoint["执行批次"] = batch
        lease = {
            "id": max((int(row.get("id", 0)) for row in store.rows(LEASE_MODULE)), default=0) + 1,
            "checkpoint_id": int(checkpoint["id"]),
            "holder": holder,
            "batch": batch,
            "token": f"LEASE-{_batch_code(checkpoint)}-{uuid.uuid4().hex[:8].upper()}",
            "state": "执行权有效",
            "acquired_at": now_text(),
            "expires_at": (datetime.now() + timedelta(seconds=LEASE_TTL_SECONDS)).strftime("%Y-%m-%d %H:%M:%S"),
            "released_at": None,
        }
        store.rows(LEASE_MODULE).append(lease)
        _log(checkpoint, holder, "领取执行权", f"颁发执行权，批次 {batch}")
        return lease

    def acquire_lease(self, checkpoint_id: int, operator: str, token: str | None = None) -> tuple[bool, str, dict[str, Any] | None]:
        with _lock():
            checkpoint = store.find(CHECKPOINT_MODULE, checkpoint_id)
            if checkpoint is None:
                return False, f"检查点 {checkpoint_id} 不存在", None
            lease = self._active_lease(checkpoint_id)
            if lease is not None:
                if token and token == lease.get("token"):
                    return True, f"执行权仍由 {lease['holder']} 持有，批次 {lease['batch']}", {
                        "checkpoint": self._summarize(checkpoint),
                        "lease": copy.deepcopy(lease),
                    }
                return False, (
                    f"检查点 {checkpoint_id} 的执行权已由 {lease['holder']} 持有（批次 {lease['batch']}），"
                    "并行续做只颁发一份执行权"
                ), {"checkpoint": self._summarize(checkpoint), "lease": copy.deepcopy(lease)}
            lease = self._issue_lease(checkpoint, operator or "值班管理员")
            return True, f"已颁发执行权，批次 {lease['batch']}", {
                "checkpoint": self._summarize(checkpoint),
                "lease": copy.deepcopy(lease),
            }

    def release_lease(self, checkpoint_id: int, token: str, operator: str = "") -> tuple[bool, str, dict[str, Any] | None]:
        with _lock():
            checkpoint = store.find(CHECKPOINT_MODULE, checkpoint_id)
            if checkpoint is None:
                return False, f"检查点 {checkpoint_id} 不存在", None
            lease = self._active_lease(checkpoint_id)
            if lease is None:
                return False, "当前没有有效执行权，无需释放", {"checkpoint": self._summarize(checkpoint)}
            if not token or token != lease.get("token"):
                return False, f"执行权令牌不匹配，当前持有者 {lease['holder']}", {
                    "checkpoint": self._summarize(checkpoint),
                    "lease": copy.deepcopy(lease),
                }
            lease["state"] = "已释放"
            lease["released_at"] = now_text()
            lease["release_reason"] = f"{operator or lease['holder']} 主动释放"
            _log(checkpoint, operator or lease["holder"], "释放执行权", f"批次 {lease['batch']} 执行权已释放")
            return True, "执行权已释放，下一轮领取会开启新批次", {
                "checkpoint": self._summarize(checkpoint),
                "lease": copy.deepcopy(lease),
            }

    def _ensure_lease(
        self, checkpoint: dict[str, Any], operator: str, token: str | None
    ) -> tuple[dict[str, Any] | None, str]:
        lease = self._active_lease(int(checkpoint["id"]))
        if lease is not None:
            if token and token == lease.get("token"):
                return lease, ""
            return None, (
                f"执行权由 {lease['holder']} 持有（批次 {lease['batch']}），"
                "本次操作未持有执行权，动作未发出；如为迟到回执请提交到旧批次证据区"
            )
        lease = self._issue_lease(checkpoint, operator or "值班管理员")
        return lease, ""

    # ================= 恢复续做 =================
    def resume(self, checkpoint_id: int, operator: str, token: str | None = None) -> tuple[bool, str, dict[str, Any] | None]:
        with _lock():
            checkpoint = store.find(CHECKPOINT_MODULE, checkpoint_id)
            if checkpoint is None:
                return False, f"检查点 {checkpoint_id} 不存在", None
            lease, error = self._ensure_lease(checkpoint, operator, token)
            if lease is None:
                return False, error, {"checkpoint": self._summarize(checkpoint)}
            current = self._current_step(checkpoint)
            if current is None:
                return False, "检查点已经完成，没有可续做的步骤卡", {"checkpoint": self._summarize(checkpoint)}
            status = str(current.get("step_status"))
            if status in UNRESOLVED:
                reason = checkpoint.get("issue") or f"第 {int(current['seq']) + 1} 张卡（{current['卡名']}）尚未完成证据核对"
                return False, (
                    f"恢复停在第 {int(current['seq']) + 1} 张卡：{reason}。"
                    "未决卡不会被跳过，也不会从第一步重放；请先在证据核对区给出结论"
                ), {"checkpoint": self._summarize(checkpoint), "current_card": _card_public(current), "lease": copy.deepcopy(lease)}
            if checkpoint.get("续做状态") in {"已完成", "已回滚"}:
                return False, f"检查点当前状态为{checkpoint.get('续做状态')}，无需恢复", {
                    "checkpoint": self._summarize(checkpoint)
                }
            checkpoint["续做状态"] = "执行中"
            checkpoint["issue"] = None
            _log(
                checkpoint,
                operator,
                "恢复续做",
                f"从第 {int(current['seq']) + 1} 张卡继续，之前完成的卡片未重放，批次 {lease['batch']}",
            )
            return True, (
                f"已从第 {int(current['seq']) + 1} 张卡（{current['卡名']}）继续，未自动重放之前的步骤；"
                f"当前批次 {lease['batch']}"
            ), {"checkpoint": self._summarize(checkpoint), "current_card": _card_public(current), "lease": copy.deepcopy(lease)}

    # ================= 执行步骤卡 =================
    def _current_step(self, checkpoint: dict[str, Any]) -> dict[str, Any] | None:
        steps = checkpoint.get("steps") or []
        seq = int(checkpoint.get("当前卡序", 0))
        if 0 <= seq < len(steps):
            return steps[seq]
        return None

    def _advance(self, checkpoint: dict[str, Any]) -> None:
        steps = checkpoint.get("steps") or []
        for step in steps:
            if step.get("step_status") not in DONE:
                checkpoint["当前卡序"] = int(step["seq"])
                if step.get("step_status") in UNRESOLVED:
                    checkpoint["续做状态"] = "待核对"
                else:
                    checkpoint["续做状态"] = "执行中"
                return
        checkpoint["当前卡序"] = len(steps)
        checkpoint["续做状态"] = "已完成"
        checkpoint["issue"] = None
        _log(checkpoint, "系统", "流程完成", "全部步骤卡均已完成")

    @staticmethod
    def _credential_expired(row: dict[str, Any]) -> bool:
        expiry = _parse_date(row.get("有效期至"))
        return expiry is not None and expiry < datetime.now().date()

    def _credential_blocker(self, step: dict[str, Any]) -> str:
        if step.get("动作类型") != "认证" or step.get("step_status") in DONE:
            return ""
        reference = str(step.get("凭证编号") or "").strip()
        if not reference:
            return "认证卡没有绑定凭证编号，不能领取下一张认证卡"
        row = _find_credential(reference)
        if row is None:
            return f"凭证 {reference} 不存在，不能领取下一张认证卡"
        consumed = row.get("消费检查点")
        if self._credential_expired(row):
            return (
                f"凭证 {reference} 有效期至 {row.get('有效期至')}，已过期，过期凭证不能领取下一张认证卡；"
                "请先重新绑定并更新有效期"
            )
        if _credential_status(row) != "已生效":
            detail = f"（已于 {row.get('消费时间')} 被检查点 {consumed} 的认证卡消费）" if consumed else ""
            return (
                f"凭证 {reference} 当前状态为{row.get('status')}{detail}，不能领取下一张认证卡；"
                "请先在登录凭证模块重新绑定并启用（重新绑定不解除未决卡的副作用核对）"
            )
        return ""

    def _consume_credential(self, checkpoint: dict[str, Any], step: dict[str, Any], request_id: str) -> None:
        if step.get("动作类型") != "认证":
            return
        row = _find_credential(str(step.get("凭证编号") or ""))
        if row is None:
            return
        row["status"] = "已失效"
        row["pending"] = False
        row["abnormal"] = True
        row["消费状态"] = "已消费"
        row["消费时间"] = now_text()
        row["消费批次"] = checkpoint.get("执行批次")
        row["消费检查点"] = checkpoint.get("检查点编号")
        row["消费卡"] = f"第 {int(step['seq']) + 1} 张卡（{step['卡名']}）"
        row["消费请求"] = request_id

    def _build_evidence(
        self,
        checkpoint: dict[str, Any],
        step: dict[str, Any],
        request_id: str,
        sent: bool,
        snapshot: dict[str, Any],
        wait_result: dict[str, Any],
        extracted: dict[str, Any],
        matched: list[dict[str, Any]],
        note: str,
        action: str | None = None,
    ) -> dict[str, Any]:
        page_id = checkpoint.get("页面编号")
        return {
            "request_id": request_id,
            "at": now_text(),
            "动作": action or step.get("执行动作"),
            "已送达": sent,
            "页面编号": page_id,
            "页面快照": snapshot,
            "匹配回执": matched,
            "页面审计": demo_service.audit_tail(page_id, 6) if page_id else [],
            "等待条件": copy.deepcopy(step.get("等待条件") or {}),
            "等待结果": wait_result,
            "结果提取": extracted,
            "核对提示": note,
        }

    def execute_step(self, checkpoint_id: int, operator: str, token: str | None = None) -> tuple[bool, str, dict[str, Any] | None]:
        with _lock():
            checkpoint = store.find(CHECKPOINT_MODULE, checkpoint_id)
            if checkpoint is None:
                return False, f"检查点 {checkpoint_id} 不存在", None
            lease, error = self._ensure_lease(checkpoint, operator, token)
            if lease is None:
                return False, error, {"checkpoint": self._summarize(checkpoint)}
            if checkpoint.get("续做状态") in {"已完成", "已回滚"}:
                return False, f"检查点当前状态为{checkpoint.get('续做状态')}，不能继续执行", {
                    "checkpoint": self._summarize(checkpoint)
                }
            step = self._current_step(checkpoint)
            if step is None:
                return False, "没有待执行的步骤卡", {"checkpoint": self._summarize(checkpoint)}
            status = str(step.get("step_status"))
            if status in UNRESOLVED:
                return False, (
                    f"第 {int(step['seq']) + 1} 张卡（{step['卡名']}）当前为{status}，"
                    "必须先完成证据核对：页面已接受则补记结果，未接受才可重试，无法判明则停止"
                ), {"checkpoint": self._summarize(checkpoint), "card": _card_public(step)}
            if status in DONE:
                return False, f"第 {int(step['seq']) + 1} 张卡已经是{status}，不能重复执行", {
                    "checkpoint": self._summarize(checkpoint)
                }
            blocker = self._credential_blocker(step)
            if blocker:
                checkpoint["issue"] = blocker
                _log(checkpoint, operator, "认证卡被阻止", blocker)
                return False, blocker, {"checkpoint": self._summarize(checkpoint), "card": _card_public(step), "lease": copy.deepcopy(lease)}
            rollback_state = (step.get("回滚") or {}).get("状态")
            if rollback_state in {"待核对", "已阻断"}:
                return False, (
                    f"第 {int(step['seq']) + 1} 张卡的回滚动作处于{rollback_state}，"
                    "先核对回滚自身的副作用，避免重复回滚再次改变演示页面"
                ), {"checkpoint": self._summarize(checkpoint), "card": _card_public(step)}

            request_id = _new_request_id(checkpoint, step)
            attempt = int(step.get("执行次数", 0)) + 1
            step["执行次数"] = attempt
            step["step_status"] = "执行中"
            checkpoint["续做状态"] = "执行中"
            checkpoint["issue"] = None
            checkpoint["当前卡序"] = int(step["seq"])
            dispatch_values = copy.deepcopy(step.get("页面参数") or {})
            simulate = str(dispatch_values.pop("模拟", "") or "").strip()
            page_id = str(checkpoint.get("页面编号") or "")
            sent = simulate != "未送达"

            if not sent:
                wait_ok, wait_note = False, "动作未送达，等待条件尚未评估"
                page_result: dict[str, Any] | None = None
                failure = "动作没有送达演示页（模拟网络中断），页面是否已接受该动作未知"
            else:
                page_result, page_message = demo_service.perform(
                    page_id, str(step.get("执行动作") or ""), request_id=request_id, values=dispatch_values
                )
                self._consume_credential(checkpoint, step, request_id)
                snapshot = demo_service.snapshot(page_id)
                wait_ok, wait_note = _wait_check(snapshot, step.get("等待条件") or {})
                if simulate == "无回执":
                    failure = "动作已发出，但执行器没有收到页面回执（模拟响应超时），副作用未知"
                elif page_result is None:
                    failure = f"演示页未受理该动作：{page_message}"
                elif not wait_ok:
                    failure = f"动作已发出，但等待条件未满足：{wait_note}"
                else:
                    failure = ""

            snapshot = demo_service.snapshot(page_id) if page_id else {}
            matched = demo_service.matched_events(page_id, request_id) if page_id else []
            wait_result = {"满足": wait_ok, "说明": wait_note, "超时秒数": (step.get("等待条件") or {}).get("timeout")}
            extract_key = str(step.get("结果提取") or "")
            extracted = {"字段": extract_key, "值": snapshot_value(snapshot, extract_key)}

            if failure:
                note = (
                    "动作已发出但副作用未知：请对照页面快照与匹配回执判断页面是否已接受。"
                    if sent
                    else "动作未送达：请核对页面是否已有其他批次留下的痕迹，再决定重试。"
                )
                evidence = self._build_evidence(checkpoint, step, request_id, sent, snapshot, wait_result, extracted, matched, note)
                step["证据"] = evidence
                step["最近执行"] = {
                    "request_id": request_id,
                    "at": now_text(),
                    "批次": lease["batch"],
                    "已送达": sent,
                    "结果": "待核对",
                    "说明": failure,
                }
                step["step_status"] = "待核对"
                checkpoint["续做状态"] = "待核对"
                checkpoint["issue"] = failure
                _log(checkpoint, operator, "执行失败", f"{step['卡名']}：{failure}")
                return False, f"{failure}。已进入证据核对区，未自动重试", {
                    "checkpoint": self._summarize(checkpoint),
                    "card": _card_public(step),
                    "evidence": evidence,
                    "lease": copy.deepcopy(lease),
                }

            step["证据"] = self._build_evidence(
                checkpoint,
                step,
                request_id,
                sent,
                snapshot,
                wait_result,
                extracted,
                matched,
                "动作已被页面接受，结果已按结果提取定义补记。",
            )
            step["最近执行"] = {
                "request_id": request_id,
                "at": now_text(),
                "批次": lease["batch"],
                "已送达": sent,
                "结果": "成功",
                "说明": page_message if sent else "",
            }
            step["结果"] = {"字段": extract_key, "值": extracted["值"], "at": now_text(), "request_id": request_id}
            step["step_status"] = "已完成"
            checkpoint["issue"] = None
            _log(checkpoint, operator, "执行成功", f"{step['卡名']} 执行成功，结果 {extract_key}={extracted['值']}")
            self._advance(checkpoint)
            return True, f"第 {int(step['seq']) + 1} 张卡执行成功，结果已补记：{extract_key}={extracted['值']}", {
                "checkpoint": self._summarize(checkpoint),
                "card": _card_public(step),
                "evidence": step["证据"],
                "lease": copy.deepcopy(lease),
            }

    # ================= 证据核对 =================
    def review_step(
        self,
        checkpoint_id: int,
        seq: int,
        decision: str,
        operator: str,
        note: str = "",
        token: str | None = None,
    ) -> tuple[bool, str, dict[str, Any] | None]:
        with _lock():
            checkpoint = store.find(CHECKPOINT_MODULE, checkpoint_id)
            if checkpoint is None:
                return False, f"检查点 {checkpoint_id} 不存在", None
            steps = checkpoint.get("steps") or []
            if seq < 0 or seq >= len(steps):
                return False, f"检查点 {checkpoint_id} 没有第 {seq + 1} 张卡", None
            step = steps[seq]
            status = str(step.get("step_status"))
            if status not in UNRESOLVED:
                return False, f"第 {seq + 1} 张卡当前为{status}，不在证据核对区", {
                    "checkpoint": self._summarize(checkpoint), "card": _card_public(step)
                }
            lease, error = self._ensure_lease(checkpoint, operator, token)
            if lease is None:
                return False, error, {"checkpoint": self._summarize(checkpoint)}
            verdict = VERDICTS.get(decision)
            if verdict is None:
                return False, "核对结论只能是「页面已接受」「页面未接受」「无法判明」", {
                    "checkpoint": self._summarize(checkpoint), "card": _card_public(step)
                }
            page_id = str(checkpoint.get("页面编号") or "")
            snapshot = demo_service.snapshot(page_id) if page_id else {}
            extract_key = str(step.get("结果提取") or "")
            review_record = {
                "结论": verdict,
                "at": now_text(),
                "actor": operator,
                "说明": note or "",
                "批次": lease["batch"],
                "页面快照": snapshot,
                "结果提取": {"字段": extract_key, "值": snapshot_value(snapshot, extract_key)},
            }
            step["核对"] = review_record
            step["证据"] = copy.deepcopy(step.get("证据") or {})

            if verdict == "已接受":
                if status == "待补记":
                    # 已接受且此前已补记结果，只补核对结论
                    pass
                value = snapshot_value(snapshot, extract_key)
                step["结果"] = {"字段": extract_key, "值": value, "at": now_text(), "request_id": (step.get("最近执行") or {}).get("request_id")}
                step["step_status"] = "已完成"
                step["最近执行"] = dict(step.get("最近执行") or {}, 结果="已接受并补记")
                checkpoint["issue"] = None
                checkpoint["续做状态"] = "执行中"
                _log(checkpoint, operator, "证据核对", f"第 {seq + 1} 张卡判定页面已接受，补记结果 {extract_key}={value}")
                self._advance(checkpoint)
                return True, (
                    f"已记入核对结论：页面已接受该动作，结果补记为 {extract_key}={value}，"
                    f"推进到下一张卡"
                ), {"checkpoint": self._summarize(checkpoint), "card": _card_public(step), "lease": copy.deepcopy(lease)}

            if verdict == "未接受":
                step["step_status"] = "未开始"
                step["最近执行"] = dict(step.get("最近执行") or {}, 结果="已核对未接受")
                checkpoint["续做状态"] = "执行中"
                checkpoint["issue"] = f"第 {seq + 1} 张卡已确认页面未接受该动作，可重试"
                checkpoint["当前卡序"] = seq
                _log(checkpoint, operator, "证据核对", f"第 {seq + 1} 张卡判定页面未接受，允许重试该卡")
                return True, (
                    f"已记入核对结论：页面未接受该动作，可以重试第 {seq + 1} 张卡；"
                    "重试后仍需先核对再推进"
                ), {"checkpoint": self._summarize(checkpoint), "card": _card_public(step), "lease": copy.deepcopy(lease)}

            step["step_status"] = "已阻断"
            step["最近执行"] = dict(step.get("最近执行") or {}, 结果="无法判明")
            checkpoint["续做状态"] = "已阻断"
            checkpoint["issue"] = (
                f"第 {seq + 1} 张卡无法判明页面是否已接受该动作，流程停止："
                "现有证据不足以确认副作用，重试可能重复提交，跳过会丢失结果"
            )
            _log(checkpoint, operator, "证据核对", checkpoint["issue"] + (f"；核对备注：{note}" if note else ""))
            return False, (
                checkpoint["issue"] + "。请补充页面证据或人工核实后再给出结论；在此之前禁止重试与推进"
            ), {"checkpoint": self._summarize(checkpoint), "card": _card_public(step), "lease": copy.deepcopy(lease)}

    # ================= 回滚（异常回滚同样要核对） =================
    def rollback_step(
        self,
        checkpoint_id: int,
        seq: int,
        operator: str,
        note: str = "",
        token: str | None = None,
    ) -> tuple[bool, str, dict[str, Any] | None]:
        with _lock():
            checkpoint = store.find(CHECKPOINT_MODULE, checkpoint_id)
            if checkpoint is None:
                return False, f"检查点 {checkpoint_id} 不存在", None
            steps = checkpoint.get("steps") or []
            if seq < 0 or seq >= len(steps):
                return False, f"检查点 {checkpoint_id} 没有第 {seq + 1} 张卡", None
            step = steps[seq]
            definition = step.get("回滚定义") or {}
            if not definition.get("action"):
                return False, f"第 {seq + 1} 张卡没有定义异常回滚动作", {
                    "checkpoint": self._summarize(checkpoint), "card": _card_public(step)
                }
            rollback = step.setdefault("回滚", {"状态": None, "次数": 0, "最近执行": None, "证据": None, "核对": None})
            if rollback.get("状态") in {"待核对", "已阻断"}:
                return False, (
                    f"第 {seq + 1} 张卡的回滚已发出但副作用未核对，"
                    "先完成回滚证据核对，避免重复回滚再次改变演示页面"
                ), {"checkpoint": self._summarize(checkpoint), "card": _card_public(step)}
            if rollback.get("状态") == "已完成":
                return False, f"第 {seq + 1} 张卡已经回滚完成，不能重复回滚", {
                    "checkpoint": self._summarize(checkpoint), "card": _card_public(step)
                }
            last = step.get("最近执行") or {}
            if not last or last.get("结果") in {"成功", "已接受并补记"}:
                return False, f"第 {seq + 1} 张卡没有异常执行记录，不需要回滚", {
                    "checkpoint": self._summarize(checkpoint), "card": _card_public(step)
                }
            lease, error = self._ensure_lease(checkpoint, operator, token)
            if lease is None:
                return False, error, {"checkpoint": self._summarize(checkpoint)}

            rollback_attempt = int(rollback.get("次数", 0)) + 1
            rollback["次数"] = rollback_attempt
            request_id = f"RB-{_batch_code(checkpoint)}-{seq + 1:02d}-{rollback_attempt:02d}-{uuid.uuid4().hex[:4].upper()}"
            params = copy.deepcopy(definition.get("页面参数") or {})
            simulate = str(params.pop("模拟", "") or "").strip()
            page_id = str(checkpoint.get("页面编号") or "")
            sent = simulate != "未送达"
            page_message = ""
            if sent:
                page_result, page_message = demo_service.perform(
                    page_id, str(definition["action"]), request_id=request_id, values=params
                )
                sent = page_result is not None

            snapshot = demo_service.snapshot(page_id) if page_id else {}
            matched = demo_service.matched_events(page_id, request_id) if page_id else []
            wait_ok, wait_note = (True, "回滚动作结果") if sent else (False, "回滚动作未送达")
            evidence = self._build_evidence(
                checkpoint,
                step,
                request_id,
                sent,
                snapshot,
                {"满足": wait_ok, "说明": wait_note},
                {"字段": "作废次数", "值": snapshot_value(snapshot, "作废次数")},
                matched,
                (
                    "回滚本身也是副作用动作：若页面已经接受，重试回滚会再次改变页面（作废次数会继续增加）。"
                    if not simulate
                    else "回滚动作已发出但回执未知，请先核对演示页是否已经作废。"
                ),
                action=str(definition["action"]),
            )
            rollback["证据"] = evidence
            rollback["最近执行"] = {
                "request_id": request_id,
                "at": now_text(),
                "批次": lease["batch"],
                "已送达": sent,
                "结果": "待核对",
                "说明": simulate or page_message,
            }
            if simulate == "无回执" or not sent:
                rollback["状态"] = "待核对"
                checkpoint["续做状态"] = "待核对"
                checkpoint["issue"] = "回滚动作已发出但副作用未知，需要先核对回滚是否已经改变演示页"
                _log(checkpoint, operator, "异常回滚待核对", checkpoint["issue"])
                return False, checkpoint["issue"], {
                    "checkpoint": self._summarize(checkpoint),
                    "card": _card_public(step),
                    "rollback_evidence": evidence,
                    "lease": copy.deepcopy(lease),
                }

            rollback["状态"] = "已完成"
            rollback["最近执行"]["结果"] = "成功"
            rollback["核对"] = {
                "结论": "已接受",
                "at": now_text(),
                "actor": operator,
                "说明": note or "回滚动作被页面接受",
            }
            step["step_status"] = "已回滚"
            checkpoint["续做状态"] = "已回滚"
            checkpoint["issue"] = f"第 {seq + 1} 张卡已按当前版本的回滚定义回滚"
            _log(checkpoint, operator, "异常回滚", f"第 {seq + 1} 张卡回滚完成，作废次数 {snapshot_value(snapshot, '作废次数')}")
            return True, f"第 {seq + 1} 张卡回滚完成，作废次数为 {snapshot_value(snapshot, '作废次数')}", {
                "checkpoint": self._summarize(checkpoint),
                "card": _card_public(step),
                "rollback_evidence": evidence,
                "lease": copy.deepcopy(lease),
            }

    def review_rollback(
        self,
        checkpoint_id: int,
        seq: int,
        decision: str,
        operator: str,
        note: str = "",
        token: str | None = None,
    ) -> tuple[bool, str, dict[str, Any] | None]:
        with _lock():
            checkpoint = store.find(CHECKPOINT_MODULE, checkpoint_id)
            if checkpoint is None:
                return False, f"检查点 {checkpoint_id} 不存在", None
            steps = checkpoint.get("steps") or []
            if seq < 0 or seq >= len(steps):
                return False, f"检查点 {checkpoint_id} 没有第 {seq + 1} 张卡", None
            step = steps[seq]
            rollback = step.get("回滚") or {}
            if rollback.get("状态") not in {"待核对", "已阻断"}:
                return False, f"第 {seq + 1} 张卡的回滚当前为{rollback.get('状态') or '未执行'}，不在核对区", {
                    "checkpoint": self._summarize(checkpoint), "card": _card_public(step)
                }
            lease, error = self._ensure_lease(checkpoint, operator, token)
            if lease is None:
                return False, error, {"checkpoint": self._summarize(checkpoint)}
            verdict = VERDICTS.get(decision)
            if verdict is None:
                return False, "核对结论只能是「页面已接受」「页面未接受」「无法判明」", {
                    "checkpoint": self._summarize(checkpoint), "card": _card_public(step)
                }
            page_id = str(checkpoint.get("页面编号") or "")
            snapshot = demo_service.snapshot(page_id) if page_id else {}
            rollback["核对"] = {
                "结论": verdict,
                "at": now_text(),
                "actor": operator,
                "说明": note or "",
                "批次": lease["batch"],
                "页面快照": snapshot,
            }
            if verdict == "已接受":
                rollback["状态"] = "已完成"
                step["step_status"] = "已回滚"
                checkpoint["续做状态"] = "已回滚"
                checkpoint["issue"] = f"第 {seq + 1} 张卡的回滚已被页面接受，不再重试回滚"
                _log(checkpoint, operator, "回滚核对", checkpoint["issue"])
                return True, checkpoint["issue"] + f"；当前作废次数 {snapshot_value(snapshot, '作废次数')}", {
                    "checkpoint": self._summarize(checkpoint), "card": _card_public(step), "lease": copy.deepcopy(lease)
                }
            if verdict == "未接受":
                rollback["状态"] = "未接受"
                checkpoint["issue"] = f"第 {seq + 1} 张卡的回滚已确认未被页面接受，可以重试回滚"
                checkpoint["续做状态"] = "执行中"
                checkpoint["当前卡序"] = seq
                _log(checkpoint, operator, "回滚核对", checkpoint["issue"])
                return True, checkpoint["issue"], {
                    "checkpoint": self._summarize(checkpoint), "card": _card_public(step), "lease": copy.deepcopy(lease)
                }
            rollback["状态"] = "已阻断"
            checkpoint["续做状态"] = "已阻断"
            checkpoint["issue"] = (
                f"第 {seq + 1} 张卡的回滚无法判明是否已改变演示页，流程停止："
                "重试回滚可能再次作废，继续执行则可能留下未回收的副作用"
            )
            _log(checkpoint, operator, "回滚核对", checkpoint["issue"])
            return False, checkpoint["issue"], {
                "checkpoint": self._summarize(checkpoint), "card": _card_public(step), "lease": copy.deepcopy(lease)
            }

    # ================= 迟到回执：旧批次证据区 =================
    def submit_late_receipt(
        self,
        checkpoint_id: int,
        operator: str,
        token: str | None,
        request_id: str,
        batch: str = "",
        note: str = "",
    ) -> tuple[bool, str, dict[str, Any] | None]:
        """失权执行者的迟到回执：只进入旧批次证据区，不推进步骤、不生成新检查点。"""
        with _lock():
            checkpoint = store.find(CHECKPOINT_MODULE, checkpoint_id)
            if checkpoint is None:
                return False, f"检查点 {checkpoint_id} 不存在", None
            active = self._active_lease(checkpoint_id, touch=False)
            if active is not None and token and token == active.get("token"):
                return False, (
                    f"该回执携带的是当前有效执行权（{active['holder']} / {active['batch']}），"
                    "属于正常执行通道，请通过「执行当前卡」提交，而不是旧批次证据区"
                ), {"checkpoint": self._summarize(checkpoint)}
            target_batch = batch or (active.get("batch") if active else checkpoint.get("执行批次"))
            if active is not None and target_batch == active.get("batch"):
                return False, (
                    f"批次 {target_batch} 仍是当前有效批次，迟到回执不能作为旧批次证据留档；"
                    "请确认执行权是否已经转移"
                ), {"checkpoint": self._summarize(checkpoint)}
            record = {
                "id": max((int(row.get("id", 0)) for row in store.rows(STALE_RECEIPT_MODULE)), default=0) + 1,
                "checkpoint_id": int(checkpoint["id"]),
                "batch": target_batch or "未署名批次",
                "holder": operator or "未署名执行者",
                "request_id": request_id,
                "seq": int(checkpoint.get("当前卡序", 0)),
                "received_at": now_text(),
                "note": note or "失权执行者的迟到回执，仅留档作为旧批次证据",
                "state": "旧批次证据",
                "token": token or None,
            }
            store.rows(STALE_RECEIPT_MODULE).append(record)
            if active is not None:
                record["note"] += f"；当前执行权由 {active['holder']} 持有（批次 {active['batch']}）"
            _log(
                checkpoint,
                operator,
                "迟到回执留档",
                f"批次 {record['batch']} 的迟到回执 {request_id} 已进入旧批次证据区，未推进步骤",
            )
            return True, (
                f"迟到回执已进入旧批次证据区（批次 {record['batch']}）：不推进步骤、不生成新检查点，"
                "如需采用该结果，请先在证据核对区给出结论"
            ), {"checkpoint": self._summarize(checkpoint), "receipt": copy.deepcopy(record)}

    # ================= 编辑流程：新版本 =================
    def edit_flow(
        self,
        checkpoint_id: int,
        steps_definitions: list[dict[str, Any]],
        operator: str,
        note: str = "",
    ) -> tuple[bool, str, dict[str, Any] | None]:
        with _lock():
            checkpoint = store.find(CHECKPOINT_MODULE, checkpoint_id)
            if checkpoint is None:
                return False, f"检查点 {checkpoint_id} 不存在", None
            if not steps_definitions:
                return False, "新版本至少要包含一张步骤卡", None
            old_version = int(checkpoint.get("当前版本", 1))
            old_steps = checkpoint.get("steps") or []
            current_seq = int(checkpoint.get("当前卡序", 0))
            if current_seq >= len(steps_definitions):
                return False, (
                    f"新版本只有 {len(steps_definitions)} 张卡，少于在途的第 {current_seq + 1} 张卡；"
                    "不能在编辑时裁掉在途卡"
                ), None
            new_version = old_version + 1
            in_flight = old_steps[current_seq] if current_seq < len(old_steps) else None
            building: list[dict[str, Any]] = []
            preserved = False
            for index, definition in enumerate(steps_definitions):
                existing = old_steps[index] if index < len(old_steps) else None
                kind = str(definition.get("动作类型") or (existing or {}).get("动作类型") or "观察")
                if kind not in CARD_KINDS:
                    return False, f"第 {index + 1} 张卡的动作类型「{kind}」不在允许范围：{'、'.join(CARD_KINDS)}", None
                inflight_card = (
                    in_flight is not None
                    and int(in_flight.get("seq", -1)) == index
                    and str(in_flight.get("step_status")) not in DONE
                )
                if inflight_card and existing is not None:
                    # 在途卡：动作、等待条件与回滚定义都沿用旧版，不因编辑而改变
                    card = copy.deepcopy(existing)
                    card["沿用版本"] = old_version
                    card["定义版本"] = old_version
                    preserved = True
                elif existing is not None and existing.get("step_status") in DONE:
                    card = copy.deepcopy(existing)
                else:
                    card = self._build_step(index, definition, new_version, existing)
                building.append(card)
            checkpoint["steps"] = building
            checkpoint["当前版本"] = new_version
            checkpoint["流程摘要"] = [
                {"seq": card["seq"], "卡名": card["卡名"], "定义版本": card["定义版本"], "沿用旧版": card.get("沿用版本") is not None}
                for card in building
            ]
            if preserved:
                checkpoint["定义说明"] = (
                    f"v{new_version} 已生效；在途第 {current_seq + 1} 张卡继续按 v{old_version} 的等待条件与回滚定义执行"
                )
            else:
                checkpoint["定义说明"] = f"v{new_version} 已生效，后续卡片按新版本执行"
            revision = {
                "版本": new_version,
                "at": now_text(),
                "actor": operator,
                "说明": note or "编辑流程形成新版本",
                "在途卡": {
                    "seq": current_seq,
                    "卡名": in_flight.get("卡名") if in_flight else None,
                    "沿用版本": old_version if preserved else None,
                },
                "等待条件": copy.deepcopy(in_flight.get("等待条件")) if in_flight else None,
                "回滚定义": copy.deepcopy(in_flight.get("回滚定义")) if in_flight else None,
                "定义": [
                    {
                        "seq": item.get("seq", index),
                        "卡名": item.get("步骤名") or item.get("卡名"),
                        "动作类型": item.get("动作类型"),
                        "执行动作": item.get("执行动作"),
                        "等待条件": copy.deepcopy(item.get("等待条件") or {}),
                        "回滚定义": copy.deepcopy(item.get("回滚定义") or {}),
                        "结果提取": item.get("结果提取"),
                    }
                    for index, item in enumerate(steps_definitions)
                ],
            }
            checkpoint.setdefault("revisions", []).append(revision)
            _log(
                checkpoint,
                operator,
                "编辑流程",
                f"流程升版到 v{new_version}；在途第 {current_seq + 1} 张卡继续按 v{old_version} 的等待条件与回滚定义执行",
            )
            return True, (
                f"已形成 v{new_version}：后续卡片按新版本执行；在途第 {current_seq + 1} 张卡仍按 v{old_version} 的等待条件与回滚定义"
                if preserved
                else f"已形成 v{new_version}，未发现需要沿用的在途卡"
            ), {"checkpoint": self._summarize(checkpoint), "revision": copy.deepcopy(revision)}

    def _build_step(
        self,
        index: int,
        definition: dict[str, Any],
        version: int,
        existing: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        wait = copy.deepcopy(definition.get("等待条件") or {})
        wait.setdefault("mode", "元素出现")
        wait.setdefault("expr", "回执状态")
        wait.setdefault("timeout", 15)
        return {
            "seq": index,
            "卡名": str(definition.get("步骤名") or definition.get("卡名") or f"步骤 {index + 1}"),
            "动作类型": str(definition.get("动作类型") or "观察"),
            "执行动作": str(definition.get("执行动作") or "observe"),
            "页面参数": copy.deepcopy(definition.get("页面参数") or {}),
            "等待条件": wait,
            "结果提取": str(definition.get("结果提取") or "回执状态"),
            "回滚定义": copy.deepcopy(definition.get("回滚定义") or {}),
            "凭证编号": str(definition.get("凭证编号") or (existing or {}).get("凭证编号") or "") or None,
            "定义版本": version,
            "沿用版本": None,
            "step_status": "未开始",
            "执行次数": 0,
            "最近执行": None,
            "证据": None,
            "核对": None,
            "结果": None,
            "回滚": {"状态": None, "次数": 0, "最近执行": None, "证据": None, "核对": None},
        }
