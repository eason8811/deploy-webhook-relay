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
logger = logging.getLogger("apexcamp-test-deploy-webhook-relay")

app = FastAPI(title="ApexCamp Test Deploy Webhook Relay")

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
DEPLOY_REF = os.getenv("DEPLOY_REF", "refs/heads/main")
DEPLOY_REPOSITORY = os.getenv("DEPLOY_REPOSITORY", "eason8811/apex-camp-deploy")
TEST_PREFIX = os.getenv("TEST_PREFIX", "environments/test/")
ARCANE_TEST_WEBHOOK_URL = os.getenv("ARCANE_TEST_WEBHOOK_URL", "")
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


def should_trigger_test(changed_files: List[str]) -> bool:
    return any(path.startswith(TEST_PREFIX) for path in changed_files)


async def post_arcane_test_webhook(payload: Dict[str, Any], changed_files: List[str]) -> Dict[str, Any]:
    if not ARCANE_TEST_WEBHOOK_URL:
        logger.error("ARCANE_TEST_WEBHOOK_URL is not configured")
        return {"target": "test", "ok": False, "error": "missing Arcane test webhook URL"}

    if DRY_RUN:
        logger.info("DRY_RUN enabled; skip Arcane test webhook url=%s", ARCANE_TEST_WEBHOOK_URL)
        return {"target": "test", "ok": True, "dry_run": True}

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
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS, follow_redirects=True) as client:
            resp = await client.post(
                ARCANE_TEST_WEBHOOK_URL,
                json=body,
                headers={"User-Agent": "apexcamp-test-deploy-webhook-relay"},
            )
            logger.info(
                "Arcane test webhook status=%s body=%s",
                resp.status_code,
                resp.text[:500],
            )
            return {
                "target": "test",
                "ok": 200 <= resp.status_code < 300,
                "status_code": resp.status_code,
            }
    except Exception as exc:  # noqa: BLE001
        logger.exception("Arcane test webhook failed")
        return {"target": "test", "ok": False, "error": str(exc)}


async def dispatch_test_webhook(payload: Dict[str, Any], changed_files: List[str]) -> None:
    logger.info(
        "Dispatch test webhook ref=%s after=%s changed_files=%s",
        payload.get("ref"),
        payload.get("after"),
        changed_files,
    )
    await post_arcane_test_webhook(payload, changed_files)


@app.get("/healthz")
def healthz() -> Dict[str, Any]:
    return {
        "ok": True,
        "dry_run": DRY_RUN,
        "deploy_ref": DEPLOY_REF,
        "deploy_repository": DEPLOY_REPOSITORY,
        "test_prefix": TEST_PREFIX,
        "test_webhook_configured": bool(ARCANE_TEST_WEBHOOK_URL),
    }


@app.post("/webhooks/deploy-test")
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

    repository = (payload.get("repository") or {}).get("full_name")
    if repository and repository != DEPLOY_REPOSITORY:
        return JSONResponse({"ok": True, "ignored": True, "reason": f"repository {repository} != {DEPLOY_REPOSITORY}"})

    ref = payload.get("ref")
    if ref != DEPLOY_REF:
        return JSONResponse({"ok": True, "ignored": True, "reason": f"ref {ref} != {DEPLOY_REF}"})

    changed_files = collect_changed_files(payload)
    trigger = should_trigger_test(changed_files)

    logger.info("Webhook received ref=%s repository=%s changed=%s trigger=%s", ref, repository, changed_files, trigger)

    if not trigger:
        return JSONResponse({"ok": True, "ignored": True, "reason": "no test environment changes", "changed_files": changed_files})

    background_tasks.add_task(dispatch_test_webhook, payload, changed_files)

    return JSONResponse(
        status_code=202,
        content={
            "ok": True,
            "accepted": True,
            "dry_run": DRY_RUN,
            "target": "test",
            "changed_files": changed_files,
        },
    )
