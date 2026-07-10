import asyncio
import hashlib
import hmac
import logging
import os
from datetime import datetime, timezone
from time import monotonic
from typing import Any, Dict, List, Set
from uuid import uuid4

import httpx
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from .notifications import (
    EmailConfig,
    PullRequestInfo,
    PullRequestVerificationError,
    SyncResult,
    WebhookContext,
    build_webhook_context,
    render_received_email,
    render_result_email,
    resolve_merged_pull_request,
    send_email,
)

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("apexcamp-test-deploy-webhook-relay")

app = FastAPI(title="ApexCamp Test Deploy Webhook Relay")

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
DEPLOY_REF = os.getenv("DEPLOY_REF", "refs/heads/main")
DEPLOY_REPOSITORY = os.getenv("DEPLOY_REPOSITORY", "eason8811/apex-camp-deploy")
TEST_PREFIX = os.getenv("TEST_PREFIX", "environments/test/")
ARCANE_TEST_WEBHOOK_URL = os.getenv("ARCANE_TEST_WEBHOOK_URL", "")
DRY_RUN = os.getenv("DRY_RUN", "false").lower() in {"1", "true", "yes", "on"}
HTTP_CONNECT_TIMEOUT_SECONDS = float(os.getenv("HTTP_CONNECT_TIMEOUT_SECONDS", "10"))
HTTP_TIMEOUT_SECONDS = float(os.getenv("HTTP_TIMEOUT_SECONDS", "600"))
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
GITHUB_API_TIMEOUT_SECONDS = float(os.getenv("GITHUB_API_TIMEOUT_SECONDS", "5"))
GITHUB_API_VERSION = os.getenv("GITHUB_API_VERSION", "2026-03-10")
CI_PULL_REQUEST_HEAD_PREFIXES = tuple(
    prefix.strip()
    for prefix in os.getenv("CI_PULL_REQUEST_HEAD_PREFIXES", "ci/").split(",")
    if prefix.strip()
)
EMAIL_CONFIG = EmailConfig.from_env("ApexCamp Test")

if EMAIL_CONFIG.enabled and not EMAIL_CONFIG.configured:
    logger.error(
        "Email notifications enabled but invalid: %s", EMAIL_CONFIG.validation_error
    )


def verify_github_signature(body: bytes, signature: str | None) -> None:
    if not WEBHOOK_SECRET:
        logger.error("WEBHOOK_SECRET is not configured")
        raise HTTPException(status_code=500, detail="relay secret not configured")

    if not signature or not signature.startswith("sha256="):
        raise HTTPException(status_code=401, detail="missing github signature")

    expected = (
        "sha256="
        + hmac.new(WEBHOOK_SECRET.encode("utf-8"), body, hashlib.sha256).hexdigest()
    )

    if not hmac.compare_digest(expected, signature):
        raise HTTPException(status_code=401, detail="invalid github signature")


def collect_changed_files(payload: Dict[str, Any]) -> List[str]:
    changed: Set[str] = set()

    for commit in payload.get("commits") or []:
        for key in ("added", "modified", "removed"):
            for path in commit.get(key) or []:
                if isinstance(path, str) and path:
                    changed.add(path)

    head_commit = payload.get("head_commit") or {}
    for key in ("added", "modified", "removed"):
        for path in head_commit.get(key) or []:
            if isinstance(path, str) and path:
                changed.add(path)

    return sorted(changed)


def should_trigger_test(changed_files: List[str]) -> bool:
    return any(path.startswith(TEST_PREFIX) for path in changed_files)


def is_ci_generated_pull_request(pull_request: PullRequestInfo) -> bool:
    return pull_request.is_merge and any(
        pull_request.head_ref.startswith(prefix)
        for prefix in CI_PULL_REQUEST_HEAD_PREFIXES
    )


def build_sync_result(
    *,
    status: str,
    ok: bool,
    started_at: float,
    status_code: int | None = None,
    arcane_success: bool | None = None,
    data: Any = None,
    error: str = "",
    response_excerpt: str = "",
) -> SyncResult:
    return SyncResult(
        target="test",
        status=status,
        ok=ok,
        status_code=status_code,
        arcane_success=arcane_success,
        data=data,
        error=error,
        response_excerpt=response_excerpt,
        duration_seconds=monotonic() - started_at,
        completed_at=datetime.now(timezone.utc),
    )


async def post_arcane_test_webhook(
    payload: Dict[str, Any], changed_files: List[str]
) -> SyncResult:
    started_at = monotonic()
    if not ARCANE_TEST_WEBHOOK_URL:
        logger.error("ARCANE_TEST_WEBHOOK_URL is not configured")
        return build_sync_result(
            status="failed",
            ok=False,
            started_at=started_at,
            error="missing Arcane test webhook URL",
        )

    if DRY_RUN:
        logger.info("DRY_RUN enabled; skip Arcane test webhook")
        return build_sync_result(
            status="skipped",
            ok=False,
            started_at=started_at,
        )

    body = {
        "source": "apexcamp-test-deploy-webhook-relay",
        "target": "test",
        "repository": payload.get("repository", {}).get("full_name"),
        "ref": payload.get("ref"),
        "after": payload.get("after"),
        "sender": (payload.get("sender") or {}).get("login"),
        "changed_files": changed_files,
    }

    try:
        timeout = httpx.Timeout(
            connect=HTTP_CONNECT_TIMEOUT_SECONDS,
            read=HTTP_TIMEOUT_SECONDS,
            write=HTTP_CONNECT_TIMEOUT_SECONDS,
            pool=HTTP_CONNECT_TIMEOUT_SECONDS,
        )
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            resp = await client.post(
                ARCANE_TEST_WEBHOOK_URL,
                json=body,
                headers={"User-Agent": "apexcamp-test-deploy-webhook-relay"},
            )
            response_excerpt = resp.text[:500]
            parsed: Any = None
            try:
                parsed = resp.json()
            except ValueError:
                parsed = None

            arcane_success = parsed.get("success") if isinstance(parsed, dict) else None
            data = parsed.get("data") if isinstance(parsed, dict) else None
            ok = 200 <= resp.status_code < 300 and arcane_success is True
            error = ""
            if not 200 <= resp.status_code < 300:
                error = (
                    str(
                        parsed.get("error")
                        or f"Arcane returned HTTP {resp.status_code}"
                    )
                    if isinstance(parsed, dict)
                    else f"Arcane returned HTTP {resp.status_code}"
                )
            elif arcane_success is not True:
                error = "Arcane response did not contain success=true"

            logger.info(
                "Arcane test webhook status=%s success=%s body=%s",
                resp.status_code,
                arcane_success,
                response_excerpt,
            )
            return build_sync_result(
                status="success" if ok else "failed",
                ok=ok,
                started_at=started_at,
                status_code=resp.status_code,
                arcane_success=arcane_success
                if isinstance(arcane_success, bool)
                else None,
                data=data,
                error=error,
                response_excerpt=response_excerpt,
            )
    except httpx.TimeoutException as exc:
        logger.error(
            "Arcane test webhook timed out error_type=%s connect_timeout=%ss read_timeout=%ss",
            type(exc).__name__,
            HTTP_CONNECT_TIMEOUT_SECONDS,
            HTTP_TIMEOUT_SECONDS,
        )
        return build_sync_result(
            status="failed",
            ok=False,
            started_at=started_at,
            error=(
                f"Arcane request timed out ({type(exc).__name__}; "
                f"connect={HTTP_CONNECT_TIMEOUT_SECONDS:g}s, read={HTTP_TIMEOUT_SECONDS:g}s)"
            ),
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("Arcane test webhook failed error_type=%s", type(exc).__name__)
        return build_sync_result(
            status="failed",
            ok=False,
            started_at=started_at,
            error=f"Arcane request failed ({type(exc).__name__})",
        )


async def dispatch_test_webhook(
    payload: Dict[str, Any],
    changed_files: List[str],
    context: WebhookContext,
    pull_request: PullRequestInfo,
) -> None:
    logger.info(
        "Dispatch test webhook delivery_id=%s ref=%s after=%s changed_files=%s",
        context.delivery_id,
        payload.get("ref"),
        payload.get("after"),
        changed_files,
    )
    arcane_task = asyncio.create_task(post_arcane_test_webhook(payload, changed_files))
    await asyncio.sleep(0)

    if EMAIL_CONFIG.enabled:
        try:
            subject, text_body, html_body = render_received_email(
                context, pull_request, EMAIL_CONFIG
            )
            await send_email(
                EMAIL_CONFIG,
                subject=subject,
                text_body=text_body,
                html_body=html_body,
                delivery_id=context.delivery_id,
                phase="received",
                logger=logger,
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "Received email rendering failed delivery_id=%s", context.delivery_id
            )

    result = await arcane_task

    if EMAIL_CONFIG.enabled:
        try:
            subject, text_body, html_body = render_result_email(
                context, pull_request, [result], EMAIL_CONFIG
            )
            await send_email(
                EMAIL_CONFIG,
                subject=subject,
                text_body=text_body,
                html_body=html_body,
                delivery_id=context.delivery_id,
                phase="result",
                logger=logger,
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "Result email rendering failed delivery_id=%s", context.delivery_id
            )


@app.get("/healthz")
def healthz() -> Dict[str, Any]:
    return {
        "ok": True,
        "dry_run": DRY_RUN,
        "deploy_ref": DEPLOY_REF,
        "deploy_repository": DEPLOY_REPOSITORY,
        "test_prefix": TEST_PREFIX,
        "test_webhook_configured": bool(ARCANE_TEST_WEBHOOK_URL),
        "email_enabled": EMAIL_CONFIG.enabled,
        "email_configured": EMAIL_CONFIG.configured,
        "github_pr_merge_verification_configured": bool(GITHUB_TOKEN),
        "ci_pull_request_head_prefixes": CI_PULL_REQUEST_HEAD_PREFIXES,
        "arcane_connect_timeout_seconds": HTTP_CONNECT_TIMEOUT_SECONDS,
        "arcane_read_timeout_seconds": HTTP_TIMEOUT_SECONDS,
    }


@app.post("/webhooks/deploy-test")
async def github_deploy_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_github_event: str | None = Header(default=None),
    x_github_delivery: str | None = Header(default=None),
    x_hub_signature_256: str | None = Header(default=None),
) -> JSONResponse:
    received_at = datetime.now(timezone.utc)
    body = await request.body()
    verify_github_signature(body, x_hub_signature_256)

    if x_github_event != "push":
        return JSONResponse(
            {
                "ok": True,
                "ignored": True,
                "reason": f"unsupported event {x_github_event}",
            }
        )

    try:
        payload = await request.json()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail="invalid json payload") from exc

    repository = (payload.get("repository") or {}).get("full_name")
    if repository and repository != DEPLOY_REPOSITORY:
        return JSONResponse(
            {
                "ok": True,
                "ignored": True,
                "reason": f"repository {repository} != {DEPLOY_REPOSITORY}",
            }
        )

    ref = payload.get("ref")
    if ref != DEPLOY_REF:
        return JSONResponse(
            {"ok": True, "ignored": True, "reason": f"ref {ref} != {DEPLOY_REF}"}
        )

    changed_files = collect_changed_files(payload)
    trigger = should_trigger_test(changed_files)

    logger.info(
        "Webhook received ref=%s repository=%s changed=%s trigger=%s",
        ref,
        repository,
        changed_files,
        trigger,
    )

    if not trigger:
        return JSONResponse(
            {
                "ok": True,
                "ignored": True,
                "reason": "no test environment changes",
                "changed_files": changed_files,
            }
        )

    delivery_id = x_github_delivery or str(uuid4())
    context = build_webhook_context(
        payload,
        delivery_id=delivery_id,
        event=x_github_event,
        environment_name=EMAIL_CONFIG.environment_name,
        targets=["test"],
        changed_files=changed_files,
        received_at=received_at,
        dry_run=DRY_RUN,
    )
    try:
        pull_request = await resolve_merged_pull_request(
            context,
            token=GITHUB_TOKEN,
            timeout_seconds=GITHUB_API_TIMEOUT_SECONDS,
            api_version=GITHUB_API_VERSION,
            logger=logger,
        )
    except PullRequestVerificationError as exc:
        logger.error(
            "Rejecting delivery_id=%s because merged PR verification failed: %s",
            delivery_id,
            exc,
        )
        raise HTTPException(
            status_code=503,
            detail="merged pull request verification is unavailable",
        ) from exc

    if pull_request is None:
        return JSONResponse(
            {
                "ok": True,
                "ignored": True,
                "reason": "push is not the merge commit of a pull request",
                "changed_files": changed_files,
            }
        )

    if not is_ci_generated_pull_request(pull_request):
        return JSONResponse(
            {
                "ok": True,
                "ignored": True,
                "reason": "pull request was not created by the CI branch policy",
                "pull_request": pull_request.number,
                "head_ref": pull_request.head_ref,
            }
        )

    background_tasks.add_task(
        dispatch_test_webhook, payload, changed_files, context, pull_request
    )

    return JSONResponse(
        status_code=202,
        content={
            "ok": True,
            "accepted": True,
            "delivery_id": delivery_id,
            "dry_run": DRY_RUN,
            "target": "test",
            "changed_files": changed_files,
        },
    )
