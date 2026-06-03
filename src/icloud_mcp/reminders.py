"""CalDAV tools for iCloud Reminders management."""

import caldav
from datetime import datetime
from typing import List, Dict, Any, Optional
from urllib.parse import urlparse
from fastmcp import Context
from .auth import require_auth
from .config import config


def _get_caldav_client(email: str, password: str) -> caldav.DAVClient:
    return caldav.DAVClient(
        url=config.CALDAV_SERVER,
        username=email,
        password=password
    )


def _parse_todo(todo, email: str, password: str, calendar_name: str = "") -> Optional[Dict[str, Any]]:
    try:
        # Data is usually included in the REPORT response — no load() needed
        vtodo = todo.vobject_instance.vtodo
    except Exception:
        # Fallback: load via URL-specific client (iCloud uses numbered sub-servers
        # like p72-caldav.icloud.com which differ from the discovery host)
        try:
            todo_url = str(todo.url)
            parsed_url = urlparse(todo_url)
            base_url = f"{parsed_url.scheme}://{parsed_url.netloc}"
            sub_client = caldav.DAVClient(url=base_url, username=email, password=password)
            todo = caldav.CalendarObjectResource(client=sub_client, url=todo_url)
            todo.load()
            vtodo = todo.vobject_instance.vtodo
        except Exception:
            return None

    due = None
    if hasattr(vtodo, 'due') and vtodo.due:
        try:
            val = vtodo.due.value
            due = val.isoformat() if hasattr(val, 'isoformat') else str(val)
        except Exception:
            pass

    completed_at = None
    if hasattr(vtodo, 'completed') and vtodo.completed:
        try:
            val = vtodo.completed.value
            completed_at = val.isoformat() if hasattr(val, 'isoformat') else str(val)
        except Exception:
            pass

    priority = None
    if hasattr(vtodo, 'priority') and vtodo.priority:
        try:
            priority = int(vtodo.priority.value)
        except Exception:
            pass

    return {
        "id": str(todo.url),
        "summary": str(vtodo.summary.value) if hasattr(vtodo, 'summary') and vtodo.summary else "",
        "description": str(vtodo.description.value) if hasattr(vtodo, 'description') and vtodo.description else "",
        "status": str(vtodo.status.value) if hasattr(vtodo, 'status') and vtodo.status else "NEEDS-ACTION",
        "due": due,
        "completed_at": completed_at,
        "priority": priority,
        "list": calendar_name,
        "url": str(todo.url)
    }


async def list_reminder_lists(context: Context) -> List[Dict[str, Any]]:
    """
    List all reminder lists (CalDAV calendars that support VTODO).

    Returns:
        List of reminder lists with id, name, and URL
    """
    email, password = require_auth(context)
    client = _get_caldav_client(email, password)
    principal = client.principal()
    calendars = principal.calendars()

    result = []
    for cal in calendars:
        result.append({
            "id": str(cal.url),
            "name": cal.name or "Unnamed List",
            "url": str(cal.url)
        })

    return result


async def list_reminders(
    context: Context,
    list_id: Optional[str] = None,
    include_completed: bool = False
) -> List[Dict[str, Any]]:
    """
    List reminders from a specific list or all reminder lists.

    Args:
        list_id: Reminder list URL/ID (optional, defaults to reminder-named calendars)
        include_completed: Include completed reminders (default: False)

    Returns:
        List of reminders with details
    """
    email, password = require_auth(context)
    client = _get_caldav_client(email, password)
    principal = client.principal()

    if list_id:
        calendars_to_search = [caldav.Calendar(client=client, url=list_id)]
    else:
        all_calendars = principal.calendars()
        reminder_cals = [
            cal for cal in all_calendars
            if cal.name and ('reminder' in cal.name.lower() or '⚠' in cal.name)
        ]
        calendars_to_search = reminder_cals if reminder_cals else all_calendars

    result = []
    for cal in calendars_to_search:
        try:
            todos = cal.todos(include_completed=include_completed)
            for todo in todos:
                parsed = _parse_todo(todo, email, password, cal.name or "")
                if parsed:
                    result.append(parsed)
        except Exception:
            continue

    return result


async def create_reminder(
    context: Context,
    summary: str,
    list_id: Optional[str] = None,
    due: Optional[str] = None,
    description: Optional[str] = None,
    priority: Optional[int] = None
) -> Dict[str, Any]:
    """
    Create a new reminder (VTODO).

    Args:
        summary: Reminder title
        list_id: Target reminder list URL/ID (optional)
        due: Due date/time in ISO format, e.g. "2025-12-01T10:00:00" (optional)
        description: Reminder notes (optional)
        priority: Priority 1-9 (1=highest, 5=medium, 9=lowest) (optional)

    Returns:
        Created reminder details
    """
    email, password = require_auth(context)
    client = _get_caldav_client(email, password)
    principal = client.principal()

    if list_id:
        calendar = caldav.Calendar(client=client, url=list_id)
    else:
        all_calendars = principal.calendars()
        reminder_cals = [
            cal for cal in all_calendars
            if cal.name and ('reminder' in cal.name.lower() or '⚠' in cal.name)
        ]
        if not reminder_cals:
            raise ValueError("No reminder list found. Please specify a list_id.")
        calendar = reminder_cals[0]

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

    try:
        todo = calendar.add_todo(ical_data)
    except Exception as e:
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
    """
    Delete a reminder.

    Args:
        reminder_id: Reminder URL/ID to delete

    Returns:
        Confirmation message
    """
    email, password = require_auth(context)

    parsed = urlparse(reminder_id)
    base_url = f"{parsed.scheme}://{parsed.netloc}"
    client = caldav.DAVClient(url=base_url, username=email, password=password)

    todo = caldav.CalendarObjectResource(client=client, url=reminder_id)
    todo.delete()

    return {"status": "success", "message": f"Reminder {reminder_id} deleted"}


async def complete_reminder(context: Context, reminder_id: str) -> Dict[str, Any]:
    """
    Mark a reminder as completed.

    Args:
        reminder_id: Reminder URL/ID to mark as complete

    Returns:
        Updated reminder details
    """
    email, password = require_auth(context)

    parsed = urlparse(reminder_id)
    base_url = f"{parsed.scheme}://{parsed.netloc}"
    client = caldav.DAVClient(url=base_url, username=email, password=password)

    todo = caldav.CalendarObjectResource(client=client, url=reminder_id)
    todo.load()

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
    client.put(reminder_id, updated_ical, {"Content-Type": "text/calendar; charset=utf-8"})

    return {
        "id": reminder_id,
        "status": "COMPLETED",
        "completed_at": now.isoformat(),
        "url": reminder_id
    }
