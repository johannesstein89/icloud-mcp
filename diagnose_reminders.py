#!/usr/bin/env python3
"""
iCloud Reminders Diagnostic Script

Reads ICLOUD_USER and ICLOUD_APP_PASSWORD from environment variables.
Tests multiple approaches to fetch VTODOs and prints raw server info.
"""

import os
import sys
import requests
from xml.etree import ElementTree as ET

CALDAV_URL = "https://caldav.icloud.com"
NS = {
    "d": "DAV:",
    "c": "urn:ietf:params:xml:ns:caldav",
    "cs": "http://calendarserver.org/ns/",
    "a": "http://apple.com/ns/ical/",
}


def _auth():
    user = os.environ.get("ICLOUD_USER")
    pw = os.environ.get("ICLOUD_APP_PASSWORD")
    if not user or not pw:
        print("ERROR: Set ICLOUD_USER and ICLOUD_APP_PASSWORD env vars.")
        sys.exit(1)
    return user, pw


def _req(method, url, auth, headers=None, body=None):
    h = {"Content-Type": "application/xml; charset=utf-8", **(headers or {})}
    r = requests.request(method, url, headers=h, data=body, auth=auth,
                         allow_redirects=True, timeout=30)
    return r


def discover_principal(auth):
    """Step 1: Discover the CalDAV principal URL."""
    print("\n── Step 1: Principal Discovery ──────────────────────────────")
    body = b"""<?xml version="1.0"?>
<d:propfind xmlns:d="DAV:">
  <d:prop>
    <d:current-user-principal/>
    <d:principal-URL/>
  </d:prop>
</d:propfind>"""
    r = _req("PROPFIND", CALDAV_URL, auth, {"Depth": "0"}, body)
    print(f"  Status: {r.status_code}  Final URL: {r.url}")

    root = ET.fromstring(r.text)
    href = root.find(".//{DAV:}current-user-principal/{DAV:}href")
    if href is None:
        href = root.find(".//{DAV:}principal-URL/{DAV:}href")

    principal_path = href.text if href is not None else None
    if not principal_path:
        print("  ERROR: Could not find principal URL in response.")
        print("  Raw:", r.text[:800])
        return None

    # Build absolute URL
    from urllib.parse import urlparse, urlunparse
    p = urlparse(r.url)
    if principal_path.startswith("http"):
        principal_url = principal_path
    else:
        principal_url = urlunparse((p.scheme, p.netloc, principal_path, "", "", ""))

    print(f"  Principal URL: {principal_url}")
    return principal_url


def discover_calendar_home(principal_url, auth):
    """Step 2: Find calendar-home-set."""
    print("\n── Step 2: Calendar Home Set ────────────────────────────────")
    body = b"""<?xml version="1.0"?>
<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
  <d:prop>
    <c:calendar-home-set/>
  </d:prop>
</d:propfind>"""
    r = _req("PROPFIND", principal_url, auth, {"Depth": "0"}, body)
    print(f"  Status: {r.status_code}")

    root = ET.fromstring(r.text)
    href = root.find(".//{urn:ietf:params:xml:ns:caldav}calendar-home-set/{DAV:}href")
    if href is None:
        print("  ERROR: No calendar-home-set found.")
        print("  Raw:", r.text[:800])
        return None

    from urllib.parse import urlparse, urlunparse
    p = urlparse(r.url)
    path = href.text
    if path.startswith("http"):
        home_url = path
    else:
        home_url = urlunparse((p.scheme, p.netloc, path, "", "", ""))

    print(f"  Calendar home: {home_url}")
    return home_url


def list_collections(home_url, auth):
    """Step 3: List all CalDAV collections with their component support."""
    print("\n── Step 3: All Collections (Depth:1 PROPFIND) ───────────────")
    body = b"""<?xml version="1.0"?>
<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav" xmlns:a="http://apple.com/ns/ical/">
  <d:prop>
    <d:displayname/>
    <d:resourcetype/>
    <c:supported-calendar-component-set/>
    <a:calendar-color/>
  </d:prop>
</d:propfind>"""
    r = _req("PROPFIND", home_url, auth, {"Depth": "1"}, body)
    print(f"  Status: {r.status_code}")

    root = ET.fromstring(r.text)
    collections = []
    for resp in root.findall("{DAV:}response"):
        href_el = resp.find("{DAV:}href")
        url = href_el.text if href_el is not None else "?"

        name_el = resp.find(".//{DAV:}displayname")
        name = name_el.text if name_el is not None else "(no name)"

        # Get supported components
        comp_set = resp.find(".//{urn:ietf:params:xml:ns:caldav}supported-calendar-component-set")
        components = []
        if comp_set is not None:
            for comp in comp_set:
                n = comp.get("name")
                if n:
                    components.append(n)

        is_calendar = resp.find(".//{urn:ietf:params:xml:ns:caldav}calendar") is not None

        print(f"  {'[CAL]' if is_calendar else '[   ]'} {name!r:30s}  components={components or '?'}  url={url}")
        collections.append({"url": url, "name": name, "components": components,
                            "is_calendar": is_calendar, "base_url": home_url})

    return collections


def fetch_todos_report(collection_url, auth, base_url):
    """Step 4: Try fetching VTODOs via calendar-query REPORT."""
    from urllib.parse import urlparse, urlunparse
    p = urlparse(base_url)

    if collection_url.startswith("http"):
        abs_url = collection_url
    else:
        abs_url = urlunparse((p.scheme, p.netloc, collection_url, "", "", ""))

    body = b"""<?xml version="1.0" encoding="utf-8"?>
<c:calendar-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
  <d:prop>
    <d:getetag/>
    <c:calendar-data/>
  </d:prop>
  <c:filter>
    <c:comp-filter name="VCALENDAR">
      <c:comp-filter name="VTODO"/>
    </c:comp-filter>
  </c:filter>
</c:calendar-query>"""
    r = _req("REPORT", abs_url, auth, {"Depth": "1"}, body)
    return r


def parse_todos_from_response(r):
    """Parse VTODO objects from a multi-status response."""
    if r.status_code not in (200, 207):
        return []

    todos = []
    try:
        root = ET.fromstring(r.text)
        for resp in root.findall("{DAV:}response"):
            cal_data = resp.find(".//{urn:ietf:params:xml:ns:caldav}calendar-data")
            if cal_data is not None and cal_data.text:
                todos.append(cal_data.text)
    except Exception as e:
        print(f"    Parse error: {e}")
    return todos


def parse_vtodo_fields(ical_text):
    """Extract basic fields from raw iCalendar VTODO text."""
    fields = {}
    in_vtodo = False
    for line in ical_text.splitlines():
        if line == "BEGIN:VTODO":
            in_vtodo = True
        elif line == "END:VTODO":
            break
        elif in_vtodo and ":" in line:
            k, _, v = line.partition(":")
            k = k.split(";")[0]  # strip params
            fields[k] = v
    return fields


def run_diagnostics(test_write=False):
    auth = _auth()
    user, _ = auth

    print(f"\n{'='*60}")
    print(f"  iCloud Reminders Diagnostics")
    print(f"  User: {user}")
    print(f"{'='*60}")

    # Step 1: Principal
    principal_url = discover_principal(auth)
    if not principal_url:
        return

    # Step 2: Calendar home
    home_url = discover_calendar_home(principal_url, auth)
    if not home_url:
        return

    # Step 3: All collections
    collections = list_collections(home_url, auth)

    vtodo_collections = [c for c in collections if "VTODO" in c["components"]]
    all_calendars = [c for c in collections if c["is_calendar"]]

    print(f"\n  Summary: {len(collections)} total, {len(all_calendars)} calendars,")
    print(f"           {len(vtodo_collections)} VTODO-capable (=reminder lists)")

    # Step 4: Fetch todos from each VTODO collection
    print("\n── Step 4: Fetch VTODOs from Reminder Lists ─────────────────")

    if not vtodo_collections:
        print("  No VTODO collections found — trying ALL calendars as fallback")
        vtodo_collections = all_calendars

    from urllib.parse import urlparse
    p = urlparse(home_url)
    base = f"{p.scheme}://{p.netloc}"

    total_found = 0
    for col in vtodo_collections:
        url = col["url"]
        name = col["name"]
        print(f"\n  List: {name!r}")
        print(f"  URL:  {url}")

        r = fetch_todos_report(url, auth, base)
        print(f"  REPORT status: {r.status_code}")

        if r.status_code not in (200, 207):
            print(f"  Response body: {r.text[:300]}")
            continue

        todos_raw = parse_todos_from_response(r)
        print(f"  VTODOs found: {len(todos_raw)}")

        for i, raw in enumerate(todos_raw[:10]):  # max 10 per list
            fields = parse_vtodo_fields(raw)
            summary = fields.get("SUMMARY", "(no title)")
            status = fields.get("STATUS", "NEEDS-ACTION")
            due = fields.get("DUE", "")
            priority = fields.get("PRIORITY", "")
            description = fields.get("DESCRIPTION", "")
            print(f"    [{i+1}] {summary}")
            print(f"         Status={status}  Due={due}  Priority={priority}")
            if description:
                print(f"         Notes={description[:80]}")
            total_found += 1

        if len(todos_raw) > 10:
            print(f"    ... and {len(todos_raw)-10} more")

    print(f"\n── Summary ──────────────────────────────────────────────────")
    print(f"  Total reminders found: {total_found}")

    if total_found == 0:
        print("\n  Possible reasons for empty results:")
        print("  1. Reminders are stored locally on device, not synced to iCloud")
        print("     → On iPhone: Settings > [Name] > iCloud > Reminders = ON")
        print("  2. Reminders are in a different iCloud account")
        print("  3. Server requires a different CalDAV endpoint")
        print(f"     → Tried: {home_url}")

    # Step 5: Optional write test
    if test_write:
        print("\n── Step 5: Write Test ───────────────────────────────────────")
        if not vtodo_collections:
            print("  No reminder list available for write test.")
            return

        target = vtodo_collections[0]
        url = target["url"]
        if not url.startswith("http"):
            url = base + url

        from datetime import datetime
        import uuid
        uid = str(uuid.uuid4())
        now = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
        ical = f"""BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//DiagScript//EN\r\nBEGIN:VTODO\r\nUID:{uid}\r\nDTSTAMP:{now}\r\nCREATED:{now}\r\nSUMMARY:iCloud MCP Diagnose-Test\r\nSTATUS:NEEDS-ACTION\r\nEND:VTODO\r\nEND:VCALENDAR\r\n"""

        put_url = url.rstrip("/") + f"/{uid}.ics"
        r_put = requests.put(put_url,
                             headers={"Content-Type": "text/calendar; charset=utf-8"},
                             data=ical.encode("utf-8"),
                             auth=auth, timeout=30)
        print(f"  PUT test reminder → {r_put.status_code}")

        if r_put.status_code in (200, 201, 204):
            print("  Write: OK")
            r_del = requests.delete(put_url, auth=auth, timeout=30)
            print(f"  DELETE test reminder → {r_del.status_code}")
            print("  Write+Delete: OK ✓")
        else:
            print(f"  Write FAILED: {r_put.text[:300]}")


if __name__ == "__main__":
    write_test = "--write-test" in sys.argv
    run_diagnostics(test_write=write_test)
