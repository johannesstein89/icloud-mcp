"""iCloud Reminders management via pyicloud (CloudKit backend)."""

import os
import logging
from datetime import datetime
from typing import List, Dict, Any, Optional
from fastmcp import Context
from .auth import require_auth

logger = logging.getLogger(__name__)

COOKIE_DIR = os.environ.get("PYICLOUD_COOKIE_DIR", "/tmp/pyicloud_cookies")

# Module-level cache so the same authenticated API instance is reused
# within the process lifetime (important for the 2FA verification flow).
_api_cache: Dict[str, Any] = {}


def _apple_password(fallback: str) -> str:
    """Return the Apple ID password for pyicloud.

    pyicloud requires the real Apple ID password, not an app-specific one.
    Set ICLOUD_PASSWORD to override; otherwise the provided credential is used.
    """
    return os.environ.get("ICLOUD_PASSWORD") or fallback


def _get_api(email: str, password: str):
    """Return an authenticated PyiCloudService, raising ValueError if 2FA is needed."""
    from pyicloud import PyiCloudService

    os.makedirs(COOKIE_DIR, exist_ok=True)
    apple_pw = _apple_password(password)

    cached = _api_cache.get(email)
    if cached is not None and not cached.requires_2fa and not cached.requires_2sa:
        return cached

    api = PyiCloudService(email, apple_pw, cookie_directory=COOKIE_DIR)
    _api_cache[email] = api

    if api.requires_2fa:
        raise ValueError(
            "2FA_REQUIRED: iCloud requires two-factor authentication. "
            "A 6-digit code was sent to your trusted Apple device. "
            "Call reminders_verify_2fa with that code."
        )
    if api.requires_2sa:
        raise ValueError(
            "2SA_REQUIRED: iCloud requires two-step verification. "
            "A code was sent to your trusted device or phone. "
            "Call reminders_verify_2fa with that code."
        )

    return api


def _format_reminder(reminder: dict, list_name: str = "") -> Dict[str, Any]:
    due = None
    due_date = reminder.get("dueDate")
    if reminder.get("hasDueDate") and due_date and isinstance(due_date, list):
        try:
            year, month, day = due_date[0], due_date[1], due_date[2]
            hour = due_date[3] if len(due_date) > 3 else 0
            minute = due_date[4] if len(due_date) > 4 else 0
            due = datetime(year, month, day, hour, minute).isoformat()
        except Exception:
            pass

    completed_at = None
    completed_date = reminder.get("completedDate")
    if reminder.get("completed") and completed_date and isinstance(completed_date, list):
        try:
            completed_at = datetime(
                completed_date[0], completed_date[1], completed_date[2]
            ).isoformat()
        except Exception:
            pass

    return {
        "id": reminder.get("guid", ""),
        "summary": reminder.get("title", ""),
        "description": reminder.get("description", ""),
        "status": "COMPLETED" if reminder.get("completed") else "NEEDS-ACTION",
        "due": due,
        "completed_at": completed_at,
        "priority": reminder.get("priority"),
        "list": list_name,
    }


async def list_reminder_lists(context: Context) -> List[Dict[str, Any]]:
    """List all iCloud reminder lists (collections)."""
    email, password = require_auth(context)
    api = _get_api(email, password)

    return [
        {"id": col.get("guid", name), "name": name}
        for name, col in api.reminders.collections.items()
    ]


async def list_reminders(
    context: Context,
    list_id: Optional[str] = None,
    include_completed: bool = False,
) -> List[Dict[str, Any]]:
    """List reminders, optionally filtered to a specific list."""
    email, password = require_auth(context)
    api = _get_api(email, password)

    if list_id:
        target_names = [
            name for name, col in api.reminders.collections.items()
            if name == list_id or col.get("guid") == list_id
        ]
        if not target_names:
            raise ValueError(f"Reminder list '{list_id}' not found.")
    else:
        target_names = list(api.reminders.collections.keys())

    result = []
    for name in target_names:
        for reminder in api.reminders.get(name):
            if not include_completed and reminder.get("completed"):
                continue
            result.append(_format_reminder(reminder, name))

    return result


async def create_reminder(
    context: Context,
    summary: str,
    list_id: Optional[str] = None,
    due: Optional[str] = None,
    description: Optional[str] = None,
    priority: Optional[int] = None,
) -> Dict[str, Any]:
    """Create a new iCloud reminder."""
    email, password = require_auth(context)
    api = _get_api(email, password)

    collection_name = None
    if list_id:
        for name, col in api.reminders.collections.items():
            if name == list_id or col.get("guid") == list_id:
                collection_name = name
                break
        if collection_name is None:
            raise ValueError(f"Reminder list '{list_id}' not found.")

    due_date = datetime.fromisoformat(due) if due else None

    api.reminders.post(
        subject=summary,
        description=description or "",
        collection=collection_name,
        due_date=due_date,
    )

    default_list = collection_name or (
        next(iter(api.reminders.collections), "") if api.reminders.collections else ""
    )
    return {
        "summary": summary,
        "description": description or "",
        "status": "NEEDS-ACTION",
        "due": due or "",
        "list": default_list,
    }


def _find_reminder(api, reminder_id: str):
    """Return (reminder_dict, list_name) or raise ValueError."""
    for name in api.reminders.collections:
        for r in api.reminders.get(name):
            if r.get("guid") == reminder_id:
                return dict(r), name
    raise ValueError(f"Reminder '{reminder_id}' not found.")


async def delete_reminder(context: Context, reminder_id: str) -> Dict[str, str]:
    """Delete an iCloud reminder by GUID."""
    email, password = require_auth(context)
    api = _get_api(email, password)

    reminder, _ = _find_reminder(api, reminder_id)

    params = dict(api.reminders._params)
    params.update({"clientVersion": "4.0", "lang": "en-us"})

    resp = api.reminders._session.delete(
        api.reminders._service_root + "/rd/reminder",
        params=params,
        json={"Reminders": {"guid": reminder_id, "pGuid": reminder.get("pGuid", "")}},
    )

    if resp.status_code not in (200, 204):
        raise ValueError(f"Delete failed: HTTP {resp.status_code} — {resp.text[:200]}")

    return {"status": "success", "message": "Reminder deleted."}


async def complete_reminder(context: Context, reminder_id: str) -> Dict[str, Any]:
    """Mark an iCloud reminder as completed."""
    email, password = require_auth(context)
    api = _get_api(email, password)

    reminder, list_name = _find_reminder(api, reminder_id)

    now = datetime.now()
    reminder["completed"] = True
    reminder["completedDate"] = [now.year, now.month, now.day, now.hour, now.minute]

    params = dict(api.reminders._params)
    params.update({"clientVersion": "4.0", "lang": "en-us"})

    resp = api.reminders._session.post(
        api.reminders._service_root + "/rd/reminders",
        params=params,
        json={"Reminders": [reminder]},
    )

    if resp.status_code not in (200, 204):
        raise ValueError(f"Complete failed: HTTP {resp.status_code} — {resp.text[:200]}")

    return {
        "id": reminder_id,
        "status": "COMPLETED",
        "completed_at": now.isoformat(),
        "list": list_name,
    }


async def verify_2fa(context: Context, code: str) -> Dict[str, str]:
    """Submit a 2FA/2SA code to complete iCloud authentication."""
    email, password = require_auth(context)
    from pyicloud import PyiCloudService

    os.makedirs(COOKIE_DIR, exist_ok=True)
    apple_pw = _apple_password(password)

    # Reuse cached instance so we verify against the same auth challenge.
    api = _api_cache.get(email)
    if api is None:
        api = PyiCloudService(email, apple_pw, cookie_directory=COOKIE_DIR)
        _api_cache[email] = api

    if not api.requires_2fa and not api.requires_2sa:
        return {"status": "ok", "message": "Already authenticated — no 2FA needed."}

    if api.requires_2fa:
        if not api.validate_2fa_code(code):
            raise ValueError("2FA code invalid or expired. Try again.")
        api.trust_session()
        return {"status": "success", "message": "2FA verified. Session trusted."}

    # 2SA path
    if not api.validate_2sa_code(code):
        raise ValueError("2SA code invalid or expired. Try again.")
    api.trust_session()
    return {"status": "success", "message": "2-step verification complete. Session trusted."}
