"""The two SQLModel typing gaps that have to be named out loud.

SQLModel 0.0.42's ``SQLModel`` inherits from ``pydantic.BaseModel``, not from
``sqlalchemy.orm.DeclarativeBase``. Its metaclass does the work a declarative
base class normally does, which is why SQLAlchemy's mypy plugin — whose
declarative detection keys on that base — never fires for a SQLModel class.
mypy therefore sees the *declared* Python type of a field (``str``,
``datetime``) where the runtime has an ``InstrumentedAttribute``.

Query construction does not need anything from this module: ``sqlmodel.col()``
is the library's own answer, returns the same object unchanged, and gives mypy
the ``Mapped[...]`` view. These two cannot be reached that way — ``__table__``
and ``Result.rowcount`` are produced by the metaclass and by the DBAPI, and
there is no declaration to point ``col()`` at.

Both helpers are casts with the reason attached, so the reason is written once
rather than at each of the sixteen call sites, and a future SQLModel release
that fixes the typing can delete one function instead of re-auditing a dozen
scattered suppressions.
"""

from __future__ import annotations

from typing import Any, cast

from sqlalchemy import CursorResult, Result, Table
from sqlmodel import SQLModel


def table_of(model: type[SQLModel]) -> Table:
    """The ``Table`` SQLModel's metaclass built for a ``table=True`` model.

    Every model declared with ``table=True`` has one; it is simply not on the
    class statically. A model declared *without* ``table=True`` does not have
    one, and ``AttributeError`` is the right outcome there — that is a
    programming mistake, not a malformed request.
    """
    return cast("Table", model.__table__)  # type: ignore[attr-defined]


def rows_changed(result: Result[Any]) -> int:
    """Rows affected by an INSERT, UPDATE, or DELETE.

    ``Result`` does not declare ``rowcount``; ``CursorResult`` does, and that is
    what ``session.execute`` returns for DML. The cast holds only because the
    caller passed a statement that changes rows — a ``SELECT`` routed through
    here reports ``-1`` rather than failing, which is why this is not a general
    "how many rows" helper.
    """
    return cast("CursorResult[Any]", result).rowcount
