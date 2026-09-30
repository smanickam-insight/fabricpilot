"""
services/alerting.py

Escalation for CRITICAL, persistent recurring failures (ADF **and** Fabric).

Rule (default): a remediation item is escalated when it is severity "Critical"
AND the same pipeline has failed on >= ESCALATION_MIN_DAYS distinct days
(default 5). For each such issue the app can:

  * send an EMAIL  — via Microsoft Graph (reusing the signed-in credential) or SMTP
  * post a TEAMS alert — via an Incoming Webhook / Power Automate "Workflows" HTTP URL

Design notes
------------
* Read-only w.r.t. Azure Data Factory and Fabric — this module only *notifies*;
  it never triggers/cancels/edits a pipeline.
* Streamlit reruns on every interaction, so we THROTTLE: each issue fires at most
  once per calendar day. Sent keys are persisted to a tiny JSON file so a restart
  within the same day won't re-spam.
* Nothing here raises to the UI: every send returns (ok: bool, detail: str).
* The remediation steps rendered below are whatever `RemediationItem.steps`
  holds — AI-generated when an LLM provider is configured, otherwise the static
  fallback playbook. The AI extras (validation / preventive / confidence) are
  included when present, and silently omitted when not, so this module works
  with either source.

It consumes the `RemediationItem` objects produced by services.remediation.
"""

from __future__ import annotations

import json
import smtplib
import ssl
import datetime as dt
from dataclasses import dataclass
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import requests

# Default: "more than 5 days" -> escalate at 5+ distinct affected days.
ESCALATION_MIN_DAYS = 5
GRAPH_SENDMAIL_SCOPE = "https://graph.microsoft.com/.default"
_STATE_FILE = Path.home() / ".adf_monitor_alert_state.json"
_TIMEOUT = 30


# --------------------------------------------------------------------------- #
# Selection
# --------------------------------------------------------------------------- #

def find_escalations(items, min_days: int = ESCALATION_MIN_DAYS, critical_only: bool = True):
    """
    From remediation items, return those that must be escalated.

    An item qualifies when days_affected >= min_days (default 5) and, if
    critical_only, its severity contains "Critical".
    """
    out = []
    for it in items or []:
        if it.days_affected < min_days:
            continue
        if critical_only and "Critical" not in it.severity:
            continue
        out.append(it)
    return out


def escalation_key(item, day: dt.date | None = None) -> str:
    """Stable per-issue-per-day key used for throttling."""
    day = day or dt.date.today()
    return f"{item.pipeline}|{item.activity}|{item.error_category}|{day.isoformat()}"


# --------------------------------------------------------------------------- #
# Throttle state (once-per-day per issue)
# --------------------------------------------------------------------------- #

def _load_state() -> dict:
    try:
        return json.loads(_STATE_FILE.read_text())
    except Exception:  # noqa: BLE001
        return {}


def _save_state(state: dict) -> None:
    try:
        _STATE_FILE.write_text(json.dumps(state, indent=2))
    except Exception:  # noqa: BLE001
        pass


def already_sent(item, channel: str) -> bool:
    state = _load_state()
    return state.get(escalation_key(item), {}).get(channel, False)


def mark_sent(item, channel: str) -> None:
    state = _load_state()
    entry = state.setdefault(escalation_key(item), {})
    entry[channel] = True
    entry["ts"] = dt.datetime.now(dt.timezone.utc).isoformat()
    _save_state(state)


# --------------------------------------------------------------------------- #
# Plan-source helpers  (tolerant of items built before the LLM upgrade)
# --------------------------------------------------------------------------- #

def _is_ai(item) -> bool:
    return getattr(item, "plan_source", "") == "llm"


def _plan_origin(item) -> str:
    return "AI-generated from the error text" if _is_ai(item) else "Static playbook"


def _confidence(item) -> str:
    return getattr(item, "confidence", "") or ""


def _validation(item) -> str:
    return getattr(item, "validation", "") or ""


def _preventive(item) -> list[str]:
    return list(getattr(item, "preventive", []) or [])


# --------------------------------------------------------------------------- #
# Message building
# --------------------------------------------------------------------------- #

def _subject(item) -> str:
    return (f"[DATA PLATFORM ALERT] Critical: {item.pipeline} failing "
            f"{item.days_affected} days ({item.error_category})")


def build_email_html(item, factory: str = "", extra_note: str = "") -> str:
    steps = "".join(f"<li>{s}</li>" for s in item.steps)

    ai_tag = ""
    if _is_ai(item):
        conf = f" · confidence: {_confidence(item)}" if _confidence(item) else ""
        ai_tag = (f'<span style="color:#666;font-size:12px;font-weight:normal">'
                  f'&nbsp;(AI-generated from the error text{conf})</span>')

    validation_html = ""
    if _validation(item):
        validation_html = (
            f'<p style="background:#e8f5e9;border-left:4px solid #2e7d32;padding:8px 12px;margin:10px 0">'
            f'<b>Validation:</b> {_validation(item)}</p>'
        )

    preventive_html = ""
    if _preventive(item):
        bullets = "".join(f"<li>{p}</li>" for p in _preventive(item))
        preventive_html = f"<p><b>Preventive actions:</b></p><ul>{bullets}</ul>"

    return f"""
    <div style="font-family:Segoe UI,Arial,sans-serif;font-size:14px;color:#1b1b1b">
      <h2 style="color:#b00020;margin:0 0 8px">🔴 Critical recurring failure</h2>
      <p><b>{item.pipeline}</b> / <code>{item.activity}</code> has failed on
         <b>{item.days_affected} distinct day(s)</b>
         ({item.occurrences} occurrences, {item.first_seen} → {item.last_seen}).</p>
      <table cellpadding="6" style="border-collapse:collapse;margin:10px 0">
        <tr><td><b>Scope</b></td><td>{factory or '—'}</td></tr>
        <tr><td><b>Root cause</b></td><td>{item.root_cause}</td></tr>
        <tr><td><b>Category</b></td><td>{item.error_category}</td></tr>
        <tr><td><b>Still failing today</b></td><td>{'Yes' if item.still_active_today else 'No'}</td></tr>
        <tr><td><b>Suggested owner</b></td><td>{item.owner}</td></tr>
        <tr><td><b>Target date</b></td><td>{item.target_date.isoformat()}</td></tr>
        <tr><td><b>Plan source</b></td><td>{_plan_origin(item)}</td></tr>
      </table>
      <p><b>Recommended remediation steps:</b>{ai_tag}</p>
      <ol>{steps}</ol>
      {validation_html}
      {preventive_html}
      <p style="color:#666">{extra_note}</p>
    </div>"""


def build_email_text(item, factory: str = "") -> str:
    steps = "\n".join(f"  {n}. {s}" for n, s in enumerate(item.steps, 1))

    extras = ""
    if _validation(item):
        extras += f"\nValidation: {_validation(item)}\n"
    if _preventive(item):
        extras += "\nPreventive actions:\n" + "\n".join(f"  - {p}" for p in _preventive(item)) + "\n"

    conf = f" (confidence: {_confidence(item)})" if _confidence(item) else ""

    return (
        f"CRITICAL recurring failure\n"
        f"{item.pipeline} / {item.activity} failed on {item.days_affected} distinct day(s) "
        f"({item.occurrences} occurrences, {item.first_seen} -> {item.last_seen}).\n\n"
        f"Scope: {factory or '-'}\n"
        f"Root cause: {item.root_cause}\n"
        f"Category: {item.error_category}\n"
        f"Still failing today: {'Yes' if item.still_active_today else 'No'}\n"
        f"Suggested owner: {item.owner}\n"
        f"Target date: {item.target_date.isoformat()}\n"
        f"Plan source: {_plan_origin(item)}{conf}\n\n"
        f"Remediation steps:\n{steps}\n{extras}"
    )


def build_teams_card(item, factory: str = "") -> dict:
    """
    Adaptive Card wrapped for a Teams Incoming Webhook / Power Automate 'Workflows'
    HTTP trigger. Works with the classic connector MessageCard consumers too, but
    Adaptive Cards are the forward-looking format.
    """
    facts = [
        {"title": "Pipeline", "value": item.pipeline},
        {"title": "Activity", "value": item.activity},
        {"title": "Scope", "value": factory or "—"},
        {"title": "Root cause", "value": item.root_cause},
        {"title": "Category", "value": item.error_category},
        {"title": "Days affected", "value": str(item.days_affected)},
        {"title": "Occurrences", "value": str(item.occurrences)},
        {"title": "Still failing today", "value": "Yes" if item.still_active_today else "No"},
        {"title": "Owner", "value": item.owner},
        {"title": "Target date", "value": item.target_date.isoformat()},
        {"title": "Plan source", "value": _plan_origin(item)},
    ]
    if _confidence(item):
        facts.append({"title": "Confidence", "value": _confidence(item)})

    body = [
        {"type": "TextBlock", "size": "Large", "weight": "Bolder", "color": "Attention",
         "text": "🔴 Critical recurring data-platform failure"},
        {"type": "TextBlock", "wrap": True,
         "text": f"**{item.pipeline} / {item.activity}** failed on "
                 f"**{item.days_affected} distinct day(s)** "
                 f"({item.first_seen} → {item.last_seen})."},
        {"type": "FactSet", "facts": facts},
        {"type": "TextBlock", "weight": "Bolder", "text": "Remediation steps:", "wrap": True},
        {"type": "TextBlock", "wrap": True,
         "text": "\n\n".join(f"{n}. {s}" for n, s in enumerate(item.steps, 1))},
    ]

    if _validation(item):
        body.append({"type": "TextBlock", "wrap": True, "color": "Good",
                     "text": f"**Validation:** {_validation(item)}"})

    if _preventive(item):
        body.append({"type": "TextBlock", "weight": "Bolder", "wrap": True,
                     "text": "Preventive actions:"})
        body.append({"type": "TextBlock", "wrap": True, "isSubtle": True,
                     "text": "\n\n".join(f"- {p}" for p in _preventive(item))})

    card = {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.4",
        "body": body,
    }

    # Envelope accepted by both Incoming Webhook and Power Automate Workflows.
    return {
        "type": "message",
        "attachments": [
            {"contentType": "application/vnd.microsoft.card.adaptive", "content": card}
        ],
    }


# --------------------------------------------------------------------------- #
# Senders  (each returns (ok, detail); never raises)
# --------------------------------------------------------------------------- #

def post_teams(webhook_url: str, item, factory: str = ""):
    if not webhook_url:
        return False, "No Teams webhook URL configured."
    try:
        resp = requests.post(webhook_url, json=build_teams_card(item, factory), timeout=_TIMEOUT)
        # Incoming Webhook returns 200 "1"; Power Automate returns 202.
        if resp.status_code in (200, 202):
            return True, f"Teams alert sent ({resp.status_code})."
        return False, f"Teams webhook returned {resp.status_code}: {resp.text[:200]}"
    except Exception as exc:  # noqa: BLE001
        return False, f"Teams webhook error: {exc}"


def send_email_graph(credential, sender_upn: str, recipients: list[str], item, factory: str = ""):
    """
    Send via Microsoft Graph using the already signed-in credential.
    Requires the token to carry the 'Mail.Send' delegated permission.

    `sender_upn` is used only in the /users/{id}/sendMail path; for the signed-in
    user you can pass their UPN or use 'me' by leaving it blank.
    """
    if not recipients:
        return False, "No email recipients configured."
    try:
        token = credential.get_token(GRAPH_SENDMAIL_SCOPE).token
    except Exception as exc:  # noqa: BLE001
        return False, f"Could not get Graph token (is Mail.Send consented?): {exc}"

    endpoint = ("https://graph.microsoft.com/v1.0/me/sendMail"
                if not sender_upn else
                f"https://graph.microsoft.com/v1.0/users/{sender_upn}/sendMail")

    body = {
        "message": {
            "subject": _subject(item),
            "body": {"contentType": "HTML", "content": build_email_html(item, factory)},
            "toRecipients": [{"emailAddress": {"address": r.strip()}} for r in recipients if r.strip()],
        },
        "saveToSentItems": True,
    }
    try:
        resp = requests.post(
            endpoint,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json=body, timeout=_TIMEOUT,
        )
        if resp.status_code in (200, 202):
            return True, "Email sent via Microsoft Graph."
        return False, f"Graph sendMail returned {resp.status_code}: {resp.text[:200]}"
    except Exception as exc:  # noqa: BLE001
        return False, f"Graph sendMail error: {exc}"


def send_email_smtp(host: str, port: int, username: str, password: str,
                    sender: str, recipients: list[str], item, factory: str = "",
                    use_tls: bool = True):
    """SMTP fallback (e.g. company relay / Office365 SMTP AUTH)."""
    if not recipients:
        return False, "No email recipients configured."
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = _subject(item)
        msg["From"] = sender
        msg["To"] = ", ".join(recipients)
        msg.attach(MIMEText(build_email_text(item, factory), "plain"))
        msg.attach(MIMEText(build_email_html(item, factory), "html"))

        if use_tls:
            ctx = ssl.create_default_context()
            with smtplib.SMTP(host, port, timeout=_TIMEOUT) as server:
                server.starttls(context=ctx)
                if username:
                    server.login(username, password)
                server.sendmail(sender, recipients, msg.as_string())
        else:
            with smtplib.SMTP(host, port, timeout=_TIMEOUT) as server:
                if username:
                    server.login(username, password)
                server.sendmail(sender, recipients, msg.as_string())
        return True, "Email sent via SMTP."
    except Exception as exc:  # noqa: BLE001
        return False, f"SMTP error: {exc}"


# --------------------------------------------------------------------------- #
# Orchestrator
# --------------------------------------------------------------------------- #

@dataclass
class AlertResult:
    pipeline: str
    activity: str
    email_ok: bool
    email_detail: str
    teams_ok: bool
    teams_detail: str
    skipped: bool = False


def dispatch_escalations(
    items,
    *,
    min_days: int = ESCALATION_MIN_DAYS,
    factory: str = "",
    # Teams
    teams_webhook: str = "",
    # Email — choose ONE path
    credential=None, sender_upn: str = "", recipients: list[str] | None = None,
    smtp: dict | None = None,
    # Behaviour
    respect_throttle: bool = True,
    force: bool = False,
) -> list[AlertResult]:
    """
    Escalate every qualifying item. Returns one AlertResult per issue.

    - Email path: pass `credential` (+ recipients) to use Graph, OR pass an `smtp`
      dict {host, port, username, password, sender, use_tls} to use SMTP.
    - `force=True` ignores the once-per-day throttle (useful for a manual "Send now").
    """
    recipients = recipients or []
    results: list[AlertResult] = []

    for it in find_escalations(items, min_days=min_days, critical_only=True):
        # Throttle
        if respect_throttle and not force and already_sent(it, "any"):
            results.append(AlertResult(it.pipeline, it.activity, False, "Already alerted today",
                                       False, "Already alerted today", skipped=True))
            continue

        # Email
        email_ok, email_detail = False, "Email not configured"
        if smtp:
            email_ok, email_detail = send_email_smtp(
                smtp.get("host", ""), int(smtp.get("port", 587)),
                smtp.get("username", ""), smtp.get("password", ""),
                smtp.get("sender", ""), recipients, it, factory,
                use_tls=smtp.get("use_tls", True),
            )
        elif credential is not None:
            email_ok, email_detail = send_email_graph(credential, sender_upn, recipients, it, factory)

        # Teams
        teams_ok, teams_detail = post_teams(teams_webhook, it, factory)

        if email_ok or teams_ok:
            mark_sent(it, "any")
            if email_ok:
                mark_sent(it, "email")
            if teams_ok:
                mark_sent(it, "teams")

        results.append(AlertResult(it.pipeline, it.activity,
                                   email_ok, email_detail, teams_ok, teams_detail))

    return results
