import importlib
import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"

ENV_KEYS = {
    "ARCANE_APP_URL",
    "ARCANE_CORE_WEBHOOK_URL",
    "ARCANE_PORTAL_WEBHOOK_URL",
    "ARCANE_TEST_WEBHOOK_URL",
    "CI_PULL_REQUEST_HEAD_PREFIXES",
    "DEPLOY_REPOSITORY",
    "DRY_RUN",
    "EMAIL_ENABLED",
    "EMAIL_LOGO_URL",
    "EMAIL_TIMEZONE",
    "GITHUB_TOKEN",
    "RELAY_ENVIRONMENT_NAME",
    "SMTP_FROM_ADDRESS",
    "SMTP_HOST",
    "SMTP_PASSWORD",
    "SMTP_PORT",
    "SMTP_TIMEOUT_SECONDS",
    "SMTP_TLS_MODE",
    "SMTP_TO_ADDRESSES",
    "SMTP_USERNAME",
    "WEBHOOK_SECRET",
}


def clear_app_modules() -> None:
    for name in list(sys.modules):
        if name == "app" or name.startswith("app."):
            del sys.modules[name]


@pytest.fixture
def load_environment(monkeypatch):
    def load(
        environment: str,
        module_name: str = "app.main",
        **overrides: str,
    ):
        clear_app_modules()
        for key in ENV_KEYS:
            monkeypatch.delenv(key, raising=False)

        defaults = {
            "WEBHOOK_SECRET": "test-webhook-secret",
            "DRY_RUN": "true",
            "EMAIL_ENABLED": "false",
            "ARCANE_CORE_WEBHOOK_URL": "https://arcane.example/api/webhooks/trigger/core-secret",
            "ARCANE_PORTAL_WEBHOOK_URL": "https://arcane.example/api/webhooks/trigger/portal-secret",
            "ARCANE_TEST_WEBHOOK_URL": "https://arcane.example/api/webhooks/trigger/test-secret",
            "GITHUB_TOKEN": "test-github-token",
        }
        defaults.update(overrides)
        for key, value in defaults.items():
            monkeypatch.setenv(key, value)

        monkeypatch.syspath_prepend(str(ROOT / "environment" / environment))
        return importlib.import_module(module_name)

    yield load
    clear_app_modules()


@pytest.fixture
def payload_fixture():
    def load(name: str) -> dict:
        return json.loads((FIXTURES / name).read_text(encoding="utf-8"))

    return load
