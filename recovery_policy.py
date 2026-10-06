"""Plexmate Guard 자동 회복/재개 상태 판단.

FF나 Plexmate 객체에 직접 의존하지 않는 순수 함수만 두어
자동 재개의 안전 조건을 단위 테스트할 수 있게 한다.
"""

from datetime import datetime


DANGER_STATES = {"METADATA_BLOCKED", "PLEX_UNAVAILABLE", "PLEXMATE_DB_LOCKED", "MIGRATION"}
SAFE_PROBE_STATES = {"NORMAL", "BUSY"}
PRE_PROBE_STAGES = {"paused", "recovery_ready", "stabilizing", "manual_required"}


def parse_timestamp(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def seconds_since(value, now=None):
    stamp = parse_timestamp(value)
    if stamp is None:
        return None
    now = now or datetime.now()
    return max(0, int((now - stamp).total_seconds()))


def within_minutes(value, minutes, now=None):
    elapsed = seconds_since(value, now)
    if elapsed is None:
        return False
    return elapsed <= max(0, int(minutes)) * 60


def expected_limit_for_stage(stage):
    if stage in PRE_PROBE_STAGES:
        return 0
    if stage == "probe":
        return 1
    return None


def assess_auto_recovery(
    *,
    active,
    owner,
    stage,
    recovery_ready,
    manual_required,
    auto_resume_enabled,
    actual_limit,
    previous_limit,
    state,
    identity_ok,
    library_ok,
    db_lock_count,
    scanning_count,
    d_state_count,
    max_d_state,
    recovery_ready_at,
    probe_started_at,
    stable_minutes,
    probe_minutes,
    now=None,
):
    """현재 관찰값을 바탕으로 자동 재개 단계에서 필요한 조치를 반환한다."""
    now = now or datetime.now()
    result = {
        "action": "none",
        "target_limit": None,
        "remaining_seconds": 0,
        "reason": "",
    }

    if not active or owner != "guard":
        return result

    expected = expected_limit_for_stage(stage)
    if expected is not None and int(actual_limit) != expected:
        result.update({
            "action": "external_override",
            "reason": "Guard가 예상한 실행 제한과 실제 값이 달라 자동 제어권을 해제합니다.",
        })
        return result

    if stage == "probe":
        unsafe_reasons = []
        if state in DANGER_STATES or state not in SAFE_PROBE_STATES:
            unsafe_reasons.append("상태=%s" % state)
        if not identity_ok:
            unsafe_reasons.append("Plex identity 응답 실패")
        if not library_ok:
            unsafe_reasons.append("Plex library 응답 실패")
        if int(db_lock_count or 0) > 0:
            unsafe_reasons.append("Plexmate DB 잠김 감지")
        if int(d_state_count or 0) > int(max_d_state):
            unsafe_reasons.append("호스트 D-state %s개" % d_state_count)
        if unsafe_reasons:
            result.update({
                "action": "probe_failed",
                "target_limit": 0,
                "reason": "; ".join(unsafe_reasons),
            })
            return result

        elapsed = seconds_since(probe_started_at, now)
        if elapsed is None:
            result.update({
                "action": "probe_failed",
                "target_limit": 0,
                "reason": "시험 재개 시작 시각을 확인할 수 없습니다.",
            })
            return result
        required = max(1, int(probe_minutes)) * 60
        if elapsed >= required:
            result.update({
                "action": "complete_resume",
                "target_limit": max(1, int(previous_limit or 1)),
                "reason": "시험 재개 후 안정 감시 시간을 통과했습니다.",
            })
        else:
            result.update({
                "action": "wait_probe",
                "remaining_seconds": required - elapsed,
                "reason": "시험 재개 상태를 감시 중입니다.",
            })
        return result

    if not recovery_ready or not auto_resume_enabled or manual_required:
        return result

    stable_reasons = []
    if state != "NORMAL":
        stable_reasons.append("상태=%s" % state)
    if not identity_ok:
        stable_reasons.append("Plex identity 응답 실패")
    if not library_ok:
        stable_reasons.append("Plex library 응답 실패")
    if int(db_lock_count or 0) > 0:
        stable_reasons.append("Plexmate DB 잠김 감지")
    if int(scanning_count or 0) > 0:
        stable_reasons.append("Plexmate SCANNING %s건" % scanning_count)
    if int(d_state_count or 0) > int(max_d_state):
        stable_reasons.append("호스트 D-state %s개" % d_state_count)
    if stable_reasons:
        result.update({
            "action": "wait_stable",
            "reason": "; ".join(stable_reasons),
        })
        return result

    elapsed = seconds_since(recovery_ready_at, now)
    if elapsed is None:
        result.update({
            "action": "wait_stable",
            "reason": "회복 안정화 시작 시각을 확인할 수 없습니다.",
        })
        return result

    required = max(1, int(stable_minutes)) * 60
    if elapsed >= required:
        result.update({
            "action": "start_probe",
            "target_limit": 1,
            "reason": "회복 안정화 시간을 통과해 1개 시험 재개를 시작합니다.",
        })
    else:
        result.update({
            "action": "wait_stable",
            "remaining_seconds": required - elapsed,
            "reason": "자동 재개 전 추가 안정화를 확인 중입니다.",
        })
    return result
