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


# --- update_card (/cards/update, HTTP Basic) ----------------------------------------------

import base64  # noqa: E402

from src.exceptions import CardRegistrationRejectedError  # noqa: E402

UPDATE_URL = "https://resolver.test/card-resolver/api/v1/cards/update"
UPDATE_USER, UPDATE_PASSWORD = "kiosk", "upd4te-pa55word"
UPDATE_OK = {"status": "unchanged", "card_number_raw": RAW,
             "decoder": {"format": "h10304-reversed", "bits": 37, "facility_code": 123, "card_number": 4567},
             "hibob_id": "99999"}


def _update_client(handler, credentials=(UPDATE_USER, UPDATE_PASSWORD)) -> CardResolverClient:
    http = httpx.Client(transport=httpx.MockTransport(handler))
    return CardResolverClient(URL, TOKEN, timeout_seconds=5, http_client=http,
                              update_url=UPDATE_URL, update_credentials=credentials)


def test_update_request_shape_uses_basic_auth_not_bearer():
    seen = {}

    def handler(request):
        seen.update(method=request.method, url=str(request.url), auth=request.headers.get("Authorization"),
                    body=json.loads(request.content))
        return httpx.Response(200, json=UPDATE_OK)

    registration = _update_client(handler).update_card(RAW, "99999")
    assert registration.status == "unchanged" and registration.hibob_id == "99999"
    assert seen["method"] == "POST" and seen["url"] == UPDATE_URL
    expected = base64.b64encode(f"{UPDATE_USER}:{UPDATE_PASSWORD}".encode()).decode()
    assert seen["auth"] == f"Basic {expected}" and TOKEN not in seen["auth"]
    # overwrite=false: the kiosk can never take a card that belongs to someone else.
    assert seen["body"] == {"card_number_raw": RAW, "hibob_id": "99999", "overwrite": False}


@pytest.mark.parametrize("status_value", ["unchanged", "created", "updated"])
def test_update_status_is_informational(status_value):
    body = {**UPDATE_OK, "status": status_value}
    assert _update_client(_respond(200, body)).update_card(RAW, "99999").status == status_value


def test_update_without_credentials_is_not_attempted():
    calls = []
    client = _update_client(lambda r: calls.append(r) or httpx.Response(200, json=UPDATE_OK), credentials=None)
    assert client.registration_enabled is False
    with pytest.raises(CardResolverResponseError):
        client.update_card(RAW, "99999")
    assert calls == []


@pytest.mark.parametrize(("status", "error"), [
    (401, CardResolverResponseError), (403, CardResolverResponseError),
    (400, CardRegistrationRejectedError), (404, CardRegistrationRejectedError),
    (409, CardRegistrationRejectedError), (422, CardRegistrationRejectedError),
    (408, CardResolverUnavailableError), (429, CardResolverUnavailableError),
    (500, CardResolverUnavailableError), (503, CardResolverUnavailableError),
    (302, CardResolverResponseError),
])
def test_update_error_statuses(status, error):
    with pytest.raises(error):
        _update_client(_respond(status, {"detail": "x"})).update_card(RAW, "99999")


@pytest.mark.parametrize("exc", [httpx.ReadTimeout, httpx.ConnectError])
def test_update_transport_failures_are_unavailable(exc):
    def handler(request):
        raise exc("boom", request=request)

    with pytest.raises(CardResolverUnavailableError):
        _update_client(handler).update_card(RAW, "99999")


@pytest.mark.parametrize("mutate", [
    lambda b: b.update(hibob_id="11111"),
    lambda b: b.pop("hibob_id"),
    lambda b: b.update(hibob_id=99999),
    lambda b: b.update(card_number_raw="999999999999"),
    lambda b: b.pop("status"),
    lambda b: b.update(status=""),
    lambda b: b.update(status=7),
], ids=["id-mismatch", "id-missing", "id-int", "raw-mismatch", "no-status", "blank-status", "int-status"])
def test_update_malformed_or_mismatching_bodies_are_rejected(mutate):
    body = json.loads(json.dumps(UPDATE_OK))
    mutate(body)
    with pytest.raises(CardResolverResponseError):
        _update_client(_respond(200, body)).update_card(RAW, "99999")


@pytest.mark.parametrize("content", [b"<html>gateway</html>", b"[]"])
def test_update_non_object_bodies_are_rejected(content):
    with pytest.raises(CardResolverResponseError):
        _update_client(_respond(200, content=content)).update_card(RAW, "99999")


@pytest.mark.parametrize("status", [401, 409, 500, 200])
def test_update_errors_never_leak_card_or_credentials(caplog, status):
    caplog.set_level(logging.DEBUG)
    body = {**UPDATE_OK, "hibob_id": "11111"} if status == 200 else {"detail": f"card {RAW} taken"}
    client = _update_client(_respond(status, body))
    with pytest.raises(Exception) as exc_info:
        client.update_card(RAW, "99999")
    for secret in (RAW, UPDATE_PASSWORD, TOKEN):
        assert secret not in str(exc_info.value) and secret not in caplog.text
    assert UPDATE_PASSWORD not in repr(client)



# --- manual mappings without a dataset name (Card Resolver source=manual_mapping) ---------


def test_manual_mapping_with_null_name_resolves_without_name():
    body = {**OK_BODY, "employee": {"hibob_id": "43052", "name": None}, "source": "manual_mapping"}
    resolution = _client(_respond(200, body)).resolve(RAW)
    assert resolution.hibob_id == "43052" and resolution.employee_name is None
    assert resolution.dataset_id == 99


@pytest.mark.parametrize("body_change", [
    {"employee": {"hibob_id": "43052", "name": None}},                                # no source
    {"employee": {"hibob_id": "43052", "name": None}, "source": "dataset"},           # other source
    {"employee": {"hibob_id": "43052", "name": "  "}, "source": "manual_mapping"},    # blank name
    {"employee": {"hibob_id": "43052", "name": None}, "source": "manual_mapping", "dataset_id": None},
])
def test_null_name_is_only_accepted_for_manual_mappings_with_a_dataset(body_change):
    with pytest.raises(CardResolverResponseError):
        _client(_respond(200, {**OK_BODY, **body_change})).resolve(RAW)
