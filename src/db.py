"""PostgreSQL connection pool (psycopg 3).

One pool serves both schemas of arrise_vm_db:
  * hibob_etl          read only (enforced by role privileges and READ ONLY transactions)
  * floor_supervisor   application data
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from typing import Callable

import psycopg
from psycopg_pool import ConnectionPool

from src.config import DatabaseSettings

ConnectionFactory = Callable[[], AbstractContextManager[psycopg.Connection]]

APPLICATION_NAME = "floor_supervisor"


def create_pool(settings: DatabaseSettings, max_size: int = 4) -> ConnectionPool:
    """Create the pool without blocking start-up on database availability.

    Connections are checked before being handed out, so a dropped SSH tunnel or
    server restart is recovered from transparently. Credentials are passed as
    connection kwargs, never embedded in a logged connection string.
    """
    return ConnectionPool(
        conninfo="",
        kwargs={
            "host": settings.host,
            "port": settings.port,
            "dbname": settings.name,
            "user": settings.user,
            "password": settings.password,
            "sslmode": settings.sslmode,
            "connect_timeout": settings.connect_timeout_seconds,
            "application_name": APPLICATION_NAME,
            "options": f"-c statement_timeout={settings.statement_timeout_ms}",
        },
        min_size=1,
        max_size=max_size,
        timeout=settings.connect_timeout_seconds,
        check=ConnectionPool.check_connection,
        name="floor_supervisor",
        open=True,
    )
