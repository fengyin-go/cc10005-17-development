"""RPA检查点续做台接口：步骤卡、网页证据、执行权、版本与旧批次证据。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.demo import demo_service
from app.services.rpaflow import RpaflowService

router = APIRouter(prefix="/api/rpa-checkpoint", tags=["RPA检查点续做台"])

service = RpaflowService()


def _payload_value(payload: EntryPayload, key: str) -> str:
    return str(payload.values.get(key) or "").strip()


def _result(ok: bool, message: str, entry: dict[str, Any] | None = None) -> ActionResult:
    return ActionResult(ok=ok, message=message, entry=entry)


def _guard_error(checkpoint_id: int) -> None:
    if service.get_checkpoint_detail(checkpoint_id)[0] is None:
        raise HTTPException(status_code=404, detail=f"检查点 {checkpoint_id} 不存在")


@router.get("", response_model=dict)
def list_checkpoints(
    keyword: str | None = Query(default=None, description="按检查点编号或名称检索"),
    status: str | None = Query(default=None, description="待执行、执行中、待核对、已阻断、已完成、已回滚"),
) -> dict[str, Any]:
    """续做台总览：检查点列表、关键计数与当前有效执行权。"""
    items, total = service.list_checkpoints(keyword=keyword, status=status)
    desk = service.desk()
    return {"items": items, "total": total, **desk}


@router.get("/pages/{page_id}", response_model=dict)
def get_demo_page(page_id: str) -> dict[str, Any]:
    """读取本地演示页面的当前状态与审计末尾，供证据核对使用。"""
    page = demo_service.page_view(page_id)
    if page is None:
        raise HTTPException(status_code=404, detail=f"演示页面 {page_id} 不存在")
    return page


@router.post("/pages/{page_id}/actions", response_model=ActionResult)
def demo_page_action(page_id: str, payload: EntryPayload) -> ActionResult:
    """直接在本地演示页上执行填表、确认、登录等可观察动作；副作用会进入页面审计。"""
    action = _payload_value(payload, "action")
    request_id = _payload_value(payload, "request_id")
    result, message = demo_service.perform(
        page_id, action, request_id=request_id, values=dict(payload.values)
    )
    if result is None:
        return _result(False, message)
    return _result(True, message, result)


@router.get("/{checkpoint_id}", response_model=dict)
def get_checkpoint(checkpoint_id: int) -> dict[str, Any]:
    """读取检查点：步骤卡、当前卡、页面证据、执行权、版本与旧批次回执。"""
    detail, message = service.get_checkpoint_detail(checkpoint_id)
    if detail is None:
        raise HTTPException(status_code=404, detail=message)
    return detail


@router.get("/{checkpoint_id}/revisions", response_model=dict)
def list_revisions(checkpoint_id: int) -> dict[str, Any]:
    """读取流程编辑形成的版本记录，便于确认在途卡沿用哪一版定义。"""
    _guard_error(checkpoint_id)
    revisions, message = service.list_revisions(checkpoint_id)
    if revisions is None:
        raise HTTPException(status_code=404, detail=message)
    return {"items": revisions, "total": len(revisions)}


@router.get("/{checkpoint_id}/stale-receipts", response_model=dict)
def list_stale_receipts(checkpoint_id: int) -> dict[str, Any]:
    """读取旧批次证据区：失权执行者提交的迟到回执只留档，不推进步骤。"""
    _guard_error(checkpoint_id)
    items = service.list_stale_receipts(checkpoint_id)
    return {"items": items, "total": len(items)}


@router.post("/{checkpoint_id}/lease", response_model=ActionResult)
def lease_action(checkpoint_id: int, payload: EntryPayload) -> ActionResult:
    """领取或释放执行权；同一检查点同一时间只颁发一份执行权。"""
    action = _payload_value(payload, "action") or "领取"
    operator = _payload_value(payload, "operator") or "值班管理员"
    token = _payload_value(payload, "token") or None
    if action == "释放":
        ok, message, entry = service.release_lease(checkpoint_id, token or "", operator)
    else:
        ok, message, entry = service.acquire_lease(checkpoint_id, operator, token)
    return _result(ok, message, entry)


@router.post("/{checkpoint_id}/resume", response_model=ActionResult)
def resume_checkpoint(checkpoint_id: int, payload: EntryPayload) -> ActionResult:
    """中断后恢复：停在不重复重放已完成的步骤，遇到未决卡则停止并说明原因。"""
    operator = _payload_value(payload, "operator") or "值班管理员"
    token = _payload_value(payload, "token") or None
    ok, message, entry = service.resume(checkpoint_id, operator, token)
    return _result(ok, message, entry)


@router.post("/{checkpoint_id}/execute", response_model=ActionResult)
def execute_step(checkpoint_id: int, payload: EntryPayload) -> ActionResult:
    """执行当前步骤卡；失败时先进入证据核对区，不自动重试。"""
    operator = _payload_value(payload, "operator") or "值班管理员"
    token = _payload_value(payload, "token") or None
    ok, message, entry = service.execute_step(checkpoint_id, operator, token)
    return _result(ok, message, entry)


@router.post("/{checkpoint_id}/cards/{seq}/review", response_model=ActionResult)
def review_card(checkpoint_id: int, seq: int, payload: EntryPayload) -> ActionResult:
    """证据核对结论：页面已接受→补记结果并推进；未接受→才可重试；无法判明→停止并解释。"""
    decision = _payload_value(payload, "decision")
    operator = _payload_value(payload, "operator") or "值班管理员"
    note = _payload_value(payload, "note")
    token = _payload_value(payload, "token") or None
    ok, message, entry = service.review_step(checkpoint_id, seq, decision, operator, note, token)
    return _result(ok, message, entry)


@router.post("/{checkpoint_id}/cards/{seq}/rollback", response_model=ActionResult)
def rollback_card(checkpoint_id: int, seq: int, payload: EntryPayload) -> ActionResult:
    """按步骤卡定义执行异常回滚；回滚失败同样先进入回滚证据核对。"""
    operator = _payload_value(payload, "operator") or "值班管理员"
    note = _payload_value(payload, "note")
    token = _payload_value(payload, "token") or None
    ok, message, entry = service.rollback_step(checkpoint_id, seq, operator, note, token)
    return _result(ok, message, entry)


@router.post("/{checkpoint_id}/cards/{seq}/rollback-review", response_model=ActionResult)
def review_rollback(checkpoint_id: int, seq: int, payload: EntryPayload) -> ActionResult:
    """核对回滚自身的副作用：已接受不再重试回滚，未接受才允许重试，无法判明则停止。"""
    decision = _payload_value(payload, "decision")
    operator = _payload_value(payload, "operator") or "值班管理员"
    note = _payload_value(payload, "note")
    token = _payload_value(payload, "token") or None
    ok, message, entry = service.review_rollback(checkpoint_id, seq, decision, operator, note, token)
    return _result(ok, message, entry)


@router.post("/{checkpoint_id}/edit", response_model=ActionResult)
def edit_flow(checkpoint_id: int, payload: EntryPayload) -> ActionResult:
    """编辑流程形成新版本；在途卡继续依据旧版等待条件与回滚定义执行。"""
    definitions = payload.values.get("steps")
    if not isinstance(definitions, list):
        return _result(False, "编辑流程需要提交 steps 列表，每项至少包含卡名与执行动作")
    operator = _payload_value(payload, "operator") or "值班管理员"
    note = _payload_value(payload, "note")
    ok, message, entry = service.edit_flow(checkpoint_id, definitions, operator, note)
    return _result(ok, message, entry)


@router.post("/{checkpoint_id}/late-receipts", response_model=ActionResult)
def submit_late_receipt(checkpoint_id: int, payload: EntryPayload) -> ActionResult:
    """失权执行者的迟到回执进入旧批次证据区：只留档，不推进步骤、不生成新检查点。"""
    operator = _payload_value(payload, "operator") or "未署名执行者"
    token = _payload_value(payload, "token")
    request_id = _payload_value(payload, "request_id")
    batch = _payload_value(payload, "batch")
    note = _payload_value(payload, "note")
    if not request_id:
        return _result(False, "迟到回执需要提供 request_id，否则无法与步骤卡对齐")
    ok, message, entry = service.submit_late_receipt(
        checkpoint_id, operator, token, request_id, batch, note
    )
    return _result(ok, message, entry)
