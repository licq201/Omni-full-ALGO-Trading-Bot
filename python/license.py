"""
license.py — License validation for OMNI-ICT.

Default mode is local/offline so personal deployments never depend on the
external license server during startup. Set OMNI_LICENSE_MODE=remote only when
you explicitly want SaaS subscription validation.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

from i18n import msg as _msg

log = logging.getLogger("license")

LICENSE_KEY    = os.getenv("OMNI_LICENSE_KEY", "")
LICENSE_MODE   = os.getenv("OMNI_LICENSE_MODE", os.getenv("OMNI_AUTH_MODE", "local")).strip().lower()
LICENSE_SERVER = os.getenv("OMNI_LICENSE_SERVER", "https://omni-full-algo-trading-bot-production.up.railway.app")
CACHE_FILE     = Path(__file__).resolve().parent.parent / "logs" / "license_cache.json"
RECHECK_HOURS  = 24
OWNER_BYPASS   = "OWNER_BYPASS"
REMOTE_MODES   = {"remote", "server", "saas", "license_server"}


def is_remote_mode() -> bool:
    """Return True only when remote SaaS license validation is explicitly enabled."""
    return LICENSE_MODE in REMOTE_MODES


def _local_result() -> dict:
    return {
        "valid": True,
        "plan": "local",
        "mode": "local",
        "message": "Local authorization mode; remote license validation is disabled.",
    }


def _cache_read() -> dict:
    try:
        if CACHE_FILE.exists():
            return json.loads(CACHE_FILE.read_text())
    except Exception:
        pass
    return {}


def _cache_write(data: dict) -> None:
    try:
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        CACHE_FILE.write_text(json.dumps(data))
    except Exception:
        pass


def _validate_remote(key: str) -> dict:
    """Call license server. Returns {"valid": bool, "plan": str, "message": str}."""
    url = f"{LICENSE_SERVER}/validate?key={key}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "omni-ict/1.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        try:
            return json.loads(body)
        except Exception:
            return {"valid": False, "message": f"HTTP {e.code}: {body[:200]}"}
    except Exception as e:
        return {"valid": False, "message": f"Network error: {e}"}


def check(raise_on_fail: bool = True) -> dict:
    """
    Validate the license. Returns the license info dict.
    Raises SystemExit if invalid and raise_on_fail=True.
    Local mode passes without network access. Owner bypass key always passes.
    """
    if not is_remote_mode():
        log.info(_msg("license.local_mode"))
        return _local_result()

    if not LICENSE_KEY:
        message = _msg("license.remote_missing_key")
        log.error(message)
        if raise_on_fail:
            raise SystemExit(1)
        return {"valid": False, "message": message}

    if LICENSE_KEY == OWNER_BYPASS:
        log.debug(_msg("license.owner_bypass"))
        return {"valid": True, "plan": "owner", "message": "Owner bypass"}

    # Check cache first — avoid hammering the server
    cache = _cache_read()
    if cache.get("key") == LICENSE_KEY:
        checked_at = cache.get("checked_at", 0)
        age_hours = (time.time() - checked_at) / 3600
        if age_hours < RECHECK_HOURS and cache.get("valid"):
            log.debug(_msg("license.cache_hit", age_hours=age_hours, plan=cache.get("plan")))
            return cache

    log.info(_msg("license.remote_check", key_masked=LICENSE_KEY[:8] + "****"))
    result = _validate_remote(LICENSE_KEY)
    result["key"]        = LICENSE_KEY
    result["checked_at"] = time.time()

    if result.get("valid"):
        plan = result.get("plan", "unknown")
        exp  = result.get("expires_at", "")[:10]
        log.info(_msg("license.remote_valid", plan=plan, expires_at=exp))
        _cache_write(result)
    else:
        message = result.get("message", "License invalid")
        log.error(_msg("license.remote_failed", message=message))
        if raise_on_fail:
            raise SystemExit(1)

    return result


def plan() -> str:
    """Return current plan name without raising (for info display)."""
    if not is_remote_mode():
        return "local"
    if LICENSE_KEY == OWNER_BYPASS:
        return "owner"
    cache = _cache_read()
    return cache.get("plan", "unknown")
