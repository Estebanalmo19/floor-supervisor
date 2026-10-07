"""In-memory stand-ins for psycopg connections (no PostgreSQL required)."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ExecutedStatement:
    sql: str
    params: Any


@dataclass
class FakeDatabase:
    """Scripted responses: ``responder(sql, params)`` returns the rows for a statement."""

    responder: Callable[[str, Any], list[Any]] = lambda sql, params: []
    fail_on: Callable[[str], BaseException | None] = lambda sql: None
    executed: list[ExecutedStatement] = field(default_factory=list)
    events: list[str] = field(default_factory=list)
    connections_opened: int = 0

    @contextmanager
    def connection(self) -> Iterator[FakeConnection]:
        self.connections_opened += 1
        conn = FakeConnection(self)
        try:
            yield conn
        except BaseException:
            self.events.append("connection_rollback")
            raise
        else:
            self.events.append("connection_commit")


class FakeConnection:
    def __init__(self, db: FakeDatabase) -> None:
        self._db = db

    def cursor(self, row_factory: Any = None) -> FakeCursor:
        return FakeCursor(self._db, dict_rows=row_factory is not None)

    @contextmanager
    def transaction(self) -> Iterator[None]:
        self._db.events.append("tx_begin")
        try:
            yield
        except BaseException:
            self._db.events.append("tx_rollback")
            raise
        else:
            self._db.events.append("tx_commit")


class FakeCursor:
    def __init__(self, db: FakeDatabase, dict_rows: bool) -> None:
        self._db = db
        self._rows: list[Any] = []
        self.dict_rows = dict_rows

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        self._db.executed.append(ExecutedStatement(sql, params))
        error = self._db.fail_on(sql)
        if error is not None:
            raise error
        self._rows = list(self._db.responder(sql, params))

    def fetchall(self) -> list[Any]:
        rows, self._rows = self._rows, []
        return rows

    def fetchone(self) -> Any:
        return self._rows.pop(0) if self._rows else None
