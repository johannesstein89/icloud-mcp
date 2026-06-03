#!/usr/bin/env python3
"""
iCloud Reminders - targeted path discovery.
Tests /reminders/ sibling path and does Depth:1 PROPFIND on user root.
"""

import os, sys, requests
from xml.etree import ElementTree as ET
from urllib.parse import urlparse, urlunparse

USER    = os.environ["ICLOUD_USER"]
PASSWD  = os.environ["ICLOUD_APP_PASSWORD"]
AUTH    = (USER, PASSWD)
HEADERS = {"Content-Type": "application/xml; charset=utf-8"}

PROPFIND_ALL = b"""<?xml version="1.0"?>
<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
  <d:prop>
    <d:displayname/>
    <d:resourcetype/>
    <c:supported-calendar-component-set/>
  </d:prop>
</d:propfind>"""

REPORT_VTODO = b"""<?xml version="1.0" encoding="utf-8"?>
<c:calendar-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
  <d:prop><d:getetag/><c:calendar-data/></d:prop>
  <c:filter>
    <c:comp-filter name="VCALENDAR">
      <c:comp-filter name="VTODO"/>
    </c:comp-filter>
  </c:filter>
</c:calendar-query>"""


def propfind(url, depth=1):
    r = requests.request("PROPFIND", url, headers={**HEADERS, "Depth": str(depth)},
                         data=PROPFIND_ALL, auth=AUTH, allow_redirects=True, timeout=30)
    print(f"  PROPFIND {url}  →  {r.status_code}")
    return r


def report_todos(url):
    r = requests.request("REPORT", url, headers={**HEADERS, "Depth": "1"},
                         data=REPORT_VTODO, auth=AUTH, allow_redirects=True, timeout=30)
    print(f"  REPORT   {url}  →  {r.status_code}")
    return r


def parse_collections(xml_text, base_url):
    root = ET.fromstring(xml_text)
    p = urlparse(base_url)
    cols = []
    for resp in root.findall("{DAV:}response"):
        href = resp.findtext("{DAV:}href", "")
        name = resp.findtext(".//{DAV:}displayname", "(no name)")
        comp_set = resp.find(".//{urn:ietf:params:xml:ns:caldav}supported-calendar-component-set")
        components = [c.get("name") for c in comp_set] if comp_set is not None else []
        is_cal = resp.find(".//{urn:ietf:params:xml:ns:caldav}calendar") is not None
        abs_url = href if href.startswith("http") else urlunparse((p.scheme, p.netloc, href, "", "", ""))
        cols.append({"name": name, "url": abs_url, "components": components, "is_calendar": is_cal})
    return cols


def parse_todos(xml_text):
    root = ET.fromstring(xml_text)
    todos = []
    for resp in root.findall("{DAV:}response"):
        data = resp.findtext(".//{urn:ietf:params:xml:ns:caldav}calendar-data", "")
        if not data:
            continue
        fields = {}
        in_todo = False
        for line in data.splitlines():
            if line.strip() == "BEGIN:VTODO":   in_todo = True
            elif line.strip() == "END:VTODO":   break
            elif in_todo and ":" in line:
                k, _, v = line.partition(":")
                fields[k.split(";")[0]] = v
        if fields:
            todos.append(fields)
    return todos


def print_todos(todos):
    if not todos:
        print("    (none)")
        return
    for t in todos:
        print(f"    ✓ {t.get('SUMMARY','?')}  status={t.get('STATUS','?')}  due={t.get('DUE','')}")


# ── Main ──────────────────────────────────────────────────────────────────────

# Known from debug output — adjust if different
PRINCIPAL = "https://caldav.icloud.com/17106585747/principal/"
CAL_HOME  = "https://p112-caldav.icloud.com/17106585747/calendars/"

# Derive server base and user ID
p = urlparse(CAL_HOME)
SERVER    = f"{p.scheme}://{p.netloc}"
USER_ID   = p.path.split("/")[1]          # "17106585747"
USER_ROOT = f"{SERVER}/{USER_ID}/"        # https://p112-caldav.icloud.com/17106585747/

print(f"\nServer:    {SERVER}")
print(f"User ID:   {USER_ID}")
print(f"User root: {USER_ROOT}")

# ── 1. PROPFIND on user root (Depth:1) ───────────────────────────────────────
print("\n── 1. User root PROPFIND (Depth:1) ─────────────────────────────")
r = propfind(USER_ROOT, depth=1)
if r.status_code in (200, 207):
    cols = parse_collections(r.text, USER_ROOT)
    for c in cols:
        print(f"    {'[CAL]' if c['is_calendar'] else '[   ]'} {c['name']!r:30s}  {c['components']}  {c['url']}")
else:
    print(f"  Failed: {r.text[:300]}")

# ── 2. Try /reminders/ sibling directly ──────────────────────────────────────
print("\n── 2. Try /reminders/ sibling ──────────────────────────────────")
reminders_home = f"{SERVER}/{USER_ID}/reminders/"
r = propfind(reminders_home, depth=1)
if r.status_code in (200, 207):
    cols = parse_collections(r.text, reminders_home)
    print(f"  Found {len(cols)} collections:")
    for c in cols:
        vtodo = "VTODO" in c["components"]
        print(f"    {'[VTODO]' if vtodo else '[     ]'} {c['name']!r:30s}  {c['url']}")
        if vtodo:
            r2 = report_todos(c["url"])
            if r2.status_code in (200, 207):
                todos = parse_todos(r2.text)
                print(f"    → {len(todos)} reminders:")
                print_todos(todos)
else:
    print(f"  Not found ({r.status_code})")

# ── 3. Try other known iCloud reminder paths ─────────────────────────────────
print("\n── 3. Try other known paths ─────────────────────────────────────")
candidates = [
    f"{SERVER}/{USER_ID}/tasks/",
    f"{SERVER}/{USER_ID}/lists/",
    f"{CAL_HOME}reminders/",
    f"{CAL_HOME}tasks/",
]
for url in candidates:
    r = propfind(url, depth=0)
    if r.status_code in (200, 207):
        print(f"  ✓ EXISTS: {url}")
        r2 = propfind(url, depth=1)
        if r2.status_code in (200, 207):
            cols = parse_collections(r2.text, url)
            for c in cols:
                print(f"      {c['name']!r}  {c['components']}")

# ── 4. Check principal for task-home-set ─────────────────────────────────────
print("\n── 4. Principal extended PROPFIND ───────────────────────────────")
body = b"""<?xml version="1.0"?>
<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav"
            xmlns:cs="http://calendarserver.org/ns/">
  <d:prop>
    <c:calendar-home-set/>
    <cs:email-address-set/>
    <d:principal-collection-set/>
  </d:prop>
</d:propfind>"""
r = requests.request("PROPFIND", PRINCIPAL, headers={**HEADERS, "Depth": "0"},
                     data=body, auth=AUTH, allow_redirects=True, timeout=30)
print(f"  Status: {r.status_code}")
if r.status_code in (200, 207):
    # Print all hrefs found
    root = ET.fromstring(r.text)
    for el in root.iter():
        if el.tag == "{DAV:}href" and el.text:
            print(f"  href: {el.text}")
