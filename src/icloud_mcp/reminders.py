"""CalDAV tools for iCloud Reminders management."""

import logging
import caldav
from datetime import datetime
from typing import List, Dict, Any, Optional
from urllib.parse import urlparse
from fastmcp import Context
from .auth import require_auth
from .config import config

logger = logging.getLogger(__name__)


def _get_caldav_client(email: str, password: str) -> caldav.DAVClient:
    logger.debug("Creating CalDAV client → %s (user=%s)", config.CALDAV_SERVER, email)
    return caldav.DAVClient(
        url=config.CALDAV_SERVER,
        username=email,
        password=password
    )


def _parse_todo(todo, email: str, password: str, calendar_name: str = "") -> Optional[Dict[str, Any]]:
    todo_url = str(todo.url)
    try:
        # Data is usually included in the REPORT response — no load() needed
        vtodo = todo.vobject_instance.vtodo
        logger.debug("  todo parsed inline: %s", todo_url)
    except Exception as e:
        logger.debug("  inline parse failed (%s), retrying via sub-server client: %s", e, todo_url)
        # Fallback: iCloud uses numbered sub-servers (p72-caldav.icloud.com)
        # that differ from the discovery host, so we need a URL-specific client
        try:
            parsed_url = urlparse(todo_url)
            base_url = f"{parsed_url.scheme}://{parsed_url.netloc}"
            logger.debug("  loading from sub-server: %s", base_url)
            sub_client = caldav.DAVClient(url=base_url, username=email, password=password)
            todo = caldav.CalendarObjectResource(client=sub_client, url=todo_url)
            todo.load()
            vtodo = todo.vobject_instance.vtodo
            logger.debug("  sub-server load OK: %s", todo_url)
        except Exception as e2:
            logger.warning("  failed to parse todo %s — skipping. Error: %s", todo_url, e2)
            return None

    summary = str(vtodo.summary.value) if hasattr(vtodo, 'summary') and vtodo.summary else ""
    logger.debug("  parsed todo '%s' from list '%s'", summary, calendar_name)

    due = None
    if hasattr(vtodo, 'due') and vtodo.due:
        try:
            val = vtodo.due.value
            due = val.isoformat() if hasattr(val, 'isoformat') else str(val)
        except Exception as e:
            logger.debug("  could not parse due date: %s", e)

    completed_at = None
    if hasattr(vtodo, 'completed') and vtodo.completed:
        try:
            val = vtodo.completed.value
            completed_at = val.isoformat() if hasattr(val, 'isoformat') else str(val)
        except Exception as e:
            logger.debug("  could not parse completed date: %s", e)

    priority = None
    if hasattr(vtodo, 'priority') and vtodo.priority:
        try:
            priority = int(vtodo.priority.value)
        except Exception as e:
            logger.debug("  could not parse priority: %s", e)

    return {
        "id": str(todo.url),
        "summary": summary,
        "description": str(vtodo.description.value) if hasattr(vtodo, 'description') and vtodo.description else "",
        "status": str(vtodo.status.value) if hasattr(vtodo, 'status') and vtodo.status else "NEEDS-ACTION",
        "due": due,
        "completed_at": completed_at,
        "priority": priority,
        "list": calendar_name,
        "url": str(todo.url)
    }


def _supports_vtodo(calendar: caldav.Calendar) -> bool:
    """Return True if this calendar supports VTODO (i.e. it's a Reminders list)."""
    try:
        components = calendar.get_supported_components()
        supports = "VTODO" in components
        logger.debug("  calendar '%s' supported components=%s → vtodo=%s", calendar.name, components, supports)
        return supports
    except Exception as e:
        logger.debug("  could not get supported components for '%s': %s", calendar.name, e)
        return False


def _is_completed(parsed: Dict[str, Any]) -> bool:
    """Return True if a parsed todo is completed."""
    return parsed.get("status") == "COMPLETED" or bool(parsed.get("completed_at"))


def _fetch_todos(cal: caldav.Calendar) -> list:
    """Fetch VTODOs from an iCloud reminders list, working around iCloud quirks.

    iCloud returns HTTP 500 for the filtered todos REPORT that the caldav
    library builds when include_completed=False — it adds prop-filters on
    COMPLETED/STATUS that iCloud's CalDAV server rejects. We always request the
    plain VTODO comp-filter (include_completed=True) and filter completed items
    client-side. If even that fails, fall back to enumerating objects directly.
    """
    try:
        todos = cal.todos(include_completed=True)
        logger.debug("  _fetch_todos: todos(include_completed=True) returned %d", len(todos))
        return todos
    except Exception as e:
        logger.warning("  _fetch_todos: todos() failed (%s); falling back to objects()", e)
        try:
            objects = list(cal.objects())
            logger.debug("  _fetch_todos: objects() fallback returned %d", len(objects))
            return objects
        except Exception as e2:
            logger.error("  _fetch_todos: objects() fallback also failed — %s", e2)
            return []


async def list_reminder_lists(context: Context) -> List[Dict[str, Any]]:
    """List all reminder lists (CalDAV calendars that support VTODO)."""
    email, password = require_auth(context)
    logger.debug("list_reminder_lists: connecting as %s", email)
    client = _get_caldav_client(email, password)

    try:
        principal = client.principal()
        logger.debug("list_reminder_lists: principal URL = %s", principal.url)
    except Exception as e:
        logger.error("list_reminder_lists: failed to get principal — %s", e)
        raise

    try:
        all_calendars = principal.calendars()
        logger.debug("list_reminder_lists: found %d total calendars", len(all_calendars))
        for cal in all_calendars:
            logger.debug("  - '%s' @ %s", cal.name, cal.url)
    except Exception as e:
        logger.error("list_reminder_lists: failed to list calendars — %s", e)
        raise

    result = []
    for cal in all_calendars:
        if _supports_vtodo(cal):
            result.append({"id": str(cal.url), "name": cal.name or "Unnamed List", "url": str(cal.url)})

    logger.debug("list_reminder_lists: returning %d reminder list(s)", len(result))
    return result


async def list_reminders(
    context: Context,
    list_id: Optional[str] = None,
    include_completed: bool = False
) -> List[Dict[str, Any]]:
    """List reminders from a specific list or all reminder lists."""
    email, password = require_auth(context)
    logger.debug("list_reminders: list_id=%s include_completed=%s", list_id, include_completed)
    client = _get_caldav_client(email, password)
    principal = client.principal()

    if list_id:
        logger.debug("list_reminders: searching specific list %s", list_id)
        calendars_to_search = [caldav.Calendar(client=client, url=list_id)]
    else:
        all_calendars = principal.calendars()
        logger.debug("list_reminders: scanning %d calendars for VTODO support", len(all_calendars))
        calendars_to_search = [cal for cal in all_calendars if _supports_vtodo(cal)]
        logger.debug("list_reminders: %d VTODO-capable calendar(s) found", len(calendars_to_search))

    result = []
    for cal in calendars_to_search:
        logger.debug("list_reminders: querying todos in '%s' @ %s", cal.name, cal.url)
        todos = _fetch_todos(cal)
        logger.debug("list_reminders: got %d todo(s) from '%s'", len(todos), cal.name)
        for todo in todos:
            parsed = _parse_todo(todo, email, password, cal.name or "")
            if not parsed:
                continue
            # iCloud can't filter completed items server-side, so do it here
            if not include_completed and _is_completed(parsed):
                logger.debug("  skipping completed todo '%s'", parsed.get("summary"))
                continue
            result.append(parsed)

    logger.debug("list_reminders: returning %d reminder(s) total", len(result))
    return result


async def create_reminder(
    context: Context,
    summary: str,
    list_id: Optional[str] = None,
    due: Optional[str] = None,
    description: Optional[str] = None,
    priority: Optional[int] = None
) -> Dict[str, Any]:
    """Create a new reminder (VTODO)."""
    email, password = require_auth(context)
    logger.debug("create_reminder: summary='%s' list_id=%s due=%s", summary, list_id, due)
    client = _get_caldav_client(email, password)
    principal = client.principal()

    if list_id:
        calendar = caldav.Calendar(client=client, url=list_id)
        logger.debug("create_reminder: using specified list %s", list_id)
    else:
        all_calendars = principal.calendars()
        calendar = None
        for cal in all_calendars:
            # Use the supported-components check, not cal.todos() — iCloud
            # returns HTTP 500 on the filtered todos REPORT.
            if _supports_vtodo(cal):
                calendar = cal
                logger.debug("create_reminder: auto-selected list '%s'", cal.name)
                break
            else:
                logger.debug("create_reminder: skipping non-VTODO list '%s'", cal.name)
                continue
        if calendar is None:
            raise ValueError("No reminder list found. Please specify a list_id.")

    now = datetime.now()
    uid = f"{int(now.timestamp())}{now.microsecond}@icloud-mcp"

    ical_lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//iCloud MCP//EN",
        "CALSCALE:GREGORIAN",
        "BEGIN:VTODO",
        f"UID:{uid}",
        f"DTSTAMP:{now.strftime('%Y%m%dT%H%M%SZ')}",
        f"CREATED:{now.strftime('%Y%m%dT%H%M%SZ')}",
        f"LAST-MODIFIED:{now.strftime('%Y%m%dT%H%M%SZ')}",
        f"SUMMARY:{summary}",
        "STATUS:NEEDS-ACTION",
        "SEQUENCE:0",
    ]

    if due:
        due_dt = datetime.fromisoformat(due)
        ical_lines.append(f"DUE:{due_dt.strftime('%Y%m%dT%H%M%S')}")

    if description:
        desc_escaped = (
            description
            .replace('\\', '\\\\')
            .replace(',', '\\,')
            .replace(';', '\\;')
            .replace('\n', '\\n')
        )
        ical_lines.append(f"DESCRIPTION:{desc_escaped}")

    if priority is not None:
        ical_lines.append(f"PRIORITY:{priority}")

    ical_lines += ["END:VTODO", "END:VCALENDAR"]
    ical_data = "\r\n".join(ical_lines)

    logger.debug("create_reminder: posting to '%s'\n%s", calendar.name, ical_data)
    try:
        todo = calendar.add_todo(ical_data)
        logger.debug("create_reminder: created → %s", todo.url)
    except Exception as e:
        logger.error("create_reminder: failed — %s", e)
        raise ValueError(f"Failed to create reminder in list '{calendar.name}': {str(e)}")

    return {
        "id": str(todo.url),
        "summary": summary,
        "status": "NEEDS-ACTION",
        "due": due or "",
        "description": description or "",
        "priority": priority,
        "list": calendar.name,
        "url": str(todo.url)
    }


async def delete_reminder(context: Context, reminder_id: str) -> Dict[str, str]:
    """Delete a reminder."""
    email, password = require_auth(context)
    logger.debug("delete_reminder: %s", reminder_id)

    parsed = urlparse(reminder_id)
    base_url = f"{parsed.scheme}://{parsed.netloc}"
    client = caldav.DAVClient(url=base_url, username=email, password=password)

    try:
        todo = caldav.CalendarObjectResource(client=client, url=reminder_id)
        todo.delete()
        logger.debug("delete_reminder: deleted OK")
    except Exception as e:
        logger.error("delete_reminder: failed — %s", e)
        raise

    return {"status": "success", "message": f"Reminder {reminder_id} deleted"}


async def complete_reminder(context: Context, reminder_id: str) -> Dict[str, Any]:
    """Mark a reminder as completed."""
    email, password = require_auth(context)
    logger.debug("complete_reminder: %s", reminder_id)

    parsed = urlparse(reminder_id)
    base_url = f"{parsed.scheme}://{parsed.netloc}"
    client = caldav.DAVClient(url=base_url, username=email, password=password)

    try:
        todo = caldav.CalendarObjectResource(client=client, url=reminder_id)
        todo.load()
        logger.debug("complete_reminder: loaded todo OK")
    except Exception as e:
        logger.error("complete_reminder: failed to load todo — %s", e)
        raise

    vtodo = todo.vobject_instance.vtodo
    now = datetime.now()

    if hasattr(vtodo, 'status'):
        vtodo.status.value = 'COMPLETED'
    else:
        vtodo.add('status').value = 'COMPLETED'

    if hasattr(vtodo, 'completed'):
        vtodo.completed.value = now
    else:
        vtodo.add('completed').value = now

    updated_ical = todo.vobject_instance.serialize()
    logger.debug("complete_reminder: PUTting updated iCal to %s", reminder_id)
    try:
        client.put(reminder_id, updated_ical, {"Content-Type": "text/calendar; charset=utf-8"})
        logger.debug("complete_reminder: PUT OK")
    except Exception as e:
        logger.error("complete_reminder: PUT failed — %s", e)
        raise

    return {
        "id": reminder_id,
        "status": "COMPLETED",
        "completed_at": now.isoformat(),
        "url": reminder_id
    }
