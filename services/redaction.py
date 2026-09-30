"""
Redaction helpers for Log Analytics / ADF / Fabric query results.

ADF and Fabric error/output payloads routinely carry credentials in the clear:
SAS tokens in blob URLs, Authorization headers on Web activities, linked service
connection strings, account keys, and bearer tokens. Rendering those verbatim
is fine locally, but exposes them on screen during demos/screenshots.

redact() walks a JSON-like structure and masks:
- values whose **key** looks sensitive (password, key, token, secret, ...)
- query-string secrets inside URLs (SAS: sig/sig=, se=, st=, ...)
- values that *look* like a bearer token or connection string regardless of key
"""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

MASK = "***REDACTED***"

_SENSITIVE_KEY_PATTERNS = (
    "password", "passwd", "pwd",
    "secret", "clientsecret",
    "token", "accesstoken", "refreshtoken", "sastoken", "bearer",
    "accountkey", "apikey", "api_key", "subscriptionkey", "primarykey",
    "secondarykey", "sharedaccesskey", "authkey",
    "connectionstring", "connstr",
    "authorization", "auth", "credential", "credentials",
    "certificate", "privatekey", "thumbprint",
    "sig", "signature",
)

_SENSITIVE_QUERY_KEYS = {"sig", "signature", "sas", "se", "st", "ske", "skt",
                         "sks", "skoid", "code", "access_token", "token"}

_VALUE_PATTERNS = (
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]*", re.I),  # JWT
    re.compile(r"\bBearer\s+[A-Za-z0-9._-]{20,}", re.I),                               # bearer header
    re.compile(r"AccountKey\s*=\s*[^;]+", re.I),                                       # storage conn str
    re.compile(r"(Password|Pwd)\s*=\s*[^;]+", re.I),                                   # SQL conn str
    re.compile(r"SharedAccessSignature\s*=\s*[^;]+", re.I),
)


def _key_is_sensitive(key: str) -> bool:
    k = re.sub(r"[^a-z0-9]", "", str(key).lower())
    return any(pat in k for pat in _SENSITIVE_KEY_PATTERNS)


def redact_url(url: str) -> str:
    """Strip secret query parameters (SAS signatures, auth codes) from a URL."""
    try:
        parts = urlsplit(url)
        if not parts.query:
            return url
        kept = [
            (k, MASK if k.lower() in _SENSITIVE_QUERY_KEYS else v)
            for k, v in parse_qsl(parts.query, keep_blank_values=True)
        ]
        return urlunsplit(parts._replace(query=urlencode(kept, safe="*")))
    except Exception:  # noqa: BLE001
        return MASK


def _redact_scalar(value):
    if not isinstance(value, str):
        return value
    if value.startswith(("http://", "https://", "abfss://", "wasbs://")):
        return redact_url(value)
    for pat in _VALUE_PATTERNS:
        if pat.search(value):
            return pat.sub(MASK, value)
    return value


def redact(obj, _depth: int = 0):
    """Return a deep copy of obj with sensitive material masked."""
    if _depth > 40:  # guard against pathological nesting
        return obj
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            out[k] = MASK if _key_is_sensitive(k) else redact(v, _depth + 1)
        return out
    if isinstance(obj, list):
        return [redact(item, _depth + 1) for item in obj]
    return _redact_scalar(obj)


def redact_text(text: str) -> str:
    """Mask secrets in a free-form string (e.g. an ARM/KQL/Fabric error body)."""
    if not isinstance(text, str):
        return text
    result = text
    for pat in _VALUE_PATTERNS:
        result = pat.sub(MASK, result)
    result = re.sub(r"([?&](?:sig|signature|code|access_token)=)[^&\s\"']+",
                    rf"\1{MASK}", result, flags=re.I)
    return result
