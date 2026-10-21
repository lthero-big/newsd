"""DEPTAI-216 3c: group permissions.

Effective permissions of a user:
  role admin           -> everything
  no group             -> DEFAULT (read, own sources, models, push): exactly what every user could do before 3c
  in one or more groups-> union of the groups' perms (read is always included)
  role readonly        -> the above minus src / model (can read and set up push, cannot change sources or models)

``users`` and ``site`` (user / group management, site-wide sources and settings) are deliberately admin-role only:
granting them through a group would let a member edit the very groups that grant them (privilege escalation),
so they are shown in the group matrix but locked. ``logs`` can be granted: members then get a read-only
日志与任务 page (no rerun, no source actions).

Write requests to the member APIs are checked by ``guard``: GET / HEAD always pass (reading is ``read``).
"""
from __future__ import annotations

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select

from .auth import current_user, get_db, require_user
from .models import User, UserGroup, UserGroupMember

PERMS = (("read", "阅读日报"), ("src", "管理自己的订阅源"), ("model", "配置模型"), ("push", "推送设置"),
         ("logs", "查看日志"), ("users", "管理用户与分组"), ("site", "全站源 / 系统设置"))
KEYS = tuple(k for k, _ in PERMS)
ADMIN_ONLY = frozenset({"users", "site"})
GRANTABLE = tuple(k for k in KEYS if k not in ADMIN_ONLY)
DEFAULT = frozenset({"read", "src", "model", "push"})
READONLY_DROPS = frozenset({"src", "model"})
ROLE_NAMES = {"admin": "管理员", "user": "成员", "readonly": "只读"}


def clean(perms) -> list[str]:
    """Known, grantable keys in catalog order; read is always on."""
    s = set(perms or ()) | {"read"}
    return [k for k in GRANTABLE if k in s]


def user_groups(db, user_id: int) -> list[UserGroup]:
    return list(db.scalars(select(UserGroup).join(UserGroupMember, UserGroupMember.group_id == UserGroup.id)
                           .where(UserGroupMember.user_id == user_id).order_by(UserGroup.position, UserGroup.id)))


def effective(db, user: User, groups: list[UserGroup] | None = None) -> set[str]:
    if user.role == "admin":
        return set(KEYS)
    groups = user_groups(db, user.id) if groups is None else groups
    perms = set().union(*(set(clean(g.perms)) for g in groups)) if groups else set(DEFAULT)
    # read-only account, or every group it belongs to is a read-only group (the most generous group wins)
    if user.role == "readonly" or (groups and all(g.role == "readonly" for g in groups)):
        perms -= READONLY_DROPS
    return perms | {"read"}


def has(db, user: User, perm: str) -> bool:
    return perm in effective(db, user)


def guard(perm: str):
    """Router dependency: write requests of a logged-in user need ``perm``. Reads and anonymous requests pass
    through (the endpoint's own require_user still answers 401)."""
    def check(request: Request, user=Depends(current_user), db=Depends(get_db)):
        if request.method not in ("GET", "HEAD") and user is not None and not has(db, user, perm):
            raise HTTPException(403, f"permission required: {perm}")
    return check


def require(perm: str):
    """Endpoint dependency: the request needs ``perm`` whatever the method."""
    def check(user=Depends(require_user), db=Depends(get_db)):
        if not has(db, user, perm):
            raise HTTPException(403, f"permission required: {perm}")
        return user
    check.perm = perm  # lets tests tell a permission-gated /api/admin route from an unguarded one
    return check


def group_quota(db, user_id: int) -> int | None:
    """Daily item cap from the user's groups: the most generous one wins; any group without a cap means no cap."""
    groups = user_groups(db, user_id)
    if not groups or any(g.quota is None for g in groups):
        return None
    return max(int(g.quota) for g in groups)
