"""HTTP API for push: the user's channel config, test push, own delivery history, and the admin delivery log."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select

from ..auth import get_db, require_admin, require_user
from ..models import PushDelivery, User
from . import prefs, service

router = APIRouter(prefix="/api")


class ChannelIn(BaseModel):
    enabled: bool = False
    # email: address, telegram: chat id / @channel, webhook: the URL. For webhook the URL and for telegram the token
    # are secrets: omit (null) to keep what is stored, send "" to clear.
    target: str | None = Field(default=None, max_length=1024)
    token: str | None = Field(default=None, max_length=256)      # telegram bot token
    secret: str | None = Field(default=None, max_length=256)     # webhook signing secret (Feishu / DingTalk)
    type: str | None = None                                       # webhook: generic | feishu | dingtalk | slack | discord
    mode: str | None = None                                       # webhook: summary | summary_and_items
    template: str | None = Field(default=None, max_length=9000)
    item_template: str | None = Field(default=None, max_length=9000)


class PushIn(BaseModel):
    enabled: bool = False
    time: str = "08:00"
    timezone: str = "Asia/Shanghai"
    channels: dict[str, ChannelIn] = Field(default_factory=dict)


@router.get("/me/push")
def my_push(user=Depends(require_user), db=Depends(get_db)):
    return prefs.get_push(db, user.id)


@router.put("/me/push")
def put_push(body: PushIn, user=Depends(require_user), db=Depends(get_db)):
    try:
        return prefs.save_push(db, user.id, {"enabled": body.enabled, "time": body.time, "timezone": body.timezone,
                                             "channels": {k: v.model_dump() for k, v in body.channels.items()}})
    except prefs.PrefError as exc:
        raise HTTPException(400, str(exc))


@router.post("/me/push/test/{channel}")
def test_push(channel: str, user=Depends(require_user), db=Depends(get_db)):
    """Send a sample message through the saved config of this channel right now (one attempt, recorded)."""
    try:
        d = service.send_test(db, user, channel)
    except service.PushRequestError as exc:
        raise HTTPException(exc.status, str(exc))
    return {"ok": d.status == "ok", "error": d.error, "delivery": service.delivery_view(d)}


@router.get("/me/push/deliveries")
def my_deliveries(limit: int = 30, user=Depends(require_user), db=Depends(get_db)):
    rows = db.scalars(select(PushDelivery).where(PushDelivery.user_id == user.id).order_by(PushDelivery.id.desc()).limit(max(1, min(limit, 100))))
    return [service.delivery_view(d) for d in rows]


def recent_deliveries(db, limit: int = 50, status: str | None = None, channel: str | None = None) -> list[dict]:
    q = select(PushDelivery, User.username).join(User, User.id == PushDelivery.user_id)
    if status:
        q = q.where(PushDelivery.status == status)
    if channel:
        q = q.where(PushDelivery.channel == channel)
    return [service.delivery_view(d, name) for d, name in db.execute(q.order_by(PushDelivery.id.desc()).limit(max(1, min(limit, 200))))]


@router.get("/admin/push-deliveries")
def admin_deliveries(limit: int = 50, status: str | None = None, channel: str | None = None, _=Depends(require_admin), db=Depends(get_db)):
    return recent_deliveries(db, limit, status, channel)
