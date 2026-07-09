import hashlib
import hmac
import logging
import os
from typing import Any, Dict, List, Set

import httpx
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("deploy-webhook-relay")

app = FastAPI(title="ApexCamp Deploy Webhook Relay")

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
DEPLOY_REF = os.getenv("DEPLOY_REF", "refs/heads/main")
CORE_PREFIX = os.getenv("CORE_PREFIX", "environments/production/core/")
PORTAL_PREFIX = os.getenv("PORTAL_PREFIX", "environments/production/portal-111/")
ARCANE_CORE_WEBHOOK_URL = os.getenv("ARCANE_CORE_WEBHOOK_URL", "")
ARCANE_PORTAL_WEBHOOK_URL = os.getenv("ARCANE_PORTAL_WEBHOOK_URL", "")
DRY_RUN = os.getenv("DRY_RUN", "false").lower() in {"1", "true", "yes", "on"}
HTTP_TIMEOUT_SECONDS = float(os.getenv("HTTP_TIMEOUT_SECONDS", "20"))


def verify_github_signature(body: bytes, signature: str | None) -> None:
    if not WEBHOOK_SECRET:
        logger.error("WEBHOOK_SECRET is not configured")
        raise HTTPException(status_code=500, detail="relay secret not configured")

    if not signature or not signature.startswith("sha256="):
        raise HTTPException(status_code=401, detail="missing github signature")

    expected = "sha256=" + hmac.new(
        WEBHOOK_SECRET.encode("utf-8"), body, hashlib.sha256
    ).hexdigest()

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


async def post_arcane_webhook(target: str, url: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    if not url:
        logger.error("Arcane webhook URL for target=%s is not configured", target)
        return {"target": target, "ok": False, "error": "missing Arcane webhook URL"}

    if DRY_RUN:
        logger.info("DRY_RUN enabled; skip Arcane webhook target=%s url=%s", target, url)
        return {"target": target, "ok": True, "dry_run": True}

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
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS, follow_redirects=True) as client:
            resp = await client.post(url, json=body, headers={"User-Agent": "apexcamp-deploy-webhook-relay"})
            logger.info(
                "Arcane webhook target=%s status=%s body=%s",
                target,
                resp.status_code,
                resp.text[:500],
            )
            return {
                "target": target,
                "ok": 200 <= resp.status_code < 300,
                "status_code": resp.status_code,
            }
    except Exception as exc:  # noqa: BLE001
        logger.exception("Arcane webhook target=%s failed", target)
        return {"target": target, "ok": False, "error": str(exc)}


async def dispatch_arcane_webhooks(targets: List[str], payload: Dict[str, Any]) -> None:
    logger.info("Dispatch targets=%s ref=%s after=%s", targets, payload.get("ref"), payload.get("after"))
    for target in targets:
        if target == "core":
            await post_arcane_webhook("core", ARCANE_CORE_WEBHOOK_URL, payload)
        elif target == "portal":
            await post_arcane_webhook("portal", ARCANE_PORTAL_WEBHOOK_URL, payload)


@app.get("/healthz")
def healthz() -> Dict[str, Any]:
    return {
        "ok": True,
        "dry_run": DRY_RUN,
        "deploy_ref": DEPLOY_REF,
        "core_webhook_configured": bool(ARCANE_CORE_WEBHOOK_URL),
        "portal_webhook_configured": bool(ARCANE_PORTAL_WEBHOOK_URL),
    }


@app.post("/webhooks/deploy")
async def github_deploy_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_github_event: str | None = Header(default=None),
    x_hub_signature_256: str | None = Header(default=None),
) -> JSONResponse:
    body = await request.body()
    verify_github_signature(body, x_hub_signature_256)

    if x_github_event != "push":
        return JSONResponse({"ok": True, "ignored": True, "reason": f"unsupported event {x_github_event}"})

    try:
        payload = await request.json()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail="invalid json payload") from exc

    ref = payload.get("ref")
    if ref != DEPLOY_REF:
        return JSONResponse({"ok": True, "ignored": True, "reason": f"ref {ref} != {DEPLOY_REF}"})

    changed_files = collect_changed_files(payload)
    targets = classify_targets(changed_files)

    logger.info("Webhook received ref=%s changed=%s targets=%s", ref, changed_files, targets)

    if not targets:
        return JSONResponse({"ok": True, "ignored": True, "reason": "no production core/portal changes", "changed_files": changed_files})

    background_tasks.add_task(dispatch_arcane_webhooks, targets, payload)

    return JSONResponse(
        status_code=202,
        content={
            "ok": True,
            "accepted": True,
            "dry_run": DRY_RUN,
            "targets": targets,
            "changed_files": changed_files,
        },
    )
