"""One scope string for every analytics function: a store code ("HS10001"),
a state ("state:FL") or None for everything. Every filter goes through
store_predicate so a state view is the same code path as a store view."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Store

STATE_PREFIX = "state:"


def is_state(scope: str | None) -> bool:
    return bool(scope) and scope.startswith(STATE_PREFIX)


def state_of(scope: str | None) -> str | None:
    return scope[len(STATE_PREFIX):].upper() if is_state(scope) else None


def store_predicate(scope: str | None):
    """SQLAlchemy condition on Store for the scope (call only when scope is set)."""
    if is_state(scope):
        return Store.state == state_of(scope)
    return Store.code == scope


def stores_in_scope(session: Session, scope: str | None) -> list[Store]:
    stmt = select(Store).order_by(Store.code)
    if scope:
        stmt = stmt.where(store_predicate(scope))
    return session.execute(stmt).scalars().all()


def scope_label(session: Session, scope: str | None) -> str | None:
    if scope is None:
        return None
    if is_state(scope):
        return f"All {state_of(scope)} stores"
    s = session.execute(select(Store).where(Store.code == scope)).scalar_one_or_none()
    return s.name if s else scope
