"""HTTP client for the Card Resolver API.

resolve() contract (verified for a known card):
    POST <url>  {"raw": "<card value>"}   Authorization: Bearer <token>
    200 {"raw": ..., "decoder": {...}, "employee": {"hibob_id": "99999", "name": "..."}, "dataset_id": 99}

Unknown-card semantics are NOT yet verified (the probe was rejected with 401). The
mapping below is deliberately defensive:
    200 with employee missing/null, 404, 400, 422  -> CardNotResolvedError
    408, 429, 5xx, timeout, connection error        -> CardResolverUnavailableError
    401, 403, other statuses, malformed body        -> CardResolverResponseError

update_card() contract (from the service's OpenAPI document, 2026-10-14):
    POST <.../cards/update>  {"card_number_raw": "<raw>", "hibob_id": "<id>", "overwrite": false}
    Authorization: HTTP Basic (realm "Card imports"); the resolve bearer token gets 401.
    card_number_raw: 1..20 chars. overwrite=false never takes a card from another owner.
    Documented: 200 (body schema unspecified) and 422. Observed body:
        {"status": "unchanged", "card_number_raw": ..., "decoder": {...}, "hibob_id": "..."}
    "status" values are not enumerated by the API; it is treated as informational.
    Success = 200/201 + JSON object whose hibob_id (and card_number_raw, if echoed)
    equal what was sent. Anything else is a failure:
        408, 429, 5xx, timeout, connection error  -> CardResolverUnavailableError
        401, 403, malformed body, mismatching ids -> CardResolverResponseError
        other 4xx (e.g. conflict, 422)            -> CardRegistrationRejectedError

Credentials are only ever placed in the Authorization header; neither they nor the
raw card value appear in exceptions or logs.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from src.exceptions import (
    CardNotResolvedError,
    CardRegistrationRejectedError,
    CardResolverResponseError,
    CardResolverUnavailableError,
)
from src.models.scan import CardRegistration, CardResolution

logger = logging.getLogger(__name__)

_NOT_RESOLVED_STATUSES = frozenset({400, 404, 422})
_UNAVAILABLE_STATUSES = frozenset({408, 429})
_UPDATE_SUCCESS_STATUSES = frozenset({200, 201})
# CardMappingRequest.card_number_raw maxLength in the Card Resolver OpenAPI schema.
CARD_NUMBER_RAW_MAX_LENGTH = 20


class CardResolverClient:
    def __init__(
        self,
        url: str,
        bearer_token: str,
        timeout_seconds: float,
        http_client: httpx.Client | None = None,
        update_url: str | None = None,
        update_credentials: tuple[str, str] | None = None,
    ) -> None:
        self._url = url
        self._bearer_token = bearer_token
        self._update_url = update_url
        self._update_credentials = update_credentials
        self._timeout = httpx.Timeout(timeout_seconds)
        self._http = http_client or httpx.Client(timeout=self._timeout)

    def __repr__(self) -> str:
        return f"CardResolverClient(url={self._url!r})"

    def close(self) -> None:
        self._http.close()

    @property
    def registration_enabled(self) -> bool:
        return bool(self._update_url and self._update_credentials)

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

    def update_card(self, card_number_raw: str, hibob_id: str) -> CardRegistration:
        """Link an unregistered card to a HiBob ID. Never overwrites another owner."""
        if not self.registration_enabled:
            raise CardResolverResponseError("Card registration is not configured")
        assert self._update_url is not None and self._update_credentials is not None
        try:
            response = self._http.post(
                self._update_url,
                json={"card_number_raw": card_number_raw, "hibob_id": hibob_id,
                      "overwrite": False},
                auth=httpx.BasicAuth(*self._update_credentials),
                headers={"Accept": "application/json"},
                timeout=self._timeout,
            )
        except httpx.TimeoutException as exc:
            raise CardResolverUnavailableError("Card Resolver update timed out") from exc
        except httpx.TransportError as exc:
            raise CardResolverUnavailableError(
                f"Card Resolver update connection failed: {type(exc).__name__}"
            ) from exc

        status = response.status_code
        if status in _UNAVAILABLE_STATUSES or status >= 500:
            raise CardResolverUnavailableError(f"Card Resolver update unavailable (HTTP {status})")
        if status in (401, 403):
            raise CardResolverResponseError(f"Card Resolver update rejected credentials (HTTP {status})")
        if 400 <= status < 500:
            raise CardRegistrationRejectedError(f"Card Resolver refused the card mapping (HTTP {status})")
        if status not in _UPDATE_SUCCESS_STATUSES:
            raise CardResolverResponseError(f"Unexpected Card Resolver update status HTTP {status}")

        try:
            body = response.json()
        except ValueError as exc:
            raise CardResolverResponseError("Card Resolver update returned non-JSON body") from exc

        return _parse_registration(body, sent_raw=card_number_raw, sent_hibob_id=hibob_id)


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


def _parse_registration(body: Any, sent_raw: str, sent_hibob_id: str) -> CardRegistration:
    if not isinstance(body, dict):
        raise CardResolverResponseError("Card Resolver update body is not a JSON object")

    if body.get("hibob_id") != sent_hibob_id:
        raise CardResolverResponseError("Card Resolver update returned a different hibob_id")

    echoed_raw = body.get("card_number_raw")
    if echoed_raw is not None and echoed_raw != sent_raw:
        raise CardResolverResponseError("Card Resolver update echoed a different card value")

    status = body.get("status")
    if not isinstance(status, str) or not status.strip() or len(status) > 64:
        raise CardResolverResponseError("Card Resolver update 'status' is not a valid string")

    return CardRegistration(status=status.strip(), hibob_id=sent_hibob_id)
