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
logger = logging.getLogger("deploy-webhook-relay")

app = FastAPI(title="ApexCamp Deploy Webhook Relay")

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
DEPLOY_REF = os.getenv("DEPLOY_REF", "refs/heads/main")
DEPLOY_REPOSITORY = os.getenv("DEPLOY_REPOSITORY", "eason8811/apex-camp-deploy")
CORE_PREFIX = os.getenv("CORE_PREFIX", "environments/production/core/")
PORTAL_PREFIX = os.getenv("PORTAL_PREFIX", "environments/production/portal-111/")
ARCANE_CORE_WEBHOOK_URL = os.getenv("ARCANE_CORE_WEBHOOK_URL", "")
ARCANE_PORTAL_WEBHOOK_URL = os.getenv("ARCANE_PORTAL_WEBHOOK_URL", "")
DRY_RUN = os.getenv("DRY_RUN", "false").lower() in {"1", "true", "yes", "on"}
HTTP_CONNECT_TIMEOUT_SECONDS = float(os.getenv("HTTP_CONNECT_TIMEOUT_SECONDS", "10"))
HTTP_TIMEOUT_SECONDS = float(os.getenv("HTTP_TIMEOUT_SECONDS", "600"))
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
GITHUB_API_TIMEOUT_SECONDS = float(os.getenv("GITHUB_API_TIMEOUT_SECONDS", "5"))
GITHUB_PR_VERIFICATION_MAX_SECONDS = float(
    os.getenv("GITHUB_PR_VERIFICATION_MAX_SECONDS", "8")
)
GITHUB_API_VERSION = os.getenv("GITHUB_API_VERSION", "2026-03-10")
CI_PULL_REQUEST_HEAD_PREFIXES = tuple(
    prefix.strip()
    for prefix in os.getenv("CI_PULL_REQUEST_HEAD_PREFIXES", "ci/").split(",")
    if prefix.strip()
)
EMAIL_CONFIG = EmailConfig.from_env("ApexCamp Production")

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


def classify_targets(changed_files: List[str]) -> List[str]:
    targets: List[str] = []
    if any(path.startswith(CORE_PREFIX) for path in changed_files):
        targets.append("core")
    if any(path.startswith(PORTAL_PREFIX) for path in changed_files):
        targets.append("portal")
    return targets


def is_ci_generated_pull_request(pull_request: PullRequestInfo) -> bool:
    return pull_request.is_merge and any(
        pull_request.head_ref.startswith(prefix)
        for prefix in CI_PULL_REQUEST_HEAD_PREFIXES
    )


def build_sync_result(
    *,
    target: str,
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
        target=target,
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


async def post_arcane_webhook(
    target: str, url: str, payload: Dict[str, Any]
) -> SyncResult:
    started_at = monotonic()
    if not url:
        logger.error("Arcane webhook URL for target=%s is not configured", target)
        return build_sync_result(
            target=target,
            status="failed",
            ok=False,
            started_at=started_at,
            error="missing Arcane webhook URL",
        )

    if DRY_RUN:
        logger.info("DRY_RUN enabled; skip Arcane webhook target=%s", target)
        return build_sync_result(
            target=target,
            status="skipped",
            ok=False,
            started_at=started_at,
        )

    body = {
        "source": "apexcamp-deploy-webhook-relay",
        "target": target,
        "repository": payload.get("repository", {}).get("full_name"),
        "ref": payload.get("ref"),
        "after": payload.get("after"),
        "sender": (payload.get("sender") or {}).get("login"),
        "changed_files": collect_changed_files(payload),
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
                url, json=body, headers={"User-Agent": "apexcamp-deploy-webhook-relay"}
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
                "Arcane webhook target=%s status=%s success=%s body=%s",
                target,
                resp.status_code,
                arcane_success,
                response_excerpt,
            )
            return build_sync_result(
                target=target,
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
            "Arcane webhook target=%s timed out error_type=%s connect_timeout=%ss read_timeout=%ss",
            target,
            type(exc).__name__,
            HTTP_CONNECT_TIMEOUT_SECONDS,
            HTTP_TIMEOUT_SECONDS,
        )
        return build_sync_result(
            target=target,
            status="failed",
            ok=False,
            started_at=started_at,
            error=(
                f"Arcane request timed out ({type(exc).__name__}; "
                f"connect={HTTP_CONNECT_TIMEOUT_SECONDS:g}s, read={HTTP_TIMEOUT_SECONDS:g}s)"
            ),
        )
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "Arcane webhook target=%s failed error_type=%s", target, type(exc).__name__
        )
        return build_sync_result(
            target=target,
            status="failed",
            ok=False,
            started_at=started_at,
            error=f"Arcane request failed ({type(exc).__name__})",
        )


async def run_arcane_webhooks(
    targets: List[str], payload: Dict[str, Any]
) -> List[SyncResult]:
    results: List[SyncResult] = []
    for target in targets:
        if target == "core":
            results.append(
                await post_arcane_webhook("core", ARCANE_CORE_WEBHOOK_URL, payload)
            )
        elif target == "portal":
            results.append(
                await post_arcane_webhook("portal", ARCANE_PORTAL_WEBHOOK_URL, payload)
            )
    return results


async def dispatch_arcane_webhooks(
    targets: List[str],
    payload: Dict[str, Any],
    context: WebhookContext,
    pull_request: PullRequestInfo,
) -> None:
    logger.info(
        "Dispatch delivery_id=%s targets=%s ref=%s after=%s",
        context.delivery_id,
        targets,
        payload.get("ref"),
        payload.get("after"),
    )
    arcane_task = asyncio.create_task(run_arcane_webhooks(targets, payload))
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

    results = await arcane_task

    if EMAIL_CONFIG.enabled:
        try:
            subject, text_body, html_body = render_result_email(
                context, pull_request, results, EMAIL_CONFIG
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
        "core_webhook_configured": bool(ARCANE_CORE_WEBHOOK_URL),
        "portal_webhook_configured": bool(ARCANE_PORTAL_WEBHOOK_URL),
        "email_enabled": EMAIL_CONFIG.enabled,
        "email_configured": EMAIL_CONFIG.configured,
        "github_pr_merge_verification_configured": bool(GITHUB_TOKEN),
        "github_pr_verification_max_seconds": GITHUB_PR_VERIFICATION_MAX_SECONDS,
        "ci_pull_request_head_prefixes": CI_PULL_REQUEST_HEAD_PREFIXES,
        "arcane_connect_timeout_seconds": HTTP_CONNECT_TIMEOUT_SECONDS,
        "arcane_read_timeout_seconds": HTTP_TIMEOUT_SECONDS,
    }


@app.post("/webhooks/deploy")
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
    targets = classify_targets(changed_files)

    logger.info(
        "Webhook received ref=%s changed=%s targets=%s", ref, changed_files, targets
    )

    if not targets:
        return JSONResponse(
            {
                "ok": True,
                "ignored": True,
                "reason": "no production core/portal changes",
                "changed_files": changed_files,
            }
        )

    delivery_id = x_github_delivery or str(uuid4())
    context = build_webhook_context(
        payload,
        delivery_id=delivery_id,
        event=x_github_event,
        environment_name=EMAIL_CONFIG.environment_name,
        targets=targets,
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
            max_wait_seconds=GITHUB_PR_VERIFICATION_MAX_SECONDS,
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
        dispatch_arcane_webhooks, targets, payload, context, pull_request
    )

    return JSONResponse(
        status_code=202,
        content={
            "ok": True,
            "accepted": True,
            "delivery_id": delivery_id,
            "dry_run": DRY_RUN,
            "targets": targets,
            "changed_files": changed_files,
        },
    )
