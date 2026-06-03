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


def _supports_vtodo(cal) -> bool:
    """Return True if the calendar collection supports VTODO components."""
    try:
        props = cal.get_properties([
            "{urn:ietf:params:xml:ns:caldav}supported-calendar-component-set"
        ])
        for value in props.values():
            # value is an XML Element — iterate children for comp name="VTODO"
            # (str(element) gives "<Element ... at 0x...>", NOT its content)
            try:
                for child in value:
                    if child.get("name") == "VTODO":
                        return True
            except Exception:
                pass
            # Fallback: XML serialization
            try:
                from xml.etree.ElementTree import tostring
                if b"VTODO" in tostring(value):
                    return True
            except Exception:
                pass
        return False
    except Exception:
        return True


def _parse_todo(todo, email: str, password: str, calendar_name: str = "") -> Optional[Dict[str, Any]]:
    try:
        # Data is usually included in the REPORT response
        vtodo = todo.vobject_instance.vtodo
    except Exception:
        # Fallback: iCloud serves todos from numbered sub-servers
        # (e.g. p72-caldav.icloud.com), load with URL-specific client
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
    if hasattr(vtodo, "due") and vtodo.due:
        try:
            val = vtodo.due.value
            due = val.isoformat() if hasattr(val, "isoformat") else str(val)
        except Exception:
            pass

    completed_at = None
    if hasattr(vtodo, "completed") and vtodo.completed:
        try:
            val = vtodo.completed.value
            completed_at = val.isoformat() if hasattr(val, "isoformat") else str(val)
        except Exception:
            pass

    priority = None
    if hasattr(vtodo, "priority") and vtodo.priority:
        try:
            priority = int(vtodo.priority.value)
        except Exception:
            pass

    return {
        "id": str(todo.url),
        "summary": str(vtodo.summary.value) if hasattr(vtodo, "summary") and vtodo.summary else "",
        "description": str(vtodo.description.value) if hasattr(vtodo, "description") and vtodo.description else "",
        "status": str(vtodo.status.value) if hasattr(vtodo, "status") and vtodo.status else "NEEDS-ACTION",
        "due": due,
        "completed_at": completed_at,
        "priority": priority,
        "list": calendar_name,
        "url": str(todo.url),
    }


async def list_reminder_lists(context: Context) -> List[Dict[str, Any]]:
    """
    List all reminder lists (CalDAV collections that support VTODO).

    Returns:
        List of reminder lists with id, name, and URL
    """
    email, password = require_auth(context)
    client = _get_caldav_client(email, password)
    principal = client.principal()

    result = []
    for cal in principal.calendars():
        if _supports_vtodo(cal):
            result.append({
                "id": str(cal.url),
                "name": cal.name or "Unnamed List",
                "url": str(cal.url),
            })

    return result


async def list_reminders(
    context: Context,
    list_id: Optional[str] = None,
    include_completed: bool = False,
) -> List[Dict[str, Any]]:
    """
    List reminders from a specific list or all reminder lists.

    Args:
        list_id: Reminder list URL/ID (optional)
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
        all_cals = principal.calendars()
        vtodo_cals = [cal for cal in all_cals if _supports_vtodo(cal)]
        # If _supports_vtodo filtered everything out (false negative), try all
        calendars_to_search = vtodo_cals if vtodo_cals else all_cals

    result = []
    for cal in calendars_to_search:
        try:
            # Fetch ALL todos without a server-side COMPLETED filter — iCloud
            # does not reliably support that filter. Filter client-side instead.
            todos = cal.todos(include_completed=True)
            for todo in todos:
                parsed = _parse_todo(todo, email, password, cal.name or "")
                if parsed is None:
                    continue
                if not include_completed and parsed.get("status") == "COMPLETED":
                    continue
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
    priority: Optional[int] = None,
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
        calendar = next(
            (cal for cal in principal.calendars() if _supports_vtodo(cal)),
            None,
        )
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
            .replace("\\", "\\\\")
            .replace(",", "\\,")
            .replace(";", "\\;")
            .replace("\n", "\\n")
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
        "url": str(todo.url),
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

    if hasattr(vtodo, "status"):
        vtodo.status.value = "COMPLETED"
    else:
        vtodo.add("status").value = "COMPLETED"

    if hasattr(vtodo, "completed"):
        vtodo.completed.value = now
    else:
        vtodo.add("completed").value = now

    updated_ical = todo.vobject_instance.serialize()
    client.put(reminder_id, updated_ical, {"Content-Type": "text/calendar; charset=utf-8"})

    return {
        "id": reminder_id,
        "status": "COMPLETED",
        "completed_at": now.isoformat(),
        "url": reminder_id,
    }


async def debug_reminders(context: Context) -> Dict[str, Any]:
    """
    Diagnostic tool: raw CalDAV discovery and VTODO fetch test.

    Returns detailed information about all CalDAV collections, their
    supported component types, and the raw REPORT results per list.
    Useful for diagnosing why reminders are not visible.
    """
    import requests as _requests
    from xml.etree import ElementTree as ET

    email, password = require_auth(context)
    auth = (email, password)
    result: Dict[str, Any] = {"steps": {}}

    def _propfind(url, depth, body):
        return _requests.request(
            "PROPFIND", url,
            headers={"Content-Type": "application/xml; charset=utf-8",
                     "Depth": str(depth)},
            data=body, auth=auth, allow_redirects=True, timeout=30
        )

    # ── 1. Principal ──────────────────────────────────────────────
    r = _propfind(config.CALDAV_SERVER, 0, b"""<?xml version="1.0"?>
<d:propfind xmlns:d="DAV:">
  <d:prop><d:current-user-principal/></d:prop>
</d:propfind>""")
    root = ET.fromstring(r.text)
    href = root.find(".//{DAV:}current-user-principal/{DAV:}href")
    if href is None:
        return {"error": "Cannot find principal", "raw": r.text[:500]}

    p = urlparse(r.url)
    principal_path = href.text
    if principal_path.startswith("http"):
        principal_url = principal_path
    else:
        from urllib.parse import urlunparse
        principal_url = urlunparse((p.scheme, p.netloc, principal_path, "", "", ""))
    result["steps"]["principal"] = principal_url

    # ── 2. Calendar home set ──────────────────────────────────────
    r = _propfind(principal_url, 0, b"""<?xml version="1.0"?>
<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
  <d:prop><c:calendar-home-set/></d:prop>
</d:propfind>""")
    root = ET.fromstring(r.text)
    href = root.find(".//{urn:ietf:params:xml:ns:caldav}calendar-home-set/{DAV:}href")
    if href is None:
        return {"error": "Cannot find calendar-home-set", "raw": r.text[:500]}

    home_path = href.text
    p2 = urlparse(r.url)
    if home_path.startswith("http"):
        home_url = home_path
    else:
        from urllib.parse import urlunparse
        home_url = urlunparse((p2.scheme, p2.netloc, home_path, "", "", ""))
    result["steps"]["calendar_home"] = home_url

    # ── 3. List all collections ───────────────────────────────────
    r = _propfind(home_url, 1, b"""<?xml version="1.0"?>
<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
  <d:prop>
    <d:displayname/>
    <d:resourcetype/>
    <c:supported-calendar-component-set/>
  </d:prop>
</d:propfind>""")
    root = ET.fromstring(r.text)
    collections = []
    for resp in root.findall("{DAV:}response"):
        href_el = resp.find("{DAV:}href")
        col_url = href_el.text if href_el is not None else ""
        name_el = resp.find(".//{DAV:}displayname")
        name = name_el.text if name_el is not None else ""
        comp_set = resp.find(".//{urn:ietf:params:xml:ns:caldav}supported-calendar-component-set")
        components = [c.get("name") for c in comp_set] if comp_set is not None else []
        is_cal = resp.find(".//{urn:ietf:params:xml:ns:caldav}calendar") is not None
        collections.append({
            "name": name, "url": col_url,
            "components": components, "is_calendar": is_cal
        })
    result["steps"]["collections"] = collections

    # ── 4. Fetch VTODOs from each VTODO-capable collection ───────
    from urllib.parse import urlunparse as _uu
    p3 = urlparse(home_url)
    base = f"{p3.scheme}://{p3.netloc}"

    vtodo_cols = [c for c in collections if "VTODO" in c["components"]]
    if not vtodo_cols:
        vtodo_cols = [c for c in collections if c["is_calendar"]]

    report_body = b"""<?xml version="1.0" encoding="utf-8"?>
<c:calendar-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
  <d:prop><d:getetag/><c:calendar-data/></d:prop>
  <c:filter>
    <c:comp-filter name="VCALENDAR">
      <c:comp-filter name="VTODO"/>
    </c:comp-filter>
  </c:filter>
</c:calendar-query>"""

    todos_by_list = []
    for col in vtodo_cols:
        col_url = col["url"]
        if not col_url.startswith("http"):
            col_url = base + col_url

        r = _requests.request(
            "REPORT", col_url,
            headers={"Content-Type": "application/xml; charset=utf-8", "Depth": "1"},
            data=report_body, auth=auth, allow_redirects=True, timeout=30
        )
        todos = []
        if r.status_code in (200, 207):
            try:
                rroot = ET.fromstring(r.text)
                for rresp in rroot.findall("{DAV:}response"):
                    cal_data = rresp.find(
                        ".//{urn:ietf:params:xml:ns:caldav}calendar-data")
                    if cal_data is not None and cal_data.text:
                        fields = {}
                        in_todo = False
                        for line in cal_data.text.splitlines():
                            if line.strip() == "BEGIN:VTODO":
                                in_todo = True
                            elif line.strip() == "END:VTODO":
                                break
                            elif in_todo and ":" in line:
                                k, _, v = line.partition(":")
                                fields[k.split(";")[0]] = v
                        todos.append({
                            "summary": fields.get("SUMMARY", ""),
                            "status": fields.get("STATUS", "NEEDS-ACTION"),
                            "due": fields.get("DUE", ""),
                            "priority": fields.get("PRIORITY", ""),
                            "description": fields.get("DESCRIPTION", ""),
                            "uid": fields.get("UID", ""),
                        })
            except Exception as e:
                todos_by_list.append({
                    "list": col["name"], "url": col_url,
                    "report_status": r.status_code,
                    "error": str(e), "todos": []
                })
                continue
        todos_by_list.append({
            "list": col["name"], "url": col_url,
            "report_status": r.status_code,
            "todos_count": len(todos),
            "todos": todos
        })

    result["steps"]["todos_by_list"] = todos_by_list
    result["total_todos"] = sum(x.get("todos_count", 0) for x in todos_by_list)

    if result["total_todos"] == 0:
        result["diagnosis"] = (
            "No reminders found via CalDAV. Most likely cause: "
            "Reminders are not synced to iCloud. "
            "Check: iPhone → Settings → [Name] → iCloud → Reminders → ON"
        )

    return result


async def find_reminder_path(context: Context) -> Dict[str, Any]:
    """
    Targeted path discovery for iCloud Reminders.

    Tests /reminders/ as a sibling to /calendars/ (the common iCloud
    structure), does a PROPFIND on the user root, and tries other known
    path patterns. Use this when reminders_debug returns empty lists.
    """
    import requests as _requests

    email, password = require_auth(context)
    auth = (email, password)
    headers = {"Content-Type": "application/xml; charset=utf-8"}
    result: Dict[str, Any] = {}

    PROPFIND_BODY = b"""<?xml version="1.0"?>
<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
  <d:prop>
    <d:displayname/>
    <d:resourcetype/>
    <c:supported-calendar-component-set/>
  </d:prop>
</d:propfind>"""

    REPORT_BODY = b"""<?xml version="1.0" encoding="utf-8"?>
<c:calendar-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
  <d:prop><d:getetag/><c:calendar-data/></d:prop>
  <c:filter>
    <c:comp-filter name="VCALENDAR">
      <c:comp-filter name="VTODO"/>
    </c:comp-filter>
  </c:filter>
</c:calendar-query>"""

    from xml.etree import ElementTree as ET
    from urllib.parse import urlunparse

    def _propfind(url, depth=1):
        return _requests.request(
            "PROPFIND", url,
            headers={**headers, "Depth": str(depth)},
            data=PROPFIND_BODY, auth=auth, allow_redirects=True, timeout=30
        )

    def _report(url):
        return _requests.request(
            "REPORT", url,
            headers={**headers, "Depth": "1"},
            data=REPORT_BODY, auth=auth, allow_redirects=True, timeout=30
        )

    def _parse_cols(xml_text, base_url):
        p = urlparse(base_url)
        cols = []
        for resp in ET.fromstring(xml_text).findall("{DAV:}response"):
            href = resp.findtext("{DAV:}href", "")
            name = resp.findtext(".//{DAV:}displayname", "")
            comp_set = resp.find(".//{urn:ietf:params:xml:ns:caldav}supported-calendar-component-set")
            components = [c.get("name") for c in comp_set] if comp_set is not None else []
            is_cal = resp.find(".//{urn:ietf:params:xml:ns:caldav}calendar") is not None
            abs_url = href if href.startswith("http") else urlunparse((p.scheme, p.netloc, href, "", "", ""))
            cols.append({"name": name, "url": abs_url, "components": components, "is_calendar": is_cal})
        return cols

    def _parse_todos(xml_text):
        todos = []
        for resp in ET.fromstring(xml_text).findall("{DAV:}response"):
            data = resp.findtext(".//{urn:ietf:params:xml:ns:caldav}calendar-data", "")
            if not data:
                continue
            fields = {}
            in_todo = False
            for line in data.splitlines():
                if line.strip() == "BEGIN:VTODO":
                    in_todo = True
                elif line.strip() == "END:VTODO":
                    break
                elif in_todo and ":" in line:
                    k, _, v = line.partition(":")
                    fields[k.split(";")[0]] = v
            if fields:
                todos.append({
                    "summary": fields.get("SUMMARY", ""),
                    "status": fields.get("STATUS", "NEEDS-ACTION"),
                    "due": fields.get("DUE", ""),
                    "priority": fields.get("PRIORITY", ""),
                })
        return todos

    # ── Discover principal and calendar home ──────────────────────
    r = _requests.request(
        "PROPFIND", config.CALDAV_SERVER,
        headers={**headers, "Depth": "0"},
        data=b"""<?xml version="1.0"?>
<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
  <d:prop>
    <d:current-user-principal/>
    <c:calendar-home-set/>
  </d:prop>
</d:propfind>""",
        auth=auth, allow_redirects=True, timeout=30
    )
    root = ET.fromstring(r.text)

    principal_href = root.findtext(".//{DAV:}current-user-principal/{DAV:}href", "")
    p0 = urlparse(r.url)
    principal_url = principal_href if principal_href.startswith("http") else \
        urlunparse((p0.scheme, p0.netloc, principal_href, "", "", ""))

    # Get calendar-home-set from principal
    r2 = _requests.request(
        "PROPFIND", principal_url,
        headers={**headers, "Depth": "0"},
        data=b"""<?xml version="1.0"?>
<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
  <d:prop><c:calendar-home-set/></d:prop>
</d:propfind>""",
        auth=auth, allow_redirects=True, timeout=30
    )
    root2 = ET.fromstring(r2.text)
    home_href = root2.findtext(".//{urn:ietf:params:xml:ns:caldav}calendar-home-set/{DAV:}href", "")
    p1 = urlparse(r2.url)
    cal_home = home_href if home_href.startswith("http") else \
        urlunparse((p1.scheme, p1.netloc, home_href, "", "", ""))

    result["principal"] = principal_url
    result["calendar_home"] = cal_home

    # Derive server base and user ID
    p2 = urlparse(cal_home)
    server = f"{p2.scheme}://{p2.netloc}"
    user_id = p2.path.strip("/").split("/")[0]
    user_root = f"{server}/{user_id}/"
    result["server"] = server
    result["user_id"] = user_id

    # ── 1. PROPFIND on user root ──────────────────────────────────
    r = _propfind(user_root, depth=1)
    result["user_root_propfind"] = {"status": r.status_code, "collections": []}
    if r.status_code in (200, 207):
        cols = _parse_cols(r.text, user_root)
        result["user_root_propfind"]["collections"] = cols

    # ── 2. Try /reminders/ sibling ────────────────────────────────
    reminders_home = f"{server}/{user_id}/reminders/"
    r = _propfind(reminders_home, depth=1)
    result["reminders_path"] = {"url": reminders_home, "status": r.status_code, "lists": []}

    if r.status_code in (200, 207):
        cols = _parse_cols(r.text, reminders_home)
        for col in cols:
            entry: Dict[str, Any] = {**col, "todos": [], "report_status": None}
            if "VTODO" in col["components"]:
                r2 = _report(col["url"])
                entry["report_status"] = r2.status_code
                if r2.status_code in (200, 207):
                    entry["todos"] = _parse_todos(r2.text)
            result["reminders_path"]["lists"].append(entry)

    # ── 3. Try other candidate paths ─────────────────────────────
    candidates = [
        f"{server}/{user_id}/tasks/",
        f"{server}/{user_id}/lists/",
        f"{cal_home}reminders/",
    ]
    found_candidates = []
    for url in candidates:
        r = _propfind(url, depth=0)
        if r.status_code in (200, 207):
            found_candidates.append({"url": url, "status": r.status_code})
    result["other_candidates"] = found_candidates

    return result


