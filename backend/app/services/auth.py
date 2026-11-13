"""登录凭证业务规则：状态流转、字段校验与筛选口径都收在这里。

与检查点续做台的约定：
- 凭证在认证卡被消费：消费后状态变为已失效，并记下消费的检查点/批次/请求；
- 过期凭证阻止领取下一张认证卡，必须先重新绑定并启用；
- 重新绑定只恢复凭证可用性，不解除检查点里未决卡的副作用核对要求。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from app.store import store

MODULE = "auth"
REQUIRED_FIELDS = ["凭证编号", "目标站点", "认证方式"]
STATUS_ORDER = ["草稿", "待绑定", "已生效", "已失效"]
ACTION_RULES = {"提交绑定": "待绑定", "启用凭证": "已生效", "标记失效": "已失效"}
NEGATIVE_ACTIONS = ["标记失效"]


def _today() -> date:
    return date.today()


def _parse_date(text: Any) -> date | None:
    try:
        return date.fromisoformat(str(text or "").strip())
    except ValueError:
        return None


class AuthService:
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
            rows = [row for row in rows if keyword in str(row.get("凭证编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None
        return self._with_expiry(entry)

    def _with_expiry(self, entry: dict[str, Any]) -> dict[str, Any]:
        expires = _parse_date(entry.get("有效期至"))
        detail = dict(entry)
        if expires is not None and entry.get("status") == "已生效" and expires < _today():
            detail["过期"] = True
            detail["过期说明"] = f"有效期至 {entry.get('有效期至')}，已过期 {( _today() - expires).days} 天"
        elif expires is not None:
            detail["过期"] = False
            detail["过期说明"] = f"有效期至 {entry.get('有效期至')}"
        else:
            detail["过期"] = False
            detail["过期说明"] = "未登记有效期"
        return detail

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
        entry["消费状态"] = "未消费"
        rows.append(entry)
        return entry, []

    def run_action(
        self, entry_id: int, action: str, values: dict[str, Any] | None = None
    ) -> tuple[dict[str, Any] | None, str]:
        values = values or {}
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"凭证配置 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于登录凭证可执行范围"
        if action == "标记失效" and entry.get("消费检查点") and not str(values.get("理由") or "").strip():
            return None, (
                f"凭证 {entry.get('凭证编号')} 已在检查点 {entry['消费检查点']} 的认证卡中被消费；"
                "再次标记失效需要填写理由，并保留消费记录"
            )
        target = ACTION_RULES[action]
        entry["status"] = target
        entry["pending"] = target != "已失效"
        entry["abnormal"] = action in NEGATIVE_ACTIONS

        if action == "提交绑定":
            entry["绑定日期"] = _today().isoformat()
            days = str(values.get("凭据有效天数") or entry.get("凭据有效天数") or "").strip()
            try:
                span = int(float(days))
            except ValueError:
                span = 0
            entry["凭据有效天数"] = days or entry.get("凭据有效天数") or "30"
            if span > 0:
                entry["有效期至"] = (_today() + timedelta(days=span)).isoformat()
            else:
                entry.setdefault("有效期至", None)
            entry["重新绑定时间"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            # 重新绑定不等于解除核对：检查点里的未决卡保持原结论与证据
            entry["核对豁免"] = None
            entry["消费检查点"] = entry.get("消费检查点")
            message = (
                f"凭证配置已{action}，有效期至 {entry.get('有效期至') or '未登记'}；"
                "重新绑定不会解除检查点中未决卡的副作用核对要求"
            )
        elif action == "启用凭证":
            if str(entry.get("status") or "") == "":
                return None, "凭证状态缺失，不能启用"
            expires = _parse_date(entry.get("有效期至"))
            if expires is not None and expires < _today():
                entry["pending"] = False
                entry["abnormal"] = True
                return None, (
                    f"凭证 {entry.get('凭证编号')} 已于 {entry.get('有效期至')} 过期，"
                    "请先重新绑定并更新有效期后再启用"
                )
            entry["消费状态"] = entry.get("消费状态") or "未消费"
            message = f"凭证配置已{action}"
        else:
            entry["失效理由"] = str(values.get("理由") or "手工标记失效")
            message = f"凭证配置已{action}"

        return entry, message
