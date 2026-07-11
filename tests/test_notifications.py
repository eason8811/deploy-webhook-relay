import asyncio
import logging
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path


def make_config(module):
    return module.EmailConfig(
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
        logo_url="https://arcane.example/api/app-images/logo-email",
        arcane_app_url="https://arcane.example",
        environment_name="ApexCamp Production",
    )


def make_context(module, payload, changed_files=None):
    files = changed_files
    if files is None:
        head = payload.get("head_commit") or {}
        files = (
            head.get("added", []) + head.get("modified", []) + head.get("removed", [])
        )
    return module.build_webhook_context(
        payload,
        delivery_id="delivery-123",
        event="push",
        environment_name="ApexCamp Production",
        targets=["core"],
        changed_files=files,
        received_at=datetime(2026, 7, 10, 4, 0, tzinfo=timezone.utc),
    )


def test_notification_modules_remain_identical():
    root = Path(__file__).resolve().parents[1]
    production = (root / "environment/production/app/notifications.py").read_bytes()
    test = (root / "environment/test/app/notifications.py").read_bytes()
    assert production == test


def test_merge_commit_fallback_uses_uploaded_payload_shape(
    load_environment, payload_fixture
):
    module = load_environment("production", "app.notifications")
    context = make_context(module, payload_fixture("production_merge.json"))

    result = module.infer_pull_request(context)

    assert result.is_merge is True
    assert result.number == 28
    assert result.source == "commit_message"
    assert result.head_ref == "eason8811/ci/prod/core/1fe1fc9aa159"
    assert result.title == "chore(prod): update core image digests 1fe1fc9aa159"


def test_direct_push_is_not_reported_as_pr_merge(load_environment, payload_fixture):
    module = load_environment("production", "app.notifications")
    context = make_context(module, payload_fixture("production_direct.json"))

    result = module.infer_pull_request(context)

    assert result.is_merge is False


def test_github_api_result_takes_precedence(
    load_environment, payload_fixture, monkeypatch
):
    module = load_environment("production", "app.notifications")
    context = make_context(module, payload_fixture("production_merge.json"))

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return [
                {
                    "number": 28,
                    "title": "Update production images",
                    "html_url": "https://github.com/eason8811/apex-camp-deploy/pull/28",
                    "merged_at": "2026-07-10T03:40:01Z",
                    "merge_commit_sha": context.after,
                    "head": {"ref": "ci/prod/core/1fe1fc9aa159"},
                    "base": {"ref": "main"},
                    "merged_by": {"login": "eason8811"},
                }
            ]

    class FakeClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        async def get(self, url, headers):
            assert context.after in url
            assert headers["Authorization"] == "Bearer token"
            return FakeResponse()

    monkeypatch.setattr(module.httpx, "AsyncClient", FakeClient)
    result = asyncio.run(
        module.resolve_pull_request(
            context,
            token="token",
            timeout_seconds=5,
            api_version="2026-03-10",
            logger=logging.getLogger("test"),
        )
    )

    assert result.is_merge is True
    assert result.source == "github_api"
    assert result.title == "Update production images"
    assert result.head_ref == "ci/prod/core/1fe1fc9aa159"


def test_strict_pr_verification_accepts_associated_merge_when_api_sha_is_stale(
    load_environment, payload_fixture, monkeypatch
):
    module = load_environment("test", "app.notifications")
    context = make_context(module, payload_fixture("test_merge.json"))

    class FakeResponse:
        def __init__(self, merged_at):
            self.merged_at = merged_at

        def raise_for_status(self):
            return None

        def json(self):
            return [
                {
                    "number": 27,
                    "title": "Update test images",
                    "html_url": "https://github.com/eason8811/apex-camp-deploy/pull/27",
                    "merged_at": self.merged_at,
                    "merge_commit_sha": "github-test-merge-sha-not-push-after",
                    "head": {"ref": "ci/test/apex-community-c83496c693a4"},
                    "base": {"ref": "main"},
                    "merged_by": {"login": "eason8811"},
                }
            ]

    class FakeClient:
        call_count = 0

        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        async def get(self, url, headers):
            assert context.after in url
            self.__class__.call_count += 1
            merged_at = (
                None
                if self.__class__.call_count == 1
                else "2026-07-10T03:40:01Z"
            )
            return FakeResponse(merged_at)

    monkeypatch.setattr(module.httpx, "AsyncClient", FakeClient)
    result = asyncio.run(
        module.resolve_merged_pull_request(
            context,
            token="token",
            timeout_seconds=5,
            api_version="2026-03-10",
            logger=logging.getLogger("test"),
        )
    )

    assert result is not None
    assert result.source == "github_api"
    assert result.number == 27
    assert result.head_ref == "ci/test/apex-community-c83496c693a4"
    assert FakeClient.call_count == 2


def test_strict_pr_verification_rejects_different_associated_pr_number(
    load_environment, payload_fixture, monkeypatch
):
    module = load_environment("test", "app.notifications")
    context = make_context(module, payload_fixture("test_merge.json"))

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return [
                {
                    "number": 999,
                    "merged_at": "2026-07-10T03:40:01Z",
                    "merge_commit_sha": "a-different-commit",
                    "base": {"ref": "main"},
                }
            ]

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        async def get(self, url, headers):
            return FakeResponse()

    monkeypatch.setattr(module.httpx, "AsyncClient", FakeClient)
    result = asyncio.run(
        module.resolve_merged_pull_request(
            context,
            token="token",
            timeout_seconds=5,
            api_version="2026-03-10",
            logger=logging.getLogger("test"),
        )
    )

    assert result is None


def test_strict_pr_verification_rejects_a_non_merge_commit(
    load_environment, payload_fixture, monkeypatch
):
    module = load_environment("production", "app.notifications")
    context = make_context(module, payload_fixture("production_direct.json"))

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return [
                {
                    "number": 99,
                    "merged_at": "2026-07-10T03:40:01Z",
                    "merge_commit_sha": "a-different-commit",
                    "base": {"ref": "main"},
                }
            ]

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        async def get(self, url, headers):
            return FakeResponse()

    monkeypatch.setattr(module.httpx, "AsyncClient", FakeClient)
    result = asyncio.run(
        module.resolve_merged_pull_request(
            context,
            token="token",
            timeout_seconds=5,
            api_version="2026-03-10",
            logger=logging.getLogger("test"),
        )
    )

    assert result is None


def test_github_api_failure_uses_commit_fallback(
    load_environment, payload_fixture, monkeypatch
):
    module = load_environment("production", "app.notifications")
    context = make_context(module, payload_fixture("production_merge.json"))

    class FailingClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            raise RuntimeError("offline")

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    monkeypatch.setattr(module.httpx, "AsyncClient", FailingClient)
    result = asyncio.run(
        module.resolve_pull_request(
            context,
            token="token",
            timeout_seconds=5,
            api_version="2026-03-10",
            logger=logging.getLogger("test"),
        )
    )

    assert result.is_merge is True
    assert result.source == "commit_message"
    assert result.number == 28


def test_received_email_escapes_payload_and_limits_files(
    load_environment, payload_fixture
):
    module = load_environment("production", "app.notifications")
    payload = payload_fixture("production_direct.json")
    payload["head_commit"]["message"] = "<script>alert('x')</script>"
    files = [f"environments/production/core/file-{index}.yaml" for index in range(25)]
    context = make_context(module, payload, files)
    config = make_config(module)

    subject, text_body, html_body = module.render_received_email(
        context, module.infer_pull_request(context), config
    )

    assert "[已接收]" in subject
    assert "<script>" not in html_body
    assert "&lt;script&gt;" in html_body
    assert "另有 5 项未展示" in html_body
    assert "file-19.yaml" in text_body
    assert "file-20.yaml" not in text_body


def test_received_email_labels_dry_run_without_claiming_arcane_was_called(
    load_environment, payload_fixture
):
    module = load_environment("production", "app.notifications")
    context = replace(
        make_context(module, payload_fixture("production_merge.json")), dry_run=True
    )

    _, text_body, html_body = module.render_received_email(
        context, module.infer_pull_request(context), make_config(module)
    )

    assert "DRY_RUN" in text_body
    assert "不会调用 Arcane WebHook" in html_body
    assert "已在后台触发 Arcane" not in html_body


def test_result_email_accepts_arcane_success_with_null_data(
    load_environment, payload_fixture
):
    module = load_environment("production", "app.notifications")
    context = make_context(module, payload_fixture("production_merge.json"))
    config = make_config(module)
    result = module.SyncResult(
        target="core",
        status="success",
        ok=True,
        status_code=200,
        arcane_success=True,
        data=None,
        error="",
        response_excerpt='{"success":true,"data":null}',
        duration_seconds=12.5,
        completed_at=datetime(2026, 7, 10, 4, 1, tzinfo=timezone.utc),
    )

    subject, text_body, html_body = module.render_result_email(
        context, module.infer_pull_request(context), [result], config
    )

    assert "[同步成功]" in subject
    assert "Arcane data: null" in text_body
    assert "同步成功" in html_body
    assert ">null</code>" in html_body


def test_result_email_marks_mixed_targets_as_partial_failure(
    load_environment, payload_fixture
):
    module = load_environment("production", "app.notifications")
    context = replace(
        make_context(module, payload_fixture("production_merge.json")),
        targets=("core", "portal"),
    )
    config = make_config(module)
    success = module.SyncResult(
        target="core",
        status="success",
        ok=True,
        status_code=200,
        arcane_success=True,
        data=None,
        error="",
        response_excerpt="",
        duration_seconds=1,
        completed_at=datetime.now(timezone.utc),
    )
    failed = replace(
        success,
        target="portal",
        status="failed",
        ok=False,
        status_code=500,
        arcane_success=False,
        error="<internal failure>",
    )

    subject, _, html_body = module.render_result_email(
        context, module.infer_pull_request(context), [success, failed], config
    )

    assert "[部分失败]" in subject
    assert "&lt;internal failure&gt;" in html_body
    assert "<internal failure>" not in html_body


def test_smtp_ssl_builds_multipart_message(load_environment, monkeypatch):
    module = load_environment("production", "app.notifications")
    config = make_config(module)
    captured = {}

    class FakeSMTP:
        def __init__(self, host, port, local_hostname, timeout, context):
            captured.update(
                host=host,
                port=port,
                local_hostname=local_hostname,
                timeout=timeout,
                context=context,
            )

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def login(self, username, password):
            captured["login"] = (username, password)

        def send_message(self, message):
            captured["message"] = message

    monkeypatch.setattr(module.smtplib, "SMTP_SSL", FakeSMTP)
    module._send_email_sync(config, "主题", "纯文本", "<strong>HTML</strong>")

    assert captured["host"] == "smtp.example.com"
    assert captured["port"] == 465
    assert captured["local_hostname"] == "localhost"
    assert captured["login"] == ("sender@example.com", "secret")
    message = captured["message"]
    assert message.is_multipart()
    assert message.get_body(preferencelist=("plain",)).get_content().strip() == "纯文本"
    assert (
        "<strong>HTML</strong>"
        in message.get_body(preferencelist=("html",)).get_content()
    )
