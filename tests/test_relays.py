import asyncio
import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient


def signed_headers(body: bytes, *, event="push", delivery="delivery-123"):
    signature = (
        "sha256=" + hmac.new(b"test-webhook-secret", body, hashlib.sha256).hexdigest()
    )
    return {
        "Content-Type": "application/json",
        "X-GitHub-Event": event,
        "X-GitHub-Delivery": delivery,
        "X-Hub-Signature-256": signature,
    }


@pytest.mark.parametrize(
    ("environment", "fixture_name", "path", "target_key", "target_value"),
    [
        (
            "production",
            "production_merge.json",
            "/webhooks/deploy",
            "targets",
            ["core"],
        ),
        ("test", "test_merge.json", "/webhooks/deploy-test", "target", "test"),
    ],
)
def test_non_ignored_webhook_returns_delivery_id(
    load_environment,
    payload_fixture,
    environment,
    fixture_name,
    path,
    target_key,
    target_value,
):
    module = load_environment(environment)
    payload = payload_fixture(fixture_name)
    body = json.dumps(payload, separators=(",", ":")).encode()

    response = TestClient(module.app).post(
        path, content=body, headers=signed_headers(body)
    )

    assert response.status_code == 202
    assert response.json()["delivery_id"] == "delivery-123"
    assert response.json()[target_key] == target_value


@pytest.mark.parametrize(
    ("environment", "fixture_name", "path", "dispatch_name"),
    [
        (
            "production",
            "production_merge.json",
            "/webhooks/deploy",
            "dispatch_arcane_webhooks",
        ),
        ("test", "test_merge.json", "/webhooks/deploy-test", "dispatch_test_webhook"),
    ],
)
def test_ignored_path_does_not_schedule_dispatch(
    load_environment,
    payload_fixture,
    monkeypatch,
    environment,
    fixture_name,
    path,
    dispatch_name,
):
    module = load_environment(environment)
    payload = payload_fixture(fixture_name)
    payload["head_commit"]["added"] = []
    payload["head_commit"]["modified"] = ["README.md"]
    payload["head_commit"]["removed"] = []
    called = []

    async def fake_dispatch(*args):
        called.append(args)

    monkeypatch.setattr(module, dispatch_name, fake_dispatch)
    body = json.dumps(payload, separators=(",", ":")).encode()
    response = TestClient(module.app).post(
        path, content=body, headers=signed_headers(body)
    )

    assert response.status_code == 200
    assert response.json()["ignored"] is True
    assert called == []


class FakeArcaneResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


class FakeArcaneClient:
    response = FakeArcaneResponse(200, {"success": True, "data": None})

    def __init__(self, **kwargs):
        self.kwargs = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def post(self, url, json, headers):
        return self.response


@pytest.mark.parametrize(
    ("environment", "fixture_name"),
    [("production", "production_merge.json"), ("test", "test_merge.json")],
)
def test_arcane_success_requires_success_true(
    load_environment, payload_fixture, monkeypatch, environment, fixture_name
):
    module = load_environment(environment, DRY_RUN="false")
    payload = payload_fixture(fixture_name)
    changed_files = module.collect_changed_files(payload)
    monkeypatch.setattr(module.httpx, "AsyncClient", FakeArcaneClient)

    if environment == "production":
        result = asyncio.run(
            module.post_arcane_webhook("core", "https://arcane.example/secret", payload)
        )
    else:
        result = asyncio.run(module.post_arcane_test_webhook(payload, changed_files))

    assert result.ok is True
    assert result.status == "success"
    assert result.status_code == 200
    assert result.arcane_success is True
    assert result.data is None


def test_http_200_with_success_false_is_failure(
    load_environment, payload_fixture, monkeypatch
):
    module = load_environment("production", DRY_RUN="false")
    payload = payload_fixture("production_merge.json")

    class FalseClient(FakeArcaneClient):
        response = FakeArcaneResponse(200, {"success": False, "data": None})

    monkeypatch.setattr(module.httpx, "AsyncClient", FalseClient)
    result = asyncio.run(
        module.post_arcane_webhook("core", "https://arcane.example/secret", payload)
    )

    assert result.ok is False
    assert result.status == "failed"
    assert result.error == "Arcane response did not contain success=true"


def test_dry_run_is_reported_as_skipped(load_environment, payload_fixture):
    module = load_environment("production", DRY_RUN="true")
    payload = payload_fixture("production_merge.json")

    result = asyncio.run(
        module.post_arcane_webhook("core", "https://arcane.example/secret", payload)
    )

    assert result.status == "skipped"
    assert result.ok is False


def test_arcane_timeout_message_does_not_expose_webhook_url(
    load_environment, payload_fixture, monkeypatch
):
    module = load_environment("production", DRY_RUN="false")
    payload = payload_fixture("production_merge.json")
    secret_url = "https://arcane.example/api/webhooks/trigger/super-secret-token"

    class TimeoutClient(FakeArcaneClient):
        async def post(self, url, json, headers):
            raise module.httpx.ReadTimeout("read timed out")

    monkeypatch.setattr(module.httpx, "AsyncClient", TimeoutClient)
    result = asyncio.run(module.post_arcane_webhook("core", secret_url, payload))

    assert result.ok is False
    assert "ReadTimeout" in result.error
    assert "super-secret-token" not in result.error


def test_dispatch_preserves_email_order_and_continues_after_email_failure(
    load_environment, payload_fixture, monkeypatch
):
    module = load_environment("production", EMAIL_ENABLED="false")
    notifications = module.infer_pull_request.__module__
    notification_module = __import__(notifications, fromlist=["EmailConfig"])
    payload = payload_fixture("production_merge.json")
    changed_files = module.collect_changed_files(payload)
    context = module.build_webhook_context(
        payload,
        delivery_id="delivery-order",
        event="push",
        environment_name="ApexCamp Production",
        targets=["core"],
        changed_files=changed_files,
        received_at=module.datetime.now(module.timezone.utc),
    )
    module.EMAIL_CONFIG = notification_module.EmailConfig(
        enabled=True,
        smtp_host="smtp.example.com",
        smtp_port=465,
        smtp_username="sender@example.com",
        smtp_password="secret",
        from_address="sender@example.com",
        to_addresses=("recipient@example.com",),
        tls_mode="ssl",
        timeout_seconds=10,
        timezone_name="Asia/Shanghai",
        logo_url="",
        arcane_app_url="https://arcane.example",
        environment_name="ApexCamp Production",
    )
    events = []

    async def fake_run_arcane(targets, event_payload):
        events.append("arcane_started")
        await asyncio.sleep(0)
        events.append("arcane_finished")
        return [
            notification_module.SyncResult(
                target="core",
                status="success",
                ok=True,
                status_code=200,
                arcane_success=True,
                data=None,
                error="",
                response_excerpt="",
                duration_seconds=1,
                completed_at=module.datetime.now(module.timezone.utc),
            )
        ]

    async def fake_resolve(context, **kwargs):
        await asyncio.sleep(0)
        return module.infer_pull_request(context)

    async def fake_send(config, *, phase, **kwargs):
        events.append(phase)
        return phase != "received"

    monkeypatch.setattr(module, "run_arcane_webhooks", fake_run_arcane)
    monkeypatch.setattr(module, "resolve_pull_request", fake_resolve)
    monkeypatch.setattr(module, "send_email", fake_send)

    asyncio.run(module.dispatch_arcane_webhooks(["core"], payload, context))

    assert events[0] == "arcane_started"
    assert events.index("received") < events.index("result")
    assert events.index("arcane_finished") < events.index("result")
