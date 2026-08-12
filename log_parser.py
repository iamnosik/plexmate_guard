"""Small, dependency-free parser for Plex server log signals."""

import re
from datetime import datetime
from urllib.parse import parse_qs, unquote, urlparse

QUEUE_RE = re.compile(r"Push: Waiting for refresh queue of (?P<count>\d+) items to quiesce")
WAIT_RE = re.compile(
    r"\[Req#(?P<request>[^\]/]+).*?\]\s+\[(?P<agent>com\.plexapp\.agents\.sjva_agent[^\]]*)\] "
    r"Plug-in is starting, waiting (?P<seconds>\d+) seconds"
)
SEARCH_RE = re.compile(r"/system/agents/search\?(?P<query>[^\s]+)")
STAMP_RE = re.compile(r"^(?P<stamp>[A-Z][a-z]{2}\s+\d{1,2},\s+\d{4}\s+\d{2}:\d{2}:\d{2}\.\d+)")


def parse_stamp(line):
    match = STAMP_RE.search(line)
    if not match:
        return None
    try:
        return datetime.strptime(match.group("stamp"), "%b %d, %Y %H:%M:%S.%f")
    except ValueError:
        return None


def parse_lines(lines):
    """Return queue, agent wait, timeout and metadata-search signals from log lines."""
    queues, waits, searches, timeout_events = [], {}, [], []
    for line in lines:
        stamp = parse_stamp(line)
        queue = QUEUE_RE.search(line)
        if queue:
            queues.append({"at": stamp, "count": int(queue.group("count"))})
        wait = WAIT_RE.search(line)
        if wait:
            key = "%s:%s" % (wait.group("agent"), wait.group("request"))
            value = {
                "agent": wait.group("agent"),
                "request": wait.group("request"),
                "seconds": int(wait.group("seconds")),
                "at": stamp,
            }
            previous = waits.get(key)
            if previous is None or value["seconds"] > previous["seconds"]:
                waits[key] = value
        if "HTTP simulating 408" in line or "HTTP reply status 408" in line or "timed out" in line.lower():
            timeout_events.append({"at": stamp})
        search = SEARCH_RE.search(line)
        if search:
            try:
                query = parse_qs(unquote(search.group("query")))
                rating_key = (query.get("id") or [""])[0]
                agent = (query.get("identifier") or [""])[0]
                filename = (query.get("filename") or [""])[0]
                if rating_key and agent.startswith("com.plexapp.agents.sjva_agent"):
                    searches.append(
                        {
                            "rating_key": str(rating_key),
                            "agent": agent,
                            "path": unquote(filename),
                            "at": stamp,
                        }
                    )
            except (TypeError, ValueError):
                pass
    queues.sort(key=lambda item: item["at"] or datetime.min)
    latest_queue = queues[-1] if queues else None
    return {
        "latest_queue": latest_queue,
        "queues": queues[-30:],
        "agent_waits": sorted(waits.values(), key=lambda item: item["seconds"], reverse=True),
        "timeout_events": timeout_events[-100:],
        "searches": searches[-20:],
    }

PLEXMATE_STAMP_RE = re.compile(r"^\[(?P<stamp>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}[,.]\d+)")
DB_LOCK_RE = re.compile(r"database is locked(?:\s*\((?P<code>\d+)\))?", re.IGNORECASE)


def parse_plexmate_stamp(line):
    """Parse Plexmate's local log timestamp without exposing the log line."""
    match = PLEXMATE_STAMP_RE.search(line)
    if not match:
        return None
    try:
        stamp = match.group("stamp").replace(",", ".")
        return datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S.%f")
    except ValueError:
        return None


def parse_plexmate_lines(lines):
    """Return bounded SQLite-lock signals from Plexmate's own log.

    Only the timestamp, SQLite error code, and countable signal are returned;
    file paths and request contents are intentionally not retained.
    """
    locks = []
    for line in lines:
        match = DB_LOCK_RE.search(line)
        if not match:
            continue
        locks.append({
            "at": parse_plexmate_stamp(line),
            "code": int(match.group("code") or 5),
        })
    locks.sort(key=lambda item: item["at"] or datetime.min)
    return {"db_locks": locks[-200:]}
