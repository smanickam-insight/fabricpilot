"""
services/llm_remediation.py
===========================

LLM-generated remediation playbooks for the ADF + Fabric Monitor.

This replaces the hard-coded `PLAYBOOKS` dict in `services/remediation.py` as the
*primary* source of remediation steps. The static playbooks remain in the code
purely as a **fallback**, so the app degrades gracefully and never breaks.

Why this matters
----------------
The static playbooks are keyed on `ErrorCategory` only, so every "Databricks Error"
gets the identical five steps regardless of whether the real failure was a driver
OOM, a library conflict, or a revoked token. This module feeds the **actual error
text** (`detail_df["Error"]`, produced by `services.analytics.enrich`) to an LLM and
gets back steps grounded in that specific failure.

Design rules
------------
1. **Never break the app.** Any failure (no provider, timeout, quota, bad JSON)
   silently falls back to the caller's static playbook.
2. **Structured output only.** Strict JSON, validated before it is trusted.
3. **Cheap + cached.** One call per *error signature*, not per failure row, keyed
   on a hash of the error text so Streamlit reruns cost nothing.
4. **Read-only + redacted.** Secrets are masked via `services.redaction` before
   any text leaves the process. This module never calls an Azure/Fabric write API.

Configuration (`.streamlit/secrets.toml` or environment)
--------------------------------------------------------
    LLM_PROVIDER              = "azure_openai" | "openai" | "anthropic" | "none"

    # Azure OpenAI  (recommended — data stays in your tenant)
    AZURE_OPENAI_ENDPOINT     = "https://<res>.openai.azure.com/"
    AZURE_OPENAI_DEPLOYMENT   = "gpt-4o-mini"
    AZURE_OPENAI_API_VERSION  = "2024-10-21"
    AZURE_OPENAI_API_KEY      = "..."      # omit entirely to use Entra ID / MSI

    # OpenAI
    OPENAI_API_KEY            = "..."
    OPENAI_MODEL              = "gpt-4o-mini"

    # Anthropic
    ANTHROPIC_API_KEY         = "..."
    ANTHROPIC_MODEL           = "claude-sonnet-4-5"
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
from dataclasses import asdict, dataclass
from typing import Any

try:
    from services.redaction import redact_text
except ImportError:  # standalone / notebook use
    def redact_text(t):  # type: ignore
        return t

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 45        # seconds per LLM call
MAX_STEPS = 7
MIN_STEPS = 3
MAX_ERROR_CHARS = 1500      # truncate giant stack traces before sending
MAX_WORKERS = 4             # concurrent signatures


# --------------------------------------------------------------------------- #
# Config helpers
# --------------------------------------------------------------------------- #

def _env(key: str, default: str = "") -> str:
    """Read from st.secrets first (Streamlit), then os.environ. Never raises."""
    try:
        import streamlit as st
        if key in st.secrets:
            return str(st.secrets[key])
    except Exception:  # noqa: BLE001
        pass
    return os.getenv(key, default)


def provider_name() -> str:
    return _env("LLM_PROVIDER", "none").strip().lower()


def llm_enabled() -> bool:
    """True when a provider is configured with the minimum required settings."""
    p = provider_name()
    if p == "azure_openai":
        return bool(_env("AZURE_OPENAI_ENDPOINT") and _env("AZURE_OPENAI_DEPLOYMENT"))
    if p == "openai":
        return bool(_env("OPENAI_API_KEY"))
    if p == "anthropic":
        return bool(_env("ANTHROPIC_API_KEY"))
    return False


def provider_label() -> str:
    """Human-readable provider description for the UI caption."""
    p = provider_name()
    if p == "azure_openai":
        auth = "API key" if _env("AZURE_OPENAI_API_KEY") else "Entra ID (keyless)"
        return f"Azure OpenAI · {_env('AZURE_OPENAI_DEPLOYMENT', '?')} · {auth}"
    if p == "openai":
        return f"OpenAI · {_env('OPENAI_MODEL', 'gpt-4o-mini')}"
    if p == "anthropic":
        return f"Anthropic · {_env('ANTHROPIC_MODEL', 'claude-sonnet-4-5')}"
    return "No provider configured"


# --------------------------------------------------------------------------- #
# Result object
# --------------------------------------------------------------------------- #

@dataclass
class LLMPlaybook:
    root_cause: str           # one-line diagnosis grounded in the real error text
    confidence: str           # High | Medium | Low
    owner: str                # suggested owning team
    sla_days: int             # 1..7
    steps: list[str]          # ordered remediation steps
    preventive: list[str]     # longer-term "stop it happening again" actions
    validation: str           # how to prove the fix worked
    source: str = "llm"       # "llm" | "fallback"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# --------------------------------------------------------------------------- #
# Prompt
# --------------------------------------------------------------------------- #

SYSTEM_PROMPT = """You are a Senior Azure Data Platform SRE. Your specialisms are \
Azure Data Factory, Microsoft Fabric Data Factory pipelines, Fabric notebooks and \
Spark job definitions, Azure Databricks notebooks, Synapse, and ADLS Gen2.

You are given ONE recurring failure signature (the same pipeline/notebook + activity \
+ root-cause category failing on multiple distinct days), together with the real \
error text captured from Log Analytics, the ADF REST API, or the Fabric REST API.

Produce a concrete, engineer-ready remediation playbook.

HARD RULES
- Ground every step in the supplied error text. NEVER invent error codes, resource \
names, cluster IDs, table names, file paths or numbers that are not in the input.
- If the error text is vague, empty, or shows ***REDACTED***, say so plainly in \
root_cause, set confidence to "Low", and make step 1 a diagnostic step that collects \
the missing detail.
- Steps must be imperative and executable ("Open X → check Y → set Z"), never generic \
filler like "investigate the issue" or "contact the team".
- Name the correct UI surface for the platform in scope:
  · ADF        → ADF Studio Monitor → pipeline run → activity output; Log Analytics ADFActivityRun
  · Fabric     → Fabric Monitoring hub → item → job instance; Capacity Metrics app for CU/throttling
  · Databricks → Databricks run page → Spark UI → Stages/Executors; cluster event log
- For NOTEBOOK failures, focus on Spark-level causes: driver/executor OOM, shuffle \
spill, skew, broadcast limits, library/runtime version conflicts, expired mounts or \
tokens, non-idempotent writes that block retry.
- For PIPELINE/COPY failures, focus on activity config: timeout, retry policy, \
parallelCopies/DIUs, staging, linked-service auth, IR sizing and network path.
- Never suggest destructive actions (dropping tables, deleting pipelines, disabling \
auditing, widening RBAC beyond least privilege).
- Between 3 and 7 steps. Each step at most 200 characters.

Return ONLY a JSON object. No markdown fence, no commentary:
{
  "root_cause": "<=200 chars, one line, specific to THIS error text>",
  "confidence": "High" | "Medium" | "Low",
  "owner": "<owning team, e.g. Data Engineering / Platform>",
  "sla_days": <integer 1-7, smaller = more urgent>,
  "steps": ["step 1", "step 2", "step 3"],
  "preventive": ["prevention 1", "prevention 2"],
  "validation": "<how to confirm the fix worked>"
}"""


USER_TEMPLATE = """Recurring failure signature
---------------------------
Platform            : {platform}
Workload type       : {workload}
Pipeline / Item     : {pipeline}
Activity / Job      : {activity}
Activity type       : {activity_type}
Rule-based category : {category}
Rule-based hint     : {hint}
Occurrences         : {occurrences} failures across {days_affected} distinct day(s)
First seen          : {first_seen}
Last seen           : {last_seen}
Still failing today : {still_active}

Representative error message(s) captured from the platform:
\"\"\"
{error_text}
\"\"\"

Produce the remediation playbook JSON."""


def _clean_error_text(texts: list[str]) -> str:
    """De-duplicate, redact secrets, and truncate the raw error text."""
    seen: set[str] = set()
    out: list[str] = []
    for t in texts:
        if not t:
            continue
        t = str(t).strip()
        if not t:
            continue
        # Primary redaction: reuse the app's own hardened redactor.
        t = redact_text(t)
        # Belt-and-braces for key=value secrets the URL/JWT patterns miss.
        t = re.sub(r"(?i)\b(password|pwd|secret|sas|token|apikey|api_key|key)\s*=\s*[^;,\s]+",
                   r"\1=***", t)
        t = re.sub(r"(?i)\b(sig|sv|se|st)=[A-Za-z0-9%\-_]{8,}", r"\1=***", t)
        fingerprint = t[:160]
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        out.append(t)
        if sum(len(x) for x in out) > MAX_ERROR_CHARS:
            break
    joined = "\n---\n".join(out) or "(no error text captured)"
    return joined[:MAX_ERROR_CHARS]


# --------------------------------------------------------------------------- #
# Provider adapters
# --------------------------------------------------------------------------- #

def _call_azure_openai(system: str, user: str) -> str:
    from openai import AzureOpenAI

    endpoint = _env("AZURE_OPENAI_ENDPOINT")
    api_key = _env("AZURE_OPENAI_API_KEY")
    deployment = _env("AZURE_OPENAI_DEPLOYMENT", "gpt-4o-mini")
    version = _env("AZURE_OPENAI_API_VERSION", "2024-10-21")

    if api_key:
        client = AzureOpenAI(azure_endpoint=endpoint, api_key=api_key,
                             api_version=version, timeout=DEFAULT_TIMEOUT)
    else:
        # Keyless: reuse Entra ID. Works with Managed Identity when deployed,
        # or the developer's az-login / VS Code identity locally.
        from azure.identity import DefaultAzureCredential, get_bearer_token_provider
        token_provider = get_bearer_token_provider(
            DefaultAzureCredential(),
            "https://cognitiveservices.azure.com/.default",
        )
        client = AzureOpenAI(azure_endpoint=endpoint, api_version=version,
                             azure_ad_token_provider=token_provider,
                             timeout=DEFAULT_TIMEOUT)

    resp = client.chat.completions.create(
        model=deployment,
        temperature=0.2,
        max_tokens=900,
        response_format={"type": "json_object"},
        messages=[{"role": "system", "content": system},
                  {"role": "user", "content": user}],
    )
    return resp.choices[0].message.content


def _call_openai(system: str, user: str) -> str:
    from openai import OpenAI

    client = OpenAI(api_key=_env("OPENAI_API_KEY"), timeout=DEFAULT_TIMEOUT)
    resp = client.chat.completions.create(
        model=_env("OPENAI_MODEL", "gpt-4o-mini"),
        temperature=0.2,
        max_tokens=900,
        response_format={"type": "json_object"},
        messages=[{"role": "system", "content": system},
                  {"role": "user", "content": user}],
    )
    return resp.choices[0].message.content


def _call_anthropic(system: str, user: str) -> str:
    import anthropic

    client = anthropic.Anthropic(api_key=_env("ANTHROPIC_API_KEY"),
                                 timeout=DEFAULT_TIMEOUT)
    resp = client.messages.create(
        model=_env("ANTHROPIC_MODEL", "claude-sonnet-4-5"),
        max_tokens=900,
        temperature=0.2,
        system=system,
        messages=[{"role": "user",
                   "content": user + "\n\nRespond with the JSON object only."}],
    )
    return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")


_PROVIDERS = {
    "azure_openai": _call_azure_openai,
    "openai": _call_openai,
    "anthropic": _call_anthropic,
}


# --------------------------------------------------------------------------- #
# Parsing + validation
# --------------------------------------------------------------------------- #

def _extract_json(raw: str) -> dict:
    raw = (raw or "").strip()
    raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.MULTILINE).strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not match:
            raise
        return json.loads(match.group(0))


def _validate(d: dict, default_owner: str, default_sla: int) -> LLMPlaybook:
    steps = [str(s).strip() for s in d.get("steps", []) if str(s).strip()]
    if len(steps) < MIN_STEPS:
        raise ValueError(f"LLM returned only {len(steps)} step(s); need >= {MIN_STEPS}")
    steps = steps[:MAX_STEPS]

    confidence = str(d.get("confidence", "Medium")).strip().title()
    if confidence not in {"High", "Medium", "Low"}:
        confidence = "Medium"

    try:
        sla = int(d.get("sla_days", default_sla))
    except (TypeError, ValueError):
        sla = default_sla
    sla = max(1, min(7, sla))

    return LLMPlaybook(
        root_cause=str(d.get("root_cause", "")).strip()[:300] or "See activity output",
        confidence=confidence,
        owner=str(d.get("owner", "")).strip() or default_owner,
        sla_days=sla,
        steps=steps,
        preventive=[str(p).strip() for p in d.get("preventive", []) if str(p).strip()][:4],
        validation=str(d.get("validation", "")).strip()[:300],
        source="llm",
    )


# --------------------------------------------------------------------------- #
# Cache — survives Streamlit reruns, keyed on signature content
# --------------------------------------------------------------------------- #

_cache: dict[str, LLMPlaybook] = {}
_cache_lock = threading.Lock()
_stats = {"hits": 0, "calls": 0, "failures": 0}


def _cache_key(**kw) -> str:
    blob = json.dumps(kw, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:32]


def clear_cache() -> None:
    """Drop every cached playbook. Wired to the UI 'Regenerate' button."""
    with _cache_lock:
        _cache.clear()
        _stats.update(hits=0, calls=0, failures=0)


def cache_stats() -> dict:
    with _cache_lock:
        return dict(_stats, cached=len(_cache))


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #

def generate_playbook(
    *,
    pipeline: str,
    activity: str,
    category: str,
    error_messages: list[str],
    occurrences: int,
    days_affected: int,
    first_seen: Any,
    last_seen: Any,
    still_active: bool,
    hint: str = "",
    activity_type: str = "",
    is_notebook: bool = False,
    platform: str = "Azure Data Factory",
    fallback_owner: str = "Data Engineering",
    fallback_sla: int = 3,
    fallback_steps: list[str] | None = None,
) -> LLMPlaybook:
    """
    Return an LLM-generated playbook for ONE recurring failure signature.

    Falls back to the caller's static playbook on any problem, so the caller can
    treat this as total.
    """
    fallback = LLMPlaybook(
        root_cause=hint or "Review the activity output for the full error detail.",
        confidence="",
        owner=fallback_owner,
        sla_days=fallback_sla,
        steps=list(fallback_steps or
                   ["Open the full activity output and capture the exact error text."]),
        preventive=[],
        validation="",
        source="fallback",
    )

    if not llm_enabled():
        return fallback

    error_text = _clean_error_text(error_messages)
    key = _cache_key(p=pipeline, a=activity, c=category, e=error_text)

    with _cache_lock:
        if key in _cache:
            _stats["hits"] += 1
            return _cache[key]

    workload = ("Notebook / Spark job" if is_notebook
                else "Data pipeline activity")

    user = USER_TEMPLATE.format(
        platform=platform,
        workload=workload,
        pipeline=pipeline,
        activity=activity,
        activity_type=activity_type or "(unknown)",
        category=category,
        hint=hint or "(none)",
        occurrences=occurrences,
        days_affected=days_affected,
        first_seen=first_seen,
        last_seen=last_seen,
        still_active="Yes" if still_active else "No",
        error_text=error_text,
    )

    fn = _PROVIDERS.get(provider_name())
    if fn is None:
        return fallback

    for attempt in (1, 2):  # one retry, specifically for malformed JSON
        try:
            with _cache_lock:
                _stats["calls"] += 1
            raw = fn(SYSTEM_PROMPT, user)
            parsed = raw if isinstance(raw, dict) else _extract_json(raw)
            playbook = _validate(parsed, fallback_owner, fallback_sla)
            with _cache_lock:
                _cache[key] = playbook
            return playbook
        except Exception as exc:  # noqa: BLE001
            log.warning("LLM playbook attempt %s failed for %s | %s: %s",
                        attempt, pipeline, activity, exc)
            user += ("\n\nYour previous reply could not be parsed. "
                     "Return ONLY the JSON object described above.")

    # Negative-cache so a persistently failing signature is not retried on
    # every Streamlit rerun.
    with _cache_lock:
        _stats["failures"] += 1
        _cache[key] = fallback
    return fallback


def generate_playbooks_bulk(requests: list[dict],
                            max_workers: int = MAX_WORKERS) -> list[LLMPlaybook]:
    """Run generate_playbook() concurrently over a list of kwargs dicts."""
    if not requests:
        return []
    from concurrent.futures import ThreadPoolExecutor

    workers = max(1, min(max_workers, len(requests)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(lambda kw: generate_playbook(**kw), requests))
