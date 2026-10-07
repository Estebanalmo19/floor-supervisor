"""Composition root: builds the long-lived application objects from Settings."""

from __future__ import annotations

from dataclasses import dataclass

from psycopg_pool import ConnectionPool

from src.config import Settings
from src.db import create_pool
from src.logging_setup import configure_logging
from src.repositories.employee_repository import EmployeeRepository
from src.repositories.scan_repository import ScanRepository
from src.services.card_resolver import CardResolverClient
from src.services.employee_service import EmployeeService
from src.services.scan_service import ScanService


@dataclass(frozen=True)
class AppContext:
    settings: Settings
    pool: ConnectionPool
    card_resolver: CardResolverClient
    scan_service: ScanService


def build_app_context(settings: Settings) -> AppContext:
    configure_logging(settings.log_level)
    pool = create_pool(settings.database)
    card_resolver = CardResolverClient(
        url=settings.card_resolver.url,
        bearer_token=settings.card_resolver.bearer_token,
        timeout_seconds=settings.card_resolver.timeout_seconds,
    )
    scan_service = ScanService(
        card_resolver=card_resolver,
        employee_service=EmployeeService(EmployeeRepository(pool.connection)),
        scan_repository=ScanRepository(pool.connection),
        device_id=settings.device_id,
        duplicate_window_seconds=settings.duplicate_scan_window_seconds,
    )
    return AppContext(
        settings=settings, pool=pool, card_resolver=card_resolver, scan_service=scan_service
    )
