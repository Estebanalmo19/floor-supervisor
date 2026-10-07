import json
import logging

import httpx
import pytest

from src.exceptions import (
    CardNotResolvedError,
    CardResolverResponseError,
    CardResolverUnavailableError,
)
from src.services.card_resolver import CardResolverClient

URL = "https://resolver.test/card-resolver/api/v1/cards/resolve"
TOKEN = "s3cr3t-token-value"
RAW = "000012345678"

OK_BODY = {
    "raw": RAW,
    "decoder": {"format": "h10304-reversed", "bits": 37, "facility_code": 123, "card_number": 4567},
    "employee": {"hibob_id": "99999", "name": "Test Employee"},
    "dataset_id": 99,
}


def _client(handler) -> CardResolverClient:
    http = httpx.Client(transport=httpx.MockTransport(handler))
    return CardResolverClient(URL, TOKEN, timeout_seconds=5, http_client=http)


def _respond(status=200, body=None, content=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if content is not None:
            return httpx.Response(status, content=content)
        return httpx.Response(status, json=body)

    return handler


def test_successful_resolution():
    resolution = _client(_respond(200, OK_BODY)).resolve(RAW)
    assert resolution.hibob_id == "99999"
    assert resolution.employee_name == "Test Employee"
    assert resolution.dataset_id == 99


def test_request_shape_and_bearer_auth():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("Authorization")
        seen["body"] = json.loads(request.content)
        seen["timeout"] = request.extensions.get("timeout")
        return httpx.Response(200, json=OK_BODY)

    _client(handler).resolve(RAW)
    assert seen["method"] == "POST"
    assert seen["url"] == URL
    assert seen["auth"] == f"Bearer {TOKEN}"
    assert seen["body"] == {"raw": RAW}
    assert seen["timeout"]["read"] == 5


def test_timeout_is_unavailable():
    def handler(request):
        raise httpx.ReadTimeout("timed out", request=request)

    with pytest.raises(CardResolverUnavailableError):
        _client(handler).resolve(RAW)


def test_connection_error_is_unavailable():
    def handler(request):
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(CardResolverUnavailableError):
        _client(handler).resolve(RAW)


@pytest.mark.parametrize("status", [408, 429, 500, 502, 503, 504])
def test_transient_statuses_are_unavailable(status):
    with pytest.raises(CardResolverUnavailableError):
        _client(_respond(status, {"detail": "x"})).resolve(RAW)


@pytest.mark.parametrize("status", [400, 404, 422])
def test_not_resolved_statuses(status):
    with pytest.raises(CardNotResolvedError):
        _client(_respond(status, {"detail": "not found"})).resolve(RAW)


@pytest.mark.parametrize("status", [401, 403, 302, 418])
def test_auth_and_unexpected_statuses_are_response_errors(status):
    with pytest.raises(CardResolverResponseError):
        _client(_respond(status, {"detail": "unauthorized"})).resolve(RAW)


@pytest.mark.parametrize("employee", [None, "missing"])
def test_200_without_employee_is_not_resolved(employee):
    body = dict(OK_BODY)
    if employee == "missing":
        del body["employee"]
    else:
        body["employee"] = None
    with pytest.raises(CardNotResolvedError):
        _client(_respond(200, body)).resolve(RAW)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda b: b.update(employee={"hibob_id": 99999, "name": "X"}),
        lambda b: b.update(employee={"hibob_id": "", "name": "X"}),
        lambda b: b.update(employee={"hibob_id": " 99999", "name": "X"}),
        lambda b: b.update(employee={"hibob_id": "5" * 65, "name": "X"}),
        lambda b: b.update(employee={"hibob_id": "99999"}),
        lambda b: b.update(employee={"hibob_id": "99999", "name": "  "}),
        lambda b: b.update(employee="99999"),
        lambda b: b.update(dataset_id="6"),
        lambda b: b.update(dataset_id=True),
        lambda b: b.pop("dataset_id"),
        lambda b: b.update(raw="999999999999"),
    ],
    ids=["int-id", "empty-id", "padded-id", "long-id", "no-name", "blank-name",
         "employee-not-object", "dataset-str", "dataset-bool", "dataset-missing", "raw-mismatch"],
)
def test_malformed_bodies_are_response_errors(mutate):
    body = json.loads(json.dumps(OK_BODY))
    mutate(body)
    with pytest.raises(CardResolverResponseError):
        _client(_respond(200, body)).resolve(RAW)


def test_non_json_body_is_response_error():
    with pytest.raises(CardResolverResponseError):
        _client(_respond(200, content=b"<html>gateway</html>")).resolve(RAW)


def test_json_array_body_is_response_error():
    with pytest.raises(CardResolverResponseError):
        _client(_respond(200, [OK_BODY])).resolve(RAW)


def test_token_never_appears_in_errors_logs_or_repr(caplog):
    caplog.set_level(logging.DEBUG)
    client = _client(_respond(401, {"detail": "unauthorized"}))
    with pytest.raises(CardResolverResponseError) as exc_info:
        client.resolve(RAW)

    assert TOKEN not in str(exc_info.value)
    assert TOKEN not in repr(client)
    assert TOKEN not in caplog.text
    assert RAW not in str(exc_info.value)
