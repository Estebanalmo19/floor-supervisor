"""HTTP client for the Card Resolver API.

Contract (verified for a known card):
    POST <url>  {"raw": "<card value>"}   Authorization: Bearer <token>
    200 {"raw": ..., "decoder": {...}, "employee": {"hibob_id": "99999", "name": "..."}, "dataset_id": 99}

Unknown-card semantics are NOT yet verified (the probe was rejected with 401). The
mapping below is deliberately defensive:
    200 with employee missing/null, 404, 400, 422  -> CardNotResolvedError
    408, 429, 5xx, timeout, connection error        -> CardResolverUnavailableError
    401, 403, other statuses, malformed body        -> CardResolverResponseError

The bearer token is only ever placed in the Authorization header; it never
appears in exceptions or logs.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from src.exceptions import (
    CardNotResolvedError,
    CardResolverResponseError,
    CardResolverUnavailableError,
)
from src.models.scan import CardResolution

logger = logging.getLogger(__name__)

_NOT_RESOLVED_STATUSES = frozenset({400, 404, 422})
_UNAVAILABLE_STATUSES = frozenset({408, 429})


class CardResolverClient:
    def __init__(
        self,
        url: str,
        bearer_token: str,
        timeout_seconds: float,
        http_client: httpx.Client | None = None,
    ) -> None:
        self._url = url
        self._bearer_token = bearer_token
        self._timeout = httpx.Timeout(timeout_seconds)
        self._http = http_client or httpx.Client(timeout=self._timeout)

    def __repr__(self) -> str:
        return f"CardResolverClient(url={self._url!r})"

    def close(self) -> None:
        self._http.close()

    def resolve(self, raw: str) -> CardResolution:
        try:
            response = self._http.post(
                self._url,
                json={"raw": raw},
                headers={
                    "Authorization": f"Bearer {self._bearer_token}",
                    "Accept": "application/json",
                },
                timeout=self._timeout,
            )
        except httpx.TimeoutException as exc:
            raise CardResolverUnavailableError("Card Resolver timed out") from exc
        except httpx.TransportError as exc:
            raise CardResolverUnavailableError(
                f"Card Resolver connection failed: {type(exc).__name__}"
            ) from exc

        status = response.status_code
        if status in _NOT_RESOLVED_STATUSES:
            raise CardNotResolvedError(f"Card Resolver could not resolve card (HTTP {status})")
        if status in _UNAVAILABLE_STATUSES or status >= 500:
            raise CardResolverUnavailableError(f"Card Resolver unavailable (HTTP {status})")
        if status != 200:
            raise CardResolverResponseError(f"Unexpected Card Resolver status HTTP {status}")

        try:
            body = response.json()
        except ValueError as exc:
            raise CardResolverResponseError("Card Resolver returned non-JSON body") from exc

        return _parse_resolution(body, sent_raw=raw)


def _parse_resolution(body: Any, sent_raw: str) -> CardResolution:
    if not isinstance(body, dict):
        raise CardResolverResponseError("Card Resolver body is not a JSON object")

    echoed_raw = body.get("raw")
    if echoed_raw is not None and echoed_raw != sent_raw:
        raise CardResolverResponseError("Card Resolver echoed a different raw value")

    employee = body.get("employee")
    if employee is None:
        raise CardNotResolvedError("Card Resolver returned no employee")
    if not isinstance(employee, dict):
        raise CardResolverResponseError("Card Resolver 'employee' is not an object")

    hibob_id = employee.get("hibob_id")
    if not isinstance(hibob_id, str) or not hibob_id.strip() or hibob_id != hibob_id.strip():
        raise CardResolverResponseError("Card Resolver 'employee.hibob_id' is not a valid string")
    if len(hibob_id) > 64:
        raise CardResolverResponseError("Card Resolver 'employee.hibob_id' is too long")

    name = employee.get("name")
    if not isinstance(name, str) or not name.strip():
        raise CardResolverResponseError("Card Resolver 'employee.name' is missing")

    dataset_id = body.get("dataset_id")
    if isinstance(dataset_id, bool) or not isinstance(dataset_id, int):
        raise CardResolverResponseError("Card Resolver 'dataset_id' is not an integer")

    return CardResolution(hibob_id=hibob_id, employee_name=name.strip(), dataset_id=dataset_id)
