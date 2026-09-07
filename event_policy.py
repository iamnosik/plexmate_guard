"""Pure event-persistence policy helpers for Plexmate Guard.

Kept independent from FlaskFarm so the policy can be unit-tested without FF.
"""

from datetime import timedelta


ALWAYS_RECORD_STATES = {
    "METADATA_BLOCKED",
    "PLEX_UNAVAILABLE",
    "PLEXMATE_DB_LOCKED",
    "MIGRATION",
}


def should_persist_observation(trigger, state, last_state, last_at, now, heartbeat_minutes=60):
    """Return (persist, reason) for an observation snapshot.

    Only scheduler observations become history. Dashboard/preflight reads are
    deliberately transient. High-signal failure states are kept every cycle;
    NORMAL/BUSY are kept on state change or as a sparse heartbeat.
    """
    if trigger != "schedule":
        return False, "transient"

    if state in ALWAYS_RECORD_STATES:
        return True, "alert"

    if last_state != state:
        return True, "state_change"

    try:
        heartbeat_minutes = max(15, min(int(heartbeat_minutes), 1440))
    except (TypeError, ValueError):
        heartbeat_minutes = 60

    if last_at is None or now - last_at >= timedelta(minutes=heartbeat_minutes):
        return True, "heartbeat"

    return False, "suppressed"


def normal_observation_ids_to_delete(rows, cutoff):
    """Choose redundant historical NORMAL observation ids for deletion.

    ``rows`` must be chronological dictionaries/objects containing id,
    created_at, state, trigger and action.  NORMAL transitions after a
    non-NORMAL observation are preserved indefinitely.  Ordinary scheduler
    NORMAL samples are compacted to one per hour inside the retention window;
    dashboard NORMAL observations are always disposable.
    """
    delete_ids = []
    transition_ids = set()
    previous_state = None

    schedule_observations = []
    for row in rows:
        trigger = row["trigger"] if isinstance(row, dict) else row.trigger
        action = row["action"] if isinstance(row, dict) else row.action
        state = row["state"] if isinstance(row, dict) else row.state
        row_id = row["id"] if isinstance(row, dict) else row.id
        if trigger == "schedule" and action == "observe":
            schedule_observations.append(row)
            if state == "NORMAL" and previous_state not in (None, "NORMAL"):
                transition_ids.add(row_id)
            previous_state = state

    kept_hour_buckets = set()
    # Walk newest first so the representative heartbeat is the latest in hour.
    for row in reversed(schedule_observations):
        row_id = row["id"] if isinstance(row, dict) else row.id
        state = row["state"] if isinstance(row, dict) else row.state
        created_at = row["created_at"] if isinstance(row, dict) else row.created_at
        if state != "NORMAL" or row_id in transition_ids:
            continue
        if created_at is None or created_at < cutoff:
            delete_ids.append(row_id)
            continue
        bucket = created_at.strftime("%Y-%m-%d %H")
        if bucket in kept_hour_buckets:
            delete_ids.append(row_id)
        else:
            kept_hour_buckets.add(bucket)

    # Dashboard NORMAL rows are read-side noise, not operational history.
    for row in rows:
        trigger = row["trigger"] if isinstance(row, dict) else row.trigger
        action = row["action"] if isinstance(row, dict) else row.action
        state = row["state"] if isinstance(row, dict) else row.state
        row_id = row["id"] if isinstance(row, dict) else row.id
        if trigger == "dashboard" and action == "observe" and state == "NORMAL":
            delete_ids.append(row_id)

    return sorted(set(delete_ids))
