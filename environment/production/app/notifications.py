import asyncio
import html
import json
import logging
import os
import re
import smtplib
import ssl
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import parseaddr
from typing import Any, Iterable
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx


ARCANE_COLORS = {
    "background": "#24262b",
    "card": "#31343a",
    "card_border": "#3a3e45",
    "panel": "#2b2e34",
    "text_primary": "#f5f7fa",
    "text_body": "#c7cbd1",
    "text_value": "#e6e8ec",
    "text_muted": "#9298a3",
    "accent": "#c084fc",
    "accent_button": "#9333ea",
    "success": "#34d399",
    "warning": "#fbbf24",
    "danger": "#fb7185",
}

MERGE_COMMIT_PATTERN = re.compile(
    r"^Merge pull request #(?P<number>\d+) from (?P<head>[^\s]+)",
    re.MULTILINE,
)


@dataclass(frozen=True)
class EmailConfig:
    enabled: bool
    smtp_host: str
    smtp_port: int
    smtp_username: str
    smtp_password: str
    from_address: str
    to_addresses: tuple[str, ...]
    tls_mode: str
    timeout_seconds: float
    timezone_name: str
    logo_url: str
    arcane_app_url: str
    environment_name: str

    @classmethod
    def from_env(cls, default_environment_name: str) -> "EmailConfig":
        return cls(
            enabled=_env_flag("EMAIL_ENABLED", False),
            smtp_host=os.getenv("SMTP_HOST", ""),
            smtp_port=_env_int("SMTP_PORT", 465),
            smtp_username=os.getenv("SMTP_USERNAME", ""),
            smtp_password=os.getenv("SMTP_PASSWORD", ""),
            from_address=os.getenv("SMTP_FROM_ADDRESS", ""),
            to_addresses=_split_addresses(os.getenv("SMTP_TO_ADDRESSES", "")),
            tls_mode=os.getenv("SMTP_TLS_MODE", "ssl").strip().lower(),
            timeout_seconds=_env_float("SMTP_TIMEOUT_SECONDS", 10.0),
            timezone_name=os.getenv("EMAIL_TIMEZONE", "Asia/Shanghai").strip(),
            logo_url=os.getenv("EMAIL_LOGO_URL", "").strip(),
            arcane_app_url=os.getenv("ARCANE_APP_URL", "").strip(),
            environment_name=os.getenv(
                "RELAY_ENVIRONMENT_NAME", default_environment_name
            ).strip(),
        )

    @property
    def validation_error(self) -> str | None:
        if not self.smtp_host:
            return "SMTP_HOST is missing"
        if not 1 <= self.smtp_port <= 65535:
            return "SMTP_PORT must be between 1 and 65535"
        if not _valid_email_address(self.from_address):
            return "SMTP_FROM_ADDRESS is invalid"
        if not self.to_addresses:
            return "SMTP_TO_ADDRESSES is empty"
        if any(not _valid_email_address(address) for address in self.to_addresses):
            return "SMTP_TO_ADDRESSES contains an invalid address"
        if bool(self.smtp_username) != bool(self.smtp_password):
            return "SMTP_USERNAME and SMTP_PASSWORD must be configured together"
        if self.tls_mode not in {"ssl", "starttls", "none"}:
            return "SMTP_TLS_MODE must be ssl, starttls, or none"
        if self.timeout_seconds <= 0:
            return "SMTP_TIMEOUT_SECONDS must be positive"
        return None

    @property
    def configured(self) -> bool:
        return self.validation_error is None

    @property
    def display_timezone(self) -> ZoneInfo:
        try:
            return ZoneInfo(self.timezone_name)
        except ZoneInfoNotFoundError:
            return ZoneInfo("UTC")


@dataclass(frozen=True)
class WebhookContext:
    delivery_id: str
    event: str
    environment_name: str
    repository: str
    repository_url: str
    ref: str
    target_branch: str
    before: str
    after: str
    commit_url: str
    compare_url: str
    sender: str
    pusher: str
    commit_message: str
    commit_timestamp: str
    received_at: datetime
    targets: tuple[str, ...]
    changed_files: tuple[str, ...]
    dry_run: bool


@dataclass(frozen=True)
class PullRequestInfo:
    is_merge: bool
    source: str
    number: int | None = None
    title: str = ""
    url: str = ""
    head_ref: str = ""
    base_ref: str = ""
    merged_at: str = ""
    merged_by: str = ""


class PullRequestVerificationError(RuntimeError):
    """GitHub could not authoritatively verify the commit's merged PR."""


@dataclass(frozen=True)
class SyncResult:
    target: str
    status: str
    ok: bool
    status_code: int | None
    arcane_success: bool | None
    data: Any
    error: str
    response_excerpt: str
    duration_seconds: float
    completed_at: datetime


def _env_flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def _split_addresses(value: str) -> tuple[str, ...]:
    return tuple(
        address.strip() for address in re.split(r"[,;]", value) if address.strip()
    )


def _valid_email_address(value: str) -> bool:
    _, parsed = parseaddr(value)
    return bool(parsed and "@" in parsed and "\r" not in value and "\n" not in value)


def build_webhook_context(
    payload: dict[str, Any],
    *,
    delivery_id: str,
    event: str,
    environment_name: str,
    targets: Iterable[str],
    changed_files: Iterable[str],
    received_at: datetime,
    dry_run: bool = False,
) -> WebhookContext:
    repository = payload.get("repository") or {}
    head_commit = payload.get("head_commit") or {}
    sender = payload.get("sender") or {}
    pusher = payload.get("pusher") or {}
    ref = str(payload.get("ref") or "")

    return WebhookContext(
        delivery_id=delivery_id,
        event=event,
        environment_name=environment_name,
        repository=str(repository.get("full_name") or ""),
        repository_url=str(repository.get("html_url") or ""),
        ref=ref,
        target_branch=ref.removeprefix("refs/heads/"),
        before=str(payload.get("before") or ""),
        after=str(payload.get("after") or ""),
        commit_url=str(head_commit.get("url") or ""),
        compare_url=str(payload.get("compare") or ""),
        sender=str(sender.get("login") or ""),
        pusher=str(pusher.get("name") or ""),
        commit_message=str(head_commit.get("message") or ""),
        commit_timestamp=str(head_commit.get("timestamp") or ""),
        received_at=received_at,
        targets=tuple(targets),
        changed_files=tuple(changed_files),
        dry_run=dry_run,
    )


def infer_pull_request(context: WebhookContext) -> PullRequestInfo:
    match = MERGE_COMMIT_PATTERN.search(context.commit_message)
    if not match:
        return PullRequestInfo(is_merge=False, source="commit_message")

    number = int(match.group("number"))
    message_lines = [
        line.strip() for line in context.commit_message.splitlines()[1:] if line.strip()
    ]
    return PullRequestInfo(
        is_merge=True,
        source="commit_message",
        number=number,
        title=message_lines[0] if message_lines else "",
        url=_join_url(context.repository_url, f"pull/{number}"),
        head_ref=match.group("head"),
        base_ref=context.target_branch,
    )


async def resolve_pull_request(
    context: WebhookContext,
    *,
    token: str,
    timeout_seconds: float,
    api_version: str,
    logger: logging.Logger,
) -> PullRequestInfo:
    fallback = infer_pull_request(context)
    try:
        resolved = await resolve_merged_pull_request(
            context,
            token=token,
            timeout_seconds=timeout_seconds,
            api_version=api_version,
            logger=logger,
        )
    except PullRequestVerificationError as exc:
        logger.warning(
            "GitHub PR lookup failed delivery_id=%s error=%s; use commit fallback",
            context.delivery_id,
            exc,
        )
        return fallback
    return resolved or fallback


async def resolve_merged_pull_request(
    context: WebhookContext,
    *,
    token: str,
    timeout_seconds: float,
    api_version: str,
    logger: logging.Logger,
) -> PullRequestInfo | None:
    """Verify the PR merged by ``context.after`` without trusting its message alone."""
    if not token:
        raise PullRequestVerificationError("GITHUB_TOKEN is not configured")
    if "/" not in context.repository or not context.after:
        raise PullRequestVerificationError("push payload is missing repository or after")

    inferred = infer_pull_request(context)
    inferred_number = inferred.number if inferred.is_merge else None
    owner, repository = context.repository.split("/", 1)
    url = f"https://api.github.com/repos/{owner}/{repository}/commits/{context.after}/pulls"
    pull_url = (
        f"https://api.github.com/repos/{owner}/{repository}/pulls/{inferred_number}"
        if inferred_number is not None
        else ""
    )
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "User-Agent": "apexcamp-deploy-webhook-relay",
        "X-GitHub-Api-Version": api_version,
    }

    payload: list[Any] = []
    candidates: list[dict[str, Any]] = []

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_seconds), follow_redirects=True
        ) as client:
            for attempt in range(3):
                response = await client.get(url, headers=headers)
                response.raise_for_status()
                response_payload = response.json()
                if not isinstance(response_payload, list):
                    raise ValueError("GitHub PR lookup returned a non-list payload")
                payload = response_payload
                candidates = [
                    item
                    for item in payload
                    if isinstance(item, dict)
                    and item.get("merged_at")
                    and ((item.get("base") or {}).get("ref") == context.target_branch)
                    and (
                        item.get("merge_commit_sha") == context.after
                        or (
                            inferred_number is not None
                            and _optional_int(item.get("number")) == inferred_number
                        )
                    )
                ]
                if not candidates and not payload and pull_url:
                    pull_response = await client.get(pull_url, headers=headers)
                    pull_response.raise_for_status()
                    pull_payload = pull_response.json()
                    if not isinstance(pull_payload, dict):
                        raise ValueError("GitHub PR lookup returned a non-object payload")
                    payload = [pull_payload]
                    if (
                        pull_payload.get("merged_at")
                        and pull_payload.get("merge_commit_sha") == context.after
                        and (
                            (pull_payload.get("base") or {}).get("ref")
                            == context.target_branch
                        )
                        and _optional_int(pull_payload.get("number")) == inferred_number
                    ):
                        candidates = [pull_payload]
                if candidates:
                    break
                if attempt < 2:
                    await asyncio.sleep(0.25 * (2**attempt))
    except Exception as exc:  # noqa: BLE001
        raise PullRequestVerificationError(type(exc).__name__) from exc

    if not candidates:
        logger.info(
            "No merged PR matched delivery_id=%s after=%s inferred_pr=%s github_prs=%s",
            context.delivery_id,
            context.after,
            inferred_number,
            [
                {
                    "number": item.get("number"),
                    "merged_at": item.get("merged_at"),
                    "merge_commit_sha": item.get("merge_commit_sha"),
                    "base_ref": (item.get("base") or {}).get("ref"),
                }
                for item in payload
                if isinstance(item, dict)
            ],
        )
        return None

    selected = max(candidates, key=lambda item: str(item.get("merged_at") or ""))
    return PullRequestInfo(
        is_merge=True,
        source="github_api",
        number=_optional_int(selected.get("number")),
        title=str(selected.get("title") or ""),
        url=str(selected.get("html_url") or ""),
        head_ref=str((selected.get("head") or {}).get("ref") or ""),
        base_ref=str((selected.get("base") or {}).get("ref") or ""),
        merged_at=str(selected.get("merged_at") or ""),
        merged_by=str((selected.get("merged_by") or {}).get("login") or ""),
    )


def _optional_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _join_url(base: str, path: str) -> str:
    return f"{base.rstrip('/')}/{path.lstrip('/')}" if base else ""


async def send_email(
    config: EmailConfig,
    *,
    subject: str,
    text_body: str,
    html_body: str,
    delivery_id: str,
    phase: str,
    logger: logging.Logger,
) -> bool:
    if not config.enabled:
        return False
    if config.validation_error:
        logger.error(
            "Email configuration invalid delivery_id=%s phase=%s error=%s",
            delivery_id,
            phase,
            config.validation_error,
        )
        return False

    try:
        await asyncio.to_thread(
            _send_email_sync,
            config,
            _sanitize_subject(subject),
            text_body,
            html_body,
        )
    except Exception:  # noqa: BLE001
        logger.exception(
            "Email send failed delivery_id=%s phase=%s recipients=%s",
            delivery_id,
            phase,
            len(config.to_addresses),
        )
        return False

    logger.info(
        "Email sent delivery_id=%s phase=%s recipients=%s",
        delivery_id,
        phase,
        len(config.to_addresses),
    )
    return True


def _sanitize_subject(subject: str) -> str:
    return " ".join(subject.replace("\r", " ").replace("\n", " ").split())


def _send_email_sync(
    config: EmailConfig, subject: str, text_body: str, html_body: str
) -> None:
    message = EmailMessage()
    message["From"] = config.from_address
    message["To"] = ", ".join(config.to_addresses)
    message["Subject"] = subject
    message.set_content(text_body, charset="utf-8")
    message.add_alternative(html_body, subtype="html", charset="utf-8")

    tls_context = ssl.create_default_context()
    if config.tls_mode == "ssl":
        with smtplib.SMTP_SSL(
            config.smtp_host,
            config.smtp_port,
            local_hostname="localhost",
            timeout=config.timeout_seconds,
            context=tls_context,
        ) as client:
            _authenticate_and_send(client, config, message)
        return

    with smtplib.SMTP(
        config.smtp_host,
        config.smtp_port,
        local_hostname="localhost",
        timeout=config.timeout_seconds,
    ) as client:
        client.ehlo()
        if config.tls_mode == "starttls":
            client.starttls(context=tls_context)
            client.ehlo()
        _authenticate_and_send(client, config, message)


def _authenticate_and_send(
    client: smtplib.SMTP, config: EmailConfig, message: EmailMessage
) -> None:
    if config.smtp_username:
        client.login(config.smtp_username, config.smtp_password)
    client.send_message(message)


def render_received_email(
    context: WebhookContext, pr: PullRequestInfo, config: EmailConfig
) -> tuple[str, str, str]:
    targets = ", ".join(context.targets)
    subject = (
        f"[{context.environment_name}][已接收] GitHub {context.event} -> {targets}"
    )
    rows = _github_rows(context, pr, config)
    rows.extend(
        [
            ("部署目标", _code(targets)),
            ("变更文件", _changed_files_html(context.changed_files)),
        ]
    )
    buttons = _github_buttons(context, pr)
    badge = "已接收 · DRY_RUN" if context.dry_run else "已接收"
    lead = (
        "Relay 已接受本次事件；当前为 DRY_RUN，不会调用 Arcane WebHook。"
        if context.dry_run
        else "Relay 已接受本次事件，并已在后台触发 Arcane Git Sync。"
    )
    html_body = _base_email_html(
        title="GitHub WebHook 已接收",
        badge=badge,
        badge_color=ARCANE_COLORS["accent"],
        lead=lead,
        panels=[("事件信息", rows)],
        buttons=buttons,
        config=config,
    )
    text_body = _received_text(context, pr, config)
    return subject, text_body, html_body


def render_result_email(
    context: WebhookContext,
    pr: PullRequestInfo,
    results: list[SyncResult],
    config: EmailConfig,
) -> tuple[str, str, str]:
    summary, color = _result_summary(results)
    targets = ", ".join(context.targets)
    subject = f"[{context.environment_name}][{summary}] Arcane Git Sync -> {targets}"
    overview_rows = _github_rows(context, pr, config)
    overview_rows.append(("部署目标", _code(targets)))

    panels: list[tuple[str, list[tuple[str, str]]]] = [("关联事件", overview_rows)]
    for result in results:
        panels.append((f"目标: {result.target}", _sync_result_rows(result, config)))

    html_body = _base_email_html(
        title="Arcane Git Sync 结果",
        badge=summary,
        badge_color=color,
        lead=_result_lead(summary),
        panels=panels,
        buttons=_result_buttons(context, pr, config),
        config=config,
    )
    return subject, _result_text(context, pr, results, config, summary), html_body


def _github_rows(
    context: WebhookContext, pr: PullRequestInfo, config: EmailConfig
) -> list[tuple[str, str]]:
    pr_value = "未检测到 PR 合并"
    if pr.is_merge:
        number = f"PR #{pr.number}" if pr.number is not None else "已合并 PR"
        detail = _link(number, pr.url) if pr.url else _escape(number)
        if pr.title:
            detail += f'<br><span style="color:{ARCANE_COLORS["text_body"]}">{_escape(pr.title)}</span>'
        source_label = "GitHub API" if pr.source == "github_api" else "提交信息推断"
        detail += f'<br><span style="color:{ARCANE_COLORS["text_muted"]};font-size:12px">来源: {_escape(source_label)}</span>'
        pr_value = detail

    rows = [
        ("Delivery ID", _code(context.delivery_id)),
        ("事件", _escape(context.event)),
        ("仓库", _link(context.repository, context.repository_url)),
        ("PR 合并", pr_value),
        ("源分支", _code(pr.head_ref or "-")),
        ("目标分支", _code(context.target_branch or context.ref)),
        ("运行模式", _escape("DRY_RUN" if context.dry_run else "实际同步")),
        ("触发者", _escape(context.sender or context.pusher or "-")),
        ("Commit", _link(_short_sha(context.after), context.commit_url)),
        ("提交信息", _multiline(context.commit_message or "-")),
        ("提交时间", _escape(_format_timestamp(context.commit_timestamp, config))),
        ("接收时间", _escape(_format_datetime(context.received_at, config))),
    ]
    if pr.merged_by:
        rows.insert(5, ("PR 合并者", _escape(pr.merged_by)))
    if pr.merged_at:
        rows.insert(
            6, ("PR 合并时间", _escape(_format_timestamp(pr.merged_at, config)))
        )
    return rows


def _sync_result_rows(result: SyncResult, config: EmailConfig) -> list[tuple[str, str]]:
    status_labels = {
        "success": "同步成功",
        "failed": "同步失败",
        "skipped": "DRY_RUN 已跳过",
    }
    colors = {
        "success": ARCANE_COLORS["success"],
        "failed": ARCANE_COLORS["danger"],
        "skipped": ARCANE_COLORS["accent"],
    }
    rows = [
        (
            "状态",
            f'<strong style="color:{colors.get(result.status, ARCANE_COLORS["text_value"])}">{_escape(status_labels.get(result.status, result.status))}</strong>',
        ),
        (
            "HTTP 状态",
            _escape(str(result.status_code) if result.status_code is not None else "-"),
        ),
        ("Arcane success", _code(_json_value(result.arcane_success))),
        ("Arcane data", _code(_truncate(_json_value(result.data), 500))),
        ("耗时", _escape(f"{result.duration_seconds:.2f} 秒")),
        ("完成时间", _escape(_format_datetime(result.completed_at, config))),
    ]
    if result.error:
        rows.append(("错误", _multiline(_truncate(result.error, 500))))
    elif result.response_excerpt and not result.ok:
        rows.append(("响应", _multiline(_truncate(result.response_excerpt, 500))))
    return rows


def _result_summary(results: list[SyncResult]) -> tuple[str, str]:
    if results and all(result.status == "skipped" for result in results):
        return "同步已跳过", ARCANE_COLORS["accent"]
    success_count = sum(result.ok for result in results)
    if results and success_count == len(results):
        return "同步成功", ARCANE_COLORS["success"]
    if success_count:
        return "部分失败", ARCANE_COLORS["warning"]
    return "同步失败", ARCANE_COLORS["danger"]


def _result_lead(summary: str) -> str:
    return {
        "同步成功": "Arcane 已完成本次 Git Sync，所有目标均返回成功。",
        "部分失败": "部分 Arcane 目标未能确认同步成功，请检查下方结果与服务日志。",
        "同步已跳过": "Relay 处于 DRY_RUN 模式，本次没有调用 Arcane WebHook。",
    }.get(summary, "Arcane Git Sync 未能确认成功，请检查下方结果与服务日志。")


def _base_email_html(
    *,
    title: str,
    badge: str,
    badge_color: str,
    lead: str,
    panels: list[tuple[str, list[tuple[str, str]]]],
    buttons: list[tuple[str, str]],
    config: EmailConfig,
) -> str:
    logo = _logo_html(config.logo_url)
    panel_html = "".join(_panel_html(panel_title, rows) for panel_title, rows in panels)
    button_html = _buttons_html(buttons)
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="x-apple-disable-message-reformatting">
  <title>{_escape(title)}</title>
  <style>@media only screen and (max-width:620px){{.shell{{padding:24px 12px!important}}.card{{padding:22px!important}}.label{{display:block!important;width:auto!important;padding:0!important}}.value{{display:block!important;width:auto!important}}}}</style>
</head>
<body style="margin:0;padding:0;background:{ARCANE_COLORS["background"]};font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC','Microsoft YaHei',Arial,sans-serif;color:{ARCANE_COLORS["text_body"]}">
  <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="width:100%;background:{ARCANE_COLORS["background"]}">
    <tr><td class="shell" align="center" style="padding:40px 20px">
      <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="width:100%;max-width:600px;margin:0 auto">
        <tr><td align="center" style="padding:0 0 28px">{logo}</td></tr>
        <tr><td class="card" style="background:{ARCANE_COLORS["card"]};border:1px solid {ARCANE_COLORS["card_border"]};border-radius:14px;padding:32px">
          <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0"><tr>
            <td><h1 style="margin:0;color:{ARCANE_COLORS["text_primary"]};font-family:ui-monospace,SFMono-Regular,Menlo,Monaco,'Courier New',monospace;font-size:24px;line-height:32px">{_escape(title)}</h1></td>
            <td align="right" style="padding-left:12px"><span style="display:inline-block;border:1px solid {badge_color};border-radius:8px;padding:5px 10px;color:{badge_color};font-size:12px;font-weight:700;white-space:nowrap">{_escape(badge)}</span></td>
          </tr></table>
          <p style="margin:18px 0 0;color:{ARCANE_COLORS["text_body"]};font-size:16px;line-height:24px">{_escape(lead)}</p>
          {panel_html}
          {button_html}
        </td></tr>
        <tr><td align="center" style="padding:26px 12px 0;color:{ARCANE_COLORS["text_muted"]};font-size:12px;line-height:20px">由 Deploy Webhook Relay 自动发送 · {_escape(config.environment_name)}</td></tr>
      </table>
    </td></tr>
  </table>
</body>
</html>"""


def _panel_html(title: str, rows: list[tuple[str, str]]) -> str:
    row_html = "".join(
        f'<tr><td class="label" style="width:138px;padding:9px 14px 9px 0;border-bottom:1px solid {ARCANE_COLORS["card_border"]};vertical-align:top;color:{ARCANE_COLORS["text_muted"]};font-size:13px;font-weight:600">{_escape(label)}</td><td class="value" style="padding:9px 0;border-bottom:1px solid {ARCANE_COLORS["card_border"]};vertical-align:top;color:{ARCANE_COLORS["text_value"]};font-size:14px;line-height:21px;word-break:break-word">{value}</td></tr>'
        for label, value in rows
    )
    return f'<table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="width:100%;margin-top:22px;background:{ARCANE_COLORS["panel"]};border:1px solid {ARCANE_COLORS["card_border"]};border-radius:10px"><tr><td style="padding:18px 20px"><h2 style="margin:0 0 7px;color:{ARCANE_COLORS["text_primary"]};font-size:15px;line-height:22px">{_escape(title)}</h2><table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0">{row_html}</table></td></tr></table>'


def _buttons_html(buttons: list[tuple[str, str]]) -> str:
    valid_buttons = [(label, url) for label, url in buttons if _safe_url(url)]
    if not valid_buttons:
        return ""
    links = "".join(
        f'<a href="{_escape(url)}" style="display:inline-block;margin:8px 6px 0;padding:11px 17px;border-radius:9px;background:{ARCANE_COLORS["accent_button"]};color:#ffffff;text-decoration:none;font-size:13px;font-weight:700">{_escape(label)}</a>'
        for label, url in valid_buttons
    )
    return f'<div style="margin-top:20px;text-align:center">{links}</div>'


def _logo_html(url: str) -> str:
    if _safe_url(url):
        return f'<img src="{_escape(url)}" width="180" alt="Arcane" style="display:inline-block;width:180px;max-width:60%;height:auto;border:0;outline:none">'
    return f'<span style="color:{ARCANE_COLORS["accent"]};font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:24px;font-weight:800;letter-spacing:1px">ARCANE</span>'


def _github_buttons(
    context: WebhookContext, pr: PullRequestInfo
) -> list[tuple[str, str]]:
    return [
        ("查看 Pull Request", pr.url),
        ("查看 Commit", context.commit_url),
        ("比较变更", context.compare_url),
    ]


def _result_buttons(
    context: WebhookContext, pr: PullRequestInfo, config: EmailConfig
) -> list[tuple[str, str]]:
    return [
        ("打开 Arcane", config.arcane_app_url),
        ("查看 Pull Request", pr.url),
        ("查看 Commit", context.commit_url),
    ]


def _changed_files_html(files: tuple[str, ...], limit: int = 20) -> str:
    if not files:
        return "-"
    visible = files[:limit]
    rendered = "".join(
        f'<div style="margin:3px 0">{_code(path)}</div>' for path in visible
    )
    if len(files) > limit:
        rendered += f'<div style="margin-top:7px;color:{ARCANE_COLORS["text_muted"]}">另有 {len(files) - limit} 项未展示</div>'
    return rendered


def _received_text(
    context: WebhookContext, pr: PullRequestInfo, config: EmailConfig
) -> str:
    lines = [
        "GitHub WebHook 已接收",
        "",
        f"环境: {context.environment_name}",
        f"Delivery ID: {context.delivery_id}",
        f"事件: {context.event}",
        f"仓库: {context.repository}",
        f"PR 合并: {_pr_text(pr)}",
        f"PR 合并者: {pr.merged_by or '-'}",
        f"PR 合并时间: {_format_timestamp(pr.merged_at, config)}",
        f"源分支: {pr.head_ref or '-'}",
        f"目标分支: {context.target_branch or context.ref}",
        f"运行模式: {'DRY_RUN' if context.dry_run else '实际同步'}",
        f"触发者: {context.sender or context.pusher or '-'}",
        f"Commit: {context.after}",
        f"提交信息: {context.commit_message or '-'}",
        f"提交时间: {_format_timestamp(context.commit_timestamp, config)}",
        f"接收时间: {_format_datetime(context.received_at, config)}",
        f"部署目标: {', '.join(context.targets)}",
        "",
        "变更文件:",
    ]
    lines.extend(f"- {path}" for path in context.changed_files[:20])
    if len(context.changed_files) > 20:
        lines.append(f"- 另有 {len(context.changed_files) - 20} 项未展示")
    return "\n".join(lines)


def _result_text(
    context: WebhookContext,
    pr: PullRequestInfo,
    results: list[SyncResult],
    config: EmailConfig,
    summary: str,
) -> str:
    lines = [
        f"Arcane Git Sync 结果: {summary}",
        "",
        f"环境: {context.environment_name}",
        f"Delivery ID: {context.delivery_id}",
        f"仓库: {context.repository}",
        f"PR 合并: {_pr_text(pr)}",
        f"目标分支: {context.target_branch or context.ref}",
        f"Commit: {context.after}",
    ]
    for result in results:
        lines.extend(
            [
                "",
                f"目标: {result.target}",
                f"状态: {result.status}",
                f"HTTP 状态: {result.status_code if result.status_code is not None else '-'}",
                f"Arcane success: {_json_value(result.arcane_success)}",
                f"Arcane data: {_truncate(_json_value(result.data), 500)}",
                f"耗时: {result.duration_seconds:.2f} 秒",
                f"完成时间: {_format_datetime(result.completed_at, config)}",
            ]
        )
        if result.error:
            lines.append(f"错误: {_truncate(result.error, 500)}")
    return "\n".join(lines)


def _pr_text(pr: PullRequestInfo) -> str:
    if not pr.is_merge:
        return "未检测到"
    number = f"PR #{pr.number}" if pr.number is not None else "已合并 PR"
    source = "GitHub API" if pr.source == "github_api" else "提交信息推断"
    return f"是，{number}，{pr.title or '-'}（{source}）"


def _format_timestamp(value: str, config: EmailConfig) -> str:
    if not value:
        return "-"
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return _format_datetime(parsed, config)


def _format_datetime(value: datetime, config: EmailConfig) -> str:
    return value.astimezone(config.display_timezone).strftime("%Y-%m-%d %H:%M:%S %Z")


def _short_sha(value: str) -> str:
    return value[:12] if value else "-"


def _json_value(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _truncate(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[: limit - 3] + "..."


def _escape(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _multiline(value: str) -> str:
    return _escape(value).replace("\n", "<br>")


def _code(value: str) -> str:
    return f"<code style=\"font-family:ui-monospace,SFMono-Regular,Menlo,Monaco,'Courier New',monospace;color:{ARCANE_COLORS['text_value']};font-size:13px;word-break:break-all\">{_escape(value)}</code>"


def _link(label: str, url: str) -> str:
    if not _safe_url(url):
        return _escape(label or "-")
    return f'<a href="{_escape(url)}" style="color:{ARCANE_COLORS["accent"]};text-decoration:none">{_escape(label or url)}</a>'


def _safe_url(value: str) -> bool:
    try:
        parsed = urlparse(value)
    except ValueError:
        return False
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)
