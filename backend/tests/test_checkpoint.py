"""检查点续做台的端到端规则测试：直接调 service，不依赖 HTTP 客户端。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import copy

from app.services import demo, rpaflow  # noqa: E402
from app.store import store  # noqa: E402
from app.seed import SEED_ROWS  # noqa: E402


def reset() -> None:
    store._tables = {name: copy.deepcopy(rows) for name, rows in SEED_ROWS.items()}
    store._counters.clear()
    demo.demo_service.reset()


def service() -> rpaflow.RpaflowService:
    return rpaflow.RpaflowService()


def card(detail, seq):
    return detail["cards"][seq]


def test_review_accepted_advances() -> None:
    reset()
    svc = service()
    ok, message, result = svc.review_step(1, 1, "页面已接受", "值班管理员", "快照里表单已是目标值")
    assert ok and result
    detail, _ = svc.get_checkpoint_detail(1)
    assert detail["cards"][1]["step_status"] == "已完成"
    assert detail["cards"][1]["结果"]["值"].startswith("陈晓")
    assert detail["checkpoint"]["当前卡序"] == 2


def test_consumed_credential_blocks_next_auth_card() -> None:
    reset()
    svc = service()
    ok, message, result = svc.execute_step(2, "值班管理员")
    assert not ok and "已过期" in message
    auth_entry = store.find("auth", 2)
    assert auth_entry["status"] == "已生效"


def test_review_rejected_allows_retry_only_once() -> None:
    reset()
    svc = service()
    ok, message, _ = svc.review_step(1, 1, "页面未接受", "值班管理员", "页面表单还是旧值")
    assert ok
    detail, _ = svc.get_checkpoint_detail(1)
    token = detail["lease"]["token"] if detail["lease"] else None
    ok, message, result = svc.execute_step(1, "值班管理员", token)
    assert ok
    assert result["card"]["step_status"] == "已完成"
    assert result["evidence"]["request_id"]


def test_unresolved_blocks_and_resume_does_not_replay() -> None:
    reset()
    svc = service()
    svc.review_step(4, 1, "无法判明", "值班管理员", "证据矛盾")
    detail, _ = svc.get_checkpoint_detail(4)
    assert detail["checkpoint"]["续做状态"] == "已阻断"
    ok, message, _ = svc.resume(4, "值班管理员", token=(detail.get("lease") or {}).get("token"))
    assert not ok and "未决卡" in message
    again, _ = svc.get_checkpoint_detail(4)
    assert again["cards"][1]["step_status"] == "已阻断"
    assert again["cards"][0]["step_status"] == "已完成"


def test_expired_credential_blocks_before_status() -> None:
    reset()
    svc = service()
    ok, message, _ = svc.execute_step(2, "值班管理员")
    assert not ok
    assert "已过期" in message


def test_execute_failure_enters_evidence_and_credential_consumed() -> None:
    reset()
    svc = service()
    svc.review_step(4, 1, "页面未接受", "值班管理员", "页面回执不属于本次请求")
    detail, _ = svc.get_checkpoint_detail(4)
    token = detail["lease"]["token"] if detail["lease"] else None
    ok, message, result = svc.execute_step(4, "值班管理员", token)
    assert not ok
    assert result["card"]["step_status"] == "待核对"
    assert result["evidence"]["已送达"] is True
    assert result["evidence"]["匹配回执"]


def test_expired_credential_blocks_auth_card() -> None:
    reset()
    svc = service()
    ok, message, result = svc.execute_step(2, "值班管理员")
    assert not ok
    assert "不能领取下一张认证卡" in message
    assert result["card"]["step_status"] == "未开始"


def test_lease_is_single_and_late_receipts_go_to_old_batch() -> None:
    reset()
    svc = service()
    ok2, message2, _ = svc.acquire_lease(3, "值班管理员")
    assert not ok2 and "只颁发一份执行权" in message2
    ok3, message3, result3 = svc.submit_late_receipt(
        3, "值班管理员", token="LEASE-CP0003-B1K3M9", batch="BATCH-CP0003-B1", request_id="REQ-CP0003-02-01-F42A"
    )
    assert ok3
    assert result3["receipt"]["state"] == "旧批次证据"


def test_inflight_card_keeps_old_version_after_edit() -> None:
    reset()
    svc = service()
    new_steps = [
        {"步骤名": "认证卡：新版登录", "动作类型": "认证", "执行动作": "login", "凭证编号": "AUTH-1004", "等待条件": {"mode": "回执编号生成", "expr": "回执编号", "timeout": 5}, "结果提取": "会话令牌", "页面参数": {"account": "wanglan@example.com"}},
        {"步骤名": "确认卡：新版提交", "动作类型": "确认", "执行动作": "confirm", "等待条件": {"mode": "固定等待", "expr": "", "timeout": 1}, "结果提取": "回执编号", "回滚定义": {"action": "cancel", "页面参数": {}}},
        {"步骤名": "观察卡：新版观察", "动作类型": "观察", "执行动作": "observe", "等待条件": {"mode": "元素出现", "expr": "回执状态", "timeout": 10}, "结果提取": "回执状态"},
    ]
    ok, message, result = svc.edit_flow(3, new_steps, "值班管理员", "调整等待条件")
    assert ok
    detail, _ = svc.get_checkpoint_detail(3)
    inflight = detail["cards"][1]
    assert inflight["沿用版本"] == 2
    assert inflight["等待条件"]["mode"] == "文本包含"
    assert inflight["回滚定义"]["页面参数"].get("模拟") == "无回执"
    nxt = detail["cards"][2]
    assert nxt["定义版本"] == 3
    assert nxt["等待条件"]["mode"] == "元素出现"


def test_rollback_needs_its_own_review_before_retry() -> None:
    reset()
    svc = service()
    token = (svc.get_checkpoint_detail(3)[0]["lease"] or {}).get("token")
    # 先把在途的确认卡判为未接受并重试，让重试再次得到“无回执”，留下异常执行记录
    ok, message, _ = svc.review_step(3, 1, "页面未接受", "值班管理员", "回执编号不属于本次请求", token)
    assert ok
    ok, message, result = svc.execute_step(3, "值班管理员", token)
    assert not ok and result["card"]["step_status"] == "待核对"
    ok, message, result = svc.rollback_step(3, 1, "值班管理员", token=token)
    assert not ok
    assert "需要先核对回滚是否已经改变演示页" in message
    detail, _ = svc.get_checkpoint_detail(3)
    assert detail["cards"][1]["回滚"]["状态"] == "待核对"
    ok, message, _ = svc.rollback_step(3, 1, "值班管理员", token=token)
    assert not ok and "先完成回滚证据核对" in message
    snapshot_before = svc.get_checkpoint_detail(3)[0]["page"]["快照"]["作废次数"]
    ok, message, result = svc.review_rollback(3, 1, "页面已接受", "值班管理员", "作废次数已增加", token)
    assert ok
    detail, _ = svc.get_checkpoint_detail(3)
    assert detail["cards"][1]["回滚"]["状态"] == "已完成"
    after = detail["page"]["快照"]["作废次数"]
    ok, message, _ = svc.rollback_step(3, 1, "值班管理员", token=token)
    assert not ok and "不能重复回滚" in message
    assert svc.get_checkpoint_detail(3)[0]["page"]["快照"]["作废次数"] == after
    detail, _ = svc.get_checkpoint_detail(3)
    assert detail["cards"][1]["step_status"] == "已回滚"


def test_rebind_does_not_clear_checkpoint_review() -> None:
    reset()
    svc = service()
    auth = __import__("app.services.auth", fromlist=["AuthService"]).AuthService()
    credential = [row for row in store.rows("auth") if row["凭证编号"] == "AUTH-1002"][0]
    entry = store.find("auth", credential["id"])
    ok, message = auth.run_action(entry["id"], "提交绑定", {"凭据有效天数": "30"})
    assert ok
    assert entry["status"] == "待绑定"
    ok, message = auth.run_action(entry["id"], "启用凭证", {})
    assert ok
    assert entry["status"] == "已生效"
    assert "核对豁免" in entry and entry["核对豁免"] is None
    detail, _ = svc.get_checkpoint_detail(1)
    assert detail["checkpoint"]["续做状态"] == "待核对"
    assert detail["cards"][1]["step_status"] == "待核对"
    ok, message, result = svc.execute_step(2, "值班管理员")
    assert ok
    assert result["card"]["step_status"] == "已完成"
    completed, _ = svc.get_checkpoint_detail(1)
    assert completed["cards"][1]["step_status"] == "待核对"
    assert completed["checkpoint"]["续做状态"] == "待核对"


def test_rebound_credential_can_consume_auth_card() -> None:
    reset()
    svc = service()
    auth = __import__("app.services.auth", fromlist=["AuthService"]).AuthService()
    entry = store.find("auth", [row for row in store.rows("auth") if row["凭证编号"] == "AUTH-1002"][0]["id"])
    auth.run_action(entry["id"], "提交绑定", {"凭据有效天数": "30"})
    auth.run_action(entry["id"], "启用凭证", {})
    ok, message, result = svc.execute_step(2, "值班管理员")
    assert ok, message
    assert result["card"]["step_status"] == "已完成"
    assert entry["status"] == "已失效"
    assert entry["消费检查点"] == "CP-0002"


def test_lease_expiry_allows_new_holder() -> None:
    reset()
    svc = service()
    lease = store.rows("rpa_lease")[0]
    lease["expires_at"] = "2000-01-01 00:00:00"
    ok, message, result = svc.acquire_lease(3, "值班管理员")
    assert ok
    assert result["lease"]["holder"] == "值班管理员"


def test_late_receipt_does_not_advance_or_create_checkpoint() -> None:
    reset()
    svc = service()
    before, _ = svc.get_checkpoint_detail(3)
    ok, message, result = svc.submit_late_receipt(
        3, "值班管理员", token="LEASE-CP0003-B1K3M9", batch="BATCH-CP0003-B1", request_id="REQ-LATE-0001"
    )
    assert ok
    after, _ = svc.get_checkpoint_detail(3)
    assert after["checkpoint"]["当前卡序"] == before["checkpoint"]["当前卡序"]
    assert after["cards"][1]["step_status"] == "待核对"
    assert len(store.rows("rpa_checkpoint")) == 4
    assert any(row["request_id"] == "REQ-LATE-0001" for row in after["stale_receipts"])


def test_edit_cannot_drop_inflight_card() -> None:
    reset()
    svc = service()
    ok, message, _ = svc.edit_flow(3, [{"步骤名": "只剩一张", "动作类型": "观察", "执行动作": "observe"}], "值班管理员")
    assert not ok
    assert "裁掉在途卡" in message
