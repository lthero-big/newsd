"""M3 favorites / trash. Marks are per user; only items that appear in one of the user's own issues can be marked."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import delete, select

from .auth import get_db, require_user
from .models import Issue, IssueItem, ItemMark, PoolItem

router = APIRouter(prefix="/api")
STATES = ("favorite", "trash", "purged")
VISIBLE = ("favorite", "trash")


def user_marks(db, user_id: int, state: str | None = None) -> dict[int, str]:
    q = select(ItemMark.item_id, ItemMark.state).where(ItemMark.user_id == user_id)
    if state:
        q = q.where(ItemMark.state == state)
    return {i: s for i, s in db.execute(q)}


def in_users_issues(db, user_id: int, item_id: int) -> bool:
    return db.scalar(select(IssueItem.id).join(Issue, Issue.id == IssueItem.issue_id)
                     .where(Issue.user_id == user_id, IssueItem.item_id == item_id).limit(1)) is not None


def set_mark(db, user_id: int, item_id: int, state: str | None) -> str | None:
    """state None clears the mark. Raises LookupError when the item is not in the user's issues."""
    if state is not None and state not in VISIBLE:
        raise ValueError(state)
    row = db.scalar(select(ItemMark).where(ItemMark.user_id == user_id, ItemMark.item_id == item_id))
    if state is None:
        if row is not None:
            db.delete(row); db.commit()
        return None
    if not in_users_issues(db, user_id, item_id) and row is None:
        raise LookupError(item_id)
    if row is None:
        row = ItemMark(user_id=user_id, item_id=item_id, state=state); db.add(row)
    elif row.state != state:  # trashed / favourited "now": restarts the 30-day trash clock and the favourite date
        from .models import now
        row.created_at = now()
    row.state = state
    db.commit()
    return state


class MarkIn(BaseModel):
    state: str | None = None  # favorite | trash | null (clear)


@router.put("/me/items/{item_id}/mark")
def put_mark(item_id: int, body: MarkIn, user=Depends(require_user), db=Depends(get_db)):
    if body.state is not None and body.state not in VISIBLE:
        raise HTTPException(400, f"state must be one of {VISIBLE} or null")
    try:
        return {"item_id": item_id, "state": set_mark(db, user.id, item_id, body.state)}
    except LookupError:
        raise HTTPException(404, "no such item")


@router.get("/me/marks")
def my_marks(state: str, user=Depends(require_user), db=Depends(get_db)):
    if state not in VISIBLE:
        raise HTTPException(400, f"state must be one of {VISIBLE}")
    return marked_items(db, user.id, state)


def marked_items(db, user_id: int, state: str) -> list[dict]:
    """Marked items, newest mark first, with the translation from the user's latest issue that carried the item."""
    rows = db.execute(select(ItemMark, PoolItem).join(PoolItem, PoolItem.id == ItemMark.item_id)
                      .where(ItemMark.user_id == user_id, ItemMark.state == state)
                      .order_by(ItemMark.created_at.desc(), ItemMark.id.desc())).all()
    ids = [p.id for _, p in rows]
    tr: dict[int, IssueItem] = {}
    if ids:
        for ii, _key in db.execute(select(IssueItem, Issue.key).join(Issue, Issue.id == IssueItem.issue_id)
                                   .where(Issue.user_id == user_id, IssueItem.item_id.in_(ids)).order_by(Issue.key)):
            tr[ii.item_id] = ii  # later keys overwrite earlier ones
    return [{"item_id": p.id, "title": p.title, "title_translated": getattr(tr.get(p.id), "title_translated", None),
             "url": p.url, "summary": p.summary, "summary_translated": getattr(tr.get(p.id), "summary_translated", None),
             "marked_at": m.created_at.isoformat(), "state": m.state} for m, p in rows]


def clear_all(db, user_id: int, state: str, *, forget: bool = False) -> int:
    """Empty the trash (keeps learning signal as ``purged`` unless ``forget``) or clear all favorites."""
    if state == "trash" and not forget:
        from sqlalchemy import update
        n = db.execute(update(ItemMark).where(ItemMark.user_id == user_id, ItemMark.state == "trash")
                       .values(state="purged")).rowcount
    else:
        n = db.execute(delete(ItemMark).where(ItemMark.user_id == user_id, ItemMark.state == state)).rowcount
    db.commit()
    return n


def trash_count(db, user_id: int) -> int:
    from sqlalchemy import func
    from .feedback import auto_purge
    auto_purge(db, user_id)
    return db.scalar(select(func.count()).select_from(ItemMark)
                     .where(ItemMark.user_id == user_id, ItemMark.state == "trash")) or 0


@router.delete("/me/marks")
def clear_marks(state: str, forget: bool = False, user=Depends(require_user), db=Depends(get_db)):
    """Empty the trash (or clear all favorites). Trash keeps the learning signal unless ``forget=1``."""
    if state not in VISIBLE:
        raise HTTPException(400, f"state must be one of {VISIBLE}")
    return {"cleared": clear_all(db, user.id, state, forget=forget)}


@router.get("/me/marks/count")
def mark_counts(user=Depends(require_user), db=Depends(get_db)):
    return {"trash": trash_count(db, user.id), "favorite": len(user_marks(db, user.id, "favorite"))}
