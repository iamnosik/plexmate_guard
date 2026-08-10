"""Collection and manual-control service for Plexmate Guard.

This module has no automatic Plex or FF restart operation.  It observes,
records recommendations, and only performs a Plex restart after an explicit
two-step confirmation in the Guard UI.
"""

import json
import os
import sqlite3
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

from .log_parser import parse_lines
from .model_event import ModelGuardEvent
from .model_retry import ModelMetadataRetry
from .setup import *


class PlexmateGuardService:
    def __init__(self, plugin):
        self.plugin = plugin
        self._batch_lock = threading.Lock()
        self._batch_stop = threading.Event()
        self._batch_thread = None
        self._batch_state = {
            "status": "idle", "total": 0, "completed": 0, "unconfirmed": 0,
            "failed": 0, "skipped": 0, "current_id": None, "message": "대기 중", "started_at": None,
        }

    @staticmethod
    def _integer(value, default=0, minimum=None, maximum=None):
        try:
            result = int(value)
        except (TypeError, ValueError):
            result = default
        if minimum is not None:
            result = max(minimum, result)
        if maximum is not None:
            result = min(maximum, result)
        return result

    def settings(self):
        return P.ModelSetting.to_dict()

    def _plexmate(self):
        try:
            return F.PluginManager.get_plugin_instance("plex_mate")
        except Exception:
            return None

    def plex_connection(self):
        plexmate = self._plexmate()
        if plexmate is None:
            return "", "", "Plexmate 플러그인을 찾을 수 없습니다."
        try:
            base_url = (plexmate.ModelSetting.get("base_url") or "").rstrip("/")
            token = plexmate.ModelSetting.get("base_token") or ""
            if not base_url:
                return "", "", "Plexmate base_url 설정이 비어 있습니다."
            return base_url, token, ""
        except Exception as error:
            return "", "", "Plexmate 연결 설정을 읽지 못했습니다: %s" % type(error).__name__

    def _request(self, endpoint, timeout=None, method="GET"):
        base_url, token, error = self.plex_connection()
        if error:
            return {"ok": False, "status": 0, "latency_ms": None, "error": error, "body": b""}
        separator = "&" if "?" in endpoint else "?"
        url = base_url + endpoint + separator + urllib.parse.urlencode({"X-Plex-Token": token})
        timeout = self._integer(timeout or P.ModelSetting.get("http_timeout_seconds"), 3, 1, 15)
        started = time.monotonic()
        try:
            request = urllib.request.Request(url, method=method, headers={"Accept": "application/xml"})
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = response.read(512 * 1024)
                return {"ok": response.status == 200, "status": response.status, "latency_ms": round((time.monotonic() - started) * 1000), "error": "", "body": body}
        except urllib.error.HTTPError as error:
            return {"ok": False, "status": error.code, "latency_ms": round((time.monotonic() - started) * 1000), "error": "HTTPError", "body": error.read(512 * 1024)}
        except Exception as error:
            return {"ok": False, "status": 0, "latency_ms": round((time.monotonic() - started) * 1000), "error": type(error).__name__, "body": b""}

    @staticmethod
    def _read_tail(path, maximum_bytes):
        try:
            with open(path, "rb") as handle:
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                handle.seek(max(0, size - maximum_bytes))
                return handle.read().decode("utf-8", errors="replace").splitlines(), ""
        except OSError as error:
            return [], type(error).__name__

    def plex_log_path(self):
        configured = (P.ModelSetting.get("plex_log_path") or "").strip()
        return configured or "/host/volume1/PlexMediaServer/AppData/Plex Media Server/Logs/Plex Media Server.log"

    def collect_log_signals(self):
        lines, error = self._read_tail(self.plex_log_path(), self._integer(P.ModelSetting.get("log_tail_bytes"), 1048576, 65536, 5242880))
        parsed = parse_lines(lines)
        latest = parsed.get("latest_queue")
        latest_queue = latest.get("count") if latest else None
        latest_at = latest.get("at") if latest else None
        recent_cutoff = datetime.now() - timedelta(minutes=10)
        recent_queues = [item for item in parsed["queues"] if item.get("at") and item["at"] >= recent_cutoff]
        recent_timeouts = [item for item in parsed["timeout_events"] if item.get("at") and item["at"] >= recent_cutoff]
        recent_searches = [item for item in parsed["searches"] if item.get("at") and item["at"] >= recent_cutoff]
        queue_delta = None
        if len(recent_queues) >= 2:
            queue_delta = recent_queues[-1]["count"] - recent_queues[0]["count"]
        waits = parsed["agent_waits"]
        max_wait = max([item["seconds"] for item in waits], default=0)
        return {
            "available": not bool(error),
            "path": self.plex_log_path(),
            "error": error,
            "queue": latest_queue,
            "queue_at": latest_at.isoformat(timespec="seconds") if latest_at else None,
            "queue_delta_10m": queue_delta,
            "agent_waits": waits[:10],
            "max_wait_seconds": max_wait,
            "timeout_count": len(recent_timeouts),
            "searches": recent_searches,
        }

    def plexmate_work(self):
        path = "/data/db/plex_mate.db"
        try:
            connection = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=2)
            try:
                rows = connection.execute(
                    "SELECT CASE WHEN status LIKE 'ENQUEUE_%' THEN 'ENQUEUE' ELSE status END, COUNT(*) "
                    "FROM scan_item WHERE status IN ('READY', 'SCANNING') OR status LIKE 'ENQUEUE_%' GROUP BY 1"
                ).fetchall()
            finally:
                connection.close()
            counts = {"READY": 0, "ENQUEUE": 0, "SCANNING": 0}
            counts.update({row[0]: row[1] for row in rows})
            return {"available": True, "counts": counts, "error": ""}
        except Exception as error:
            return {"available": False, "counts": {}, "error": type(error).__name__}

    def process_snapshot(self):
        command = ["chroot", "/host", "/bin/ps", "-eo", "pid=,etimes=,pcpu=,pmem=,args="]
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=3, check=False)
            rows = []
            for line in result.stdout.splitlines():
                if "Plex Media Server" in line or "Plex Media Scanner" in line:
                    rows.append(line.strip())
            return {"available": result.returncode == 0, "rows": rows[:20], "error": "" if result.returncode == 0 else "ps_return_%s" % result.returncode}
        except Exception as error:
            return {"available": False, "rows": [], "error": type(error).__name__}

    def host_snapshot(self):
        data = {"load": "", "mem_available_kb": None}
        try:
            with open("/host/proc/loadavg", "r", encoding="utf-8") as handle:
                data["load"] = handle.read().split()[0:3]
        except OSError:
            pass
        try:
            with open("/host/proc/meminfo", "r", encoding="utf-8") as handle:
                for line in handle:
                    if line.startswith("MemAvailable:"):
                        data["mem_available_kb"] = int(line.split()[1])
                        break
        except (OSError, ValueError, IndexError):
            pass
        return data

    def deployment(self):
        selected = P.ModelSetting.get("deployment_mode") or "auto"
        native_paths = [
            "/host/var/packages/PlexMediaServer/enabled",
            "/host/volume1/@appstore/PlexMediaServer/Plex Media Server",
        ]
        native_present = all(os.path.exists(path) for path in native_paths)
        docker_name = (P.ModelSetting.get("docker_container") or "").strip()
        docker_socket = "/host/var/run/docker.sock"
        docker_candidate = bool(docker_name and os.path.exists(docker_socket))
        detected = "unknown"
        if selected in ("native", "docker", "external"):
            detected = selected
        elif native_present and not docker_candidate:
            detected = "native"
        elif docker_candidate and not native_present:
            detected = "docker"
        elif native_present and docker_candidate:
            detected = "ambiguous"
        control = "monitor_only"
        reason = ""
        if detected == "native":
            synopkg = "/host/usr/syno/bin/synopkg"
            if not os.path.exists(synopkg):
                return {"selected": selected, "detected": detected, "native_present": native_present, "docker_candidate": docker_candidate, "control": control, "reason": "synopkg is not mounted in the FF container"}
            control = "manual_available"
            try:
                result = subprocess.run(["chroot", "/host", "/usr/syno/bin/synopkg", "status", "PlexMediaServer"], capture_output=True, text=True, timeout=4, check=False)
                if '"status":"running"' in result.stdout:
                    reason = "DSM package status: running"
                else:
                    reason = "DSM package status is not running or could not be read"
            except Exception as error:
                reason = "synopkg status check: %s (manual restart request is still available)" % type(error).__name__
        elif detected == "docker":
            if docker_candidate:
                control = "manual_available"
                reason = "Manual restart will target Docker container '%s'" % docker_name
            else:
                reason = "Docker socket or container name is not available"
        else:
            reason = "A single verified Plex deployment was not detected"
        return {"selected": selected, "detected": detected, "native_present": native_present, "docker_candidate": docker_candidate, "control": control, "reason": reason}

    @staticmethod
    def _command_text(result):
        text = (result.stdout or "") + (result.stderr or "")
        return " ".join(text.strip().split())[:500]

    def manual_restart_preflight(self):
        """Return a target only when a user-requested restart is safe to try.

        This deliberately does not require a successful synopkg *status*
        response. On some DSM versions it can time out while the package
        manager can still accept a restart request. That response remains in
        the dashboard and event history as diagnostic information.
        """
        current_limit = self._integer(self.current_scan_limit(), -1, 0, 20)
        if current_limit != 0:
            return {"success": False, "message": "먼저 Plexmate 신규 스캔 제한을 0으로 설정하세요.", "target": ""}

        identity = self._request("/identity")
        body = identity.get("body") or b""
        if identity.get("status") == 503 and b"database migrations" in body.lower():
            return {"success": False, "message": "Plex 데이터베이스 마이그레이션 중입니다. 완료될 때까지 재시작하지 않습니다.", "target": ""}

        deployment = self.deployment()
        if deployment["detected"] == "native" and deployment["control"] == "manual_available":
            return {"success": True, "message": "Synology 네이티브 Plex 재시작을 요청할 수 있습니다.", "target": "native", "deployment": deployment}
        if deployment["detected"] == "docker" and deployment["control"] == "manual_available":
            return {"success": True, "message": "Docker Plex 재시작을 요청할 수 있습니다.", "target": "docker", "deployment": deployment}
        return {"success": False, "message": "재시작 대상을 확인하지 못했습니다: %s" % (deployment.get("reason") or deployment.get("detected")), "target": "", "deployment": deployment}

    def manual_restart(self, confirmed):
        """Request exactly one user-confirmed Plex restart; never retry it."""
        if not confirmed:
            P.logger.warning("GUARD_RESTART result=blocked reason=confirmation_missing")
            return {"success": False, "message": "재시작 확인이 필요합니다."}
        preflight = self.manual_restart_preflight()
        if not preflight.get("success"):
            ModelGuardEvent.record("manual", "RESTART_BLOCKED", "preflight", preflight["message"], {"deployment": preflight.get("deployment", {})})
            P.logger.warning("GUARD_RESTART result=blocked reason=%s", preflight["message"])
            return preflight

        target = preflight["target"]
        P.logger.warning("GUARD_RESTART result=requested target=%s plexmate_limit=0", target)
        try:
            if target == "native":
                command = ["chroot", "/host", "/usr/syno/bin/synopkg", "restart", "PlexMediaServer"]
                result = subprocess.run(command, capture_output=True, text=True, timeout=45, check=False)
                detail = self._command_text(result)
                success = result.returncode == 0
            else:
                container = (P.ModelSetting.get("docker_container") or "").strip()
                command = ["curl", "--silent", "--show-error", "--max-time", "30", "--unix-socket", "/host/var/run/docker.sock", "-o", "/dev/null", "-w", "%{http_code}", "-X", "POST", "http://localhost/containers/%s/restart?t=10" % urllib.parse.quote(container, safe="")]
                result = subprocess.run(command, capture_output=True, text=True, timeout=35, check=False)
                detail = self._command_text(result)
                success = result.returncode == 0 and result.stdout.strip() == "204"
            if success:
                message = "Plex 재시작 요청을 전달했습니다. 1~2분 뒤 새로고침으로 상태를 확인하세요."
                ModelGuardEvent.record("manual", "RESTART_REQUESTED", target, message, {"detail": detail, "deployment": preflight.get("deployment", {})})
                P.logger.warning("GUARD_RESTART result=accepted target=%s detail=%s", target, detail or "-")
                return {"success": True, "message": message, "target": target}
            message = "Plex 재시작 요청이 실패했습니다. 자동 재시도하지 않았습니다. %s" % (detail or "명령 실패")
            ModelGuardEvent.record("manual", "RESTART_ERROR", target, message, {"detail": detail, "deployment": preflight.get("deployment", {})})
            P.logger.error("GUARD_RESTART result=error target=%s detail=%s", target, detail or "-")
            return {"success": False, "message": message, "target": target}
        except subprocess.TimeoutExpired:
            message = "Plex 재시작 명령의 응답 시간이 초과되었습니다. 자동 재시도하지 않았습니다. DSM 패키지 센터에서 상태를 확인하세요."
            ModelGuardEvent.record("manual", "RESTART_TIMEOUT", target, message, {"deployment": preflight.get("deployment", {})})
            P.logger.error("GUARD_RESTART result=timeout target=%s", target)
            return {"success": False, "message": message, "target": target}
        except Exception as error:
            message = "Plex 재시작 요청을 실행하지 못했습니다: %s" % type(error).__name__
            ModelGuardEvent.record("manual", "RESTART_ERROR", target, message, {"deployment": preflight.get("deployment", {})})
            P.logger.error("GUARD_RESTART result=exception target=%s error=%s", target, type(error).__name__)
            return {"success": False, "message": message, "target": target}

    def log_snapshot(self, trigger, snapshot):
        """Write one redacted operational line for each collection cycle."""
        if not P.ModelSetting.get_bool("detailed_log_enabled"):
            return
        logs = snapshot.get("logs", {})
        plexmate = snapshot.get("plexmate", {})
        counts = plexmate.get("counts", {})
        deploy = snapshot.get("deployment", {})
        identity = snapshot.get("identity", {})
        library = snapshot.get("library", {})
        P.logger.info(
            "GUARD_OBSERVE trigger=%s state=%s plex=http%s/%sms library=http%s/%sms "
            "queue=%s delta_10m=%s sjva_max=%ss timeout_10m=%s plexmate=R%s/E%s/S%s "
            "limit=%s/%s deploy=%s/%s load=%s process_count=%s log=%s",
            trigger, snapshot.get("state"), identity.get("status"), identity.get("latency_ms"),
            library.get("status"), library.get("latency_ms"), logs.get("queue"),
            logs.get("queue_delta_10m"), logs.get("max_wait_seconds"), logs.get("timeout_count"),
            counts.get("READY", 0), counts.get("ENQUEUE", 0), counts.get("SCANNING", 0),
            snapshot.get("desired_limit") or "-", snapshot.get("actual_limit") or "-",
            deploy.get("detected"), deploy.get("control"), "/".join(snapshot.get("host", {}).get("load", [])),
            len(snapshot.get("processes", {}).get("rows", [])), "ok" if logs.get("available") else logs.get("error"),
        )
        if snapshot.get("state") in ("METADATA_BLOCKED", "PLEX_UNAVAILABLE", "MIGRATION"):
            waits = ["%s:%ss" % (item.get("agent"), item.get("seconds")) for item in logs.get("agent_waits", [])[:3]]
            P.logger.warning(
                "GUARD_ALERT state=%s reason=%s candidates=%s waits=%s deployment_reason=%s",
                snapshot.get("state"), snapshot.get("message"), len(logs.get("searches", [])),
                ",".join(waits) or "-", deploy.get("reason") or "-",
            )

    def decide_state(self, identity, logs):
        body = identity.get("body") or b""
        if identity.get("status") == 503 and b"database migrations" in body.lower():
            return "MIGRATION", "Plex database migration is running; wait only."
        if not identity.get("ok"):
            return "PLEX_UNAVAILABLE", "Plex identity request failed."
        queue_stalled = logs.get("queue") is not None and logs.get("queue_delta_10m") is not None and logs.get("queue_delta_10m") >= 0
        agent_stalled = logs.get("max_wait_seconds", 0) >= self._integer(P.ModelSetting.get("agent_wait_threshold_seconds"), 60, 30, 300)
        timeout_stalled = logs.get("timeout_count", 0) >= self._integer(P.ModelSetting.get("timeout_threshold"), 2, 1, 10)
        if queue_stalled and (agent_stalled or timeout_stalled):
            return "METADATA_BLOCKED", "Metadata queue is not decreasing while SJVA agents are delayed."
        if logs.get("queue") is not None or logs.get("max_wait_seconds", 0):
            return "BUSY", "Plex is processing scans or metadata."
        return "NORMAL", "No metadata blockage signal was found."

    def collect(self, trigger="manual"):
        identity = self._request("/identity")
        library = self._request("/library/sections")
        logs = self.collect_log_signals()
        state, message = self.decide_state(identity, logs)
        deployment = self.deployment()
        plexmate = self.plexmate_work()
        snapshot = {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "guard_enabled": P.ModelSetting.get_bool("guard_enabled"),
            "state": state,
            "message": message,
            "identity": {key: identity.get(key) for key in ("ok", "status", "latency_ms", "error")},
            "library": {key: library.get(key) for key in ("ok", "status", "latency_ms", "error")},
            "logs": logs,
            "deployment": deployment,
            "plexmate": plexmate,
            "processes": self.process_snapshot(),
            "host": self.host_snapshot(),
            "desired_limit": P.ModelSetting.get("desired_scan_limit") or "",
            "actual_limit": self.current_scan_limit(),
        }
        if state == "METADATA_BLOCKED":
            for candidate in logs.get("searches", []):
                ModelMetadataRetry.upsert_candidate(candidate, "metadata_blocked")
        ModelGuardEvent.record(trigger, state, "observe", message, snapshot)
        self.log_snapshot(trigger, snapshot)
        return snapshot

    def current_scan_limit(self):
        plexmate = self._plexmate()
        try:
            return plexmate.ModelSetting.get("scan_max_scan_count") if plexmate else ""
        except Exception:
            return ""

    def set_scan_limit(self, limit, action):
        limit = self._integer(limit, -1, 0, 20)
        if limit < 0:
            return {"success": False, "message": "실행 제한값이 올바르지 않습니다."}
        plexmate = self._plexmate()
        if plexmate is None:
            return {"success": False, "message": "Plexmate 플러그인을 찾을 수 없습니다."}
        try:
            current = self._integer(plexmate.ModelSetting.get("scan_max_scan_count"), 2, 0, 20)
            if not P.ModelSetting.get("baseline_scan_limit"):
                P.ModelSetting.set("baseline_scan_limit", str(current))
            plexmate.ModelSetting.set("scan_max_scan_count", str(limit))
            P.ModelSetting.set("desired_scan_limit", str(limit))
            message = "Plexmate 신규 스캔 제한을 %s로 설정했습니다." % limit
            ModelGuardEvent.record("manual", "MANUAL_CONTROL", action, message, {"previous": current, "requested": limit})
            P.logger.info("GUARD_CONTROL action=%s previous_limit=%s requested_limit=%s result=success", action, current, limit)
            return {"success": True, "message": message, "previous": current, "requested": limit}
        except Exception as error:
            message = "Plexmate 실행 제한을 변경하지 못했습니다: %s" % type(error).__name__
            ModelGuardEvent.record("manual", "CONTROL_ERROR", action, message, {})
            P.logger.error("GUARD_CONTROL action=%s result=error error=%s", action, type(error).__name__)
            return {"success": False, "message": message}

    def restore_baseline(self):
        baseline = self._integer(P.ModelSetting.get("baseline_scan_limit"), 2, 1, 20)
        return self.set_scan_limit(baseline, "restore_baseline")

    def batch_status(self):
        with self._batch_lock:
            data = dict(self._batch_state)
            data["running"] = bool(self._batch_thread and self._batch_thread.is_alive())
        return data

    def _set_batch_state(self, **values):
        with self._batch_lock:
            self._batch_state.update(values)

    def start_metadata_batch(self):
        with self._batch_lock:
            if self._batch_thread and self._batch_thread.is_alive():
                return {"success": False, "message": "이미 전체 metadata 갱신 작업이 실행 중입니다."}

        snapshot = self.collect("batch_preflight")
        if str(snapshot.get("actual_limit")) != "0":
            return {"success": False, "message": "전체 갱신 전에는 Plexmate 신규 스캔 제한을 0으로 설정하세요."}
        if snapshot.get("state") != "NORMAL":
            return {"success": False, "message": "Plex가 안정화된 NORMAL 상태에서만 전체 갱신을 시작합니다. 현재: %s" % snapshot.get("state")}
        item_ids = ModelMetadataRetry.pending_ids()
        if not item_ids:
            return {"success": False, "message": "전체 갱신할 대기 후보가 없습니다."}

        self._batch_stop.clear()
        self._set_batch_state(status="running", total=len(item_ids), completed=0, unconfirmed=0, failed=0, skipped=0, current_id=None, message="순차 갱신을 시작했습니다.", started_at=datetime.now().isoformat(timespec="seconds"))
        self._batch_thread = threading.Thread(target=self._batch_worker, args=(item_ids,), daemon=True, name="plexmate-guard-metadata-batch")
        self._batch_thread.start()
        ModelGuardEvent.record("manual", "METADATA_BATCH", "start", "전체 metadata 순차 갱신을 시작했습니다.", {"total": len(item_ids)})
        P.logger.warning("GUARD_METADATA_BATCH result=started total=%s interval_s=%s", len(item_ids), self._integer(P.ModelSetting.get("metadata_batch_interval_seconds"), 5, 2, 60))
        return {"success": True, "message": "%s건의 전체 metadata 갱신을 순차로 시작했습니다." % len(item_ids)}

    def stop_metadata_batch(self):
        status = self.batch_status()
        if not status.get("running"):
            return {"success": False, "message": "실행 중인 전체 갱신 작업이 없습니다."}
        self._batch_stop.set()
        self._set_batch_state(status="stopping", message="현재 요청이 끝난 뒤 중지합니다.")
        P.logger.warning("GUARD_METADATA_BATCH result=stop_requested current_id=%s", status.get("current_id"))
        return {"success": True, "message": "현재 요청이 끝난 뒤 전체 갱신을 중지합니다."}

    def _batch_pause(self, message):
        self._set_batch_state(status="paused", current_id=None, message=message)
        ModelGuardEvent.record("batch", "METADATA_BATCH", "paused", message, self.batch_status())
        P.logger.warning("GUARD_METADATA_BATCH result=paused message=%s", message)

    def _wait_for_batch_ready(self):
        """Wait for normal post-refresh work to drain before the next request."""
        interval = self._integer(P.ModelSetting.get("metadata_batch_interval_seconds"), 5, 2, 60)
        maximum_wait = self._integer(P.ModelSetting.get("metadata_batch_ready_wait_seconds"), 300, 30, 1800)
        waited = 0
        while True:
            if self._batch_stop.is_set():
                self._set_batch_state(status="stopped", current_id=None, message="사용자 요청으로 중지했습니다.")
                ModelGuardEvent.record("batch", "METADATA_BATCH", "stopped", "사용자 요청으로 전체 metadata 갱신을 중지했습니다.", self.batch_status())
                P.logger.warning("GUARD_METADATA_BATCH result=stopped")
                return False
            snapshot = self.collect("batch_item_preflight")
            if str(snapshot.get("actual_limit")) != "0":
                self._batch_pause("Plexmate 제한이 0이 아니어서 전체 갱신을 중지했습니다.")
                return False
            state = snapshot.get("state")
            if state == "NORMAL":
                return True
            if state == "BUSY" and waited < maximum_wait:
                self._set_batch_state(status="waiting", current_id=None, message="Plex가 이전 요청을 처리 중입니다. %s/%s초 대기" % (waited, maximum_wait))
                P.logger.info("GUARD_METADATA_BATCH result=waiting waited_s=%s max_wait_s=%s", waited, maximum_wait)
                if self._batch_stop.wait(interval):
                    continue
                waited += interval
                continue
            if state == "BUSY":
                self._batch_pause("Plex가 %s초 동안 계속 BUSY 상태여서 전체 갱신을 중지했습니다." % maximum_wait)
            else:
                self._batch_pause("Plex 상태가 %s여서 전체 갱신을 중지했습니다." % state)
            return False

    def _batch_worker(self, item_ids):
        interval = self._integer(P.ModelSetting.get("metadata_batch_interval_seconds"), 5, 2, 60)
        for retry_id in item_ids:
            if not self._wait_for_batch_ready():
                return

            self._set_batch_state(status="running", current_id=retry_id, message="ratingKey 요청 중")
            result = self.request_metadata_refresh(retry_id)
            row = ModelMetadataRetry.get(retry_id)
            status = row.status if row else "missing"
            state = self.batch_status()
            if status == "refresh_requested":
                self._set_batch_state(completed=state["completed"] + 1, current_id=None, message="요청 완료")
                P.logger.info("GUARD_METADATA_BATCH result=requested retry_id=%s", retry_id)
            elif result.get("http_status") == 404:
                archived = ModelMetadataRetry.archive_pending(retry_id, "plex_item_missing_http_404")
                if archived is None:
                    self._batch_pause("404 응답 후보를 이력 보관하지 못했습니다. 목록을 새로고침해 확인하세요.")
                    return
                state = self.batch_status()
                message = "Plex에서 사라진 후보 1건(HTTP 404)을 이력으로 보관하고 다음 항목을 처리합니다."
                self._set_batch_state(skipped=state.get("skipped", 0) + 1, current_id=None, message=message)
                ModelGuardEvent.record("batch", "METADATA_BATCH", "skip_404", message, {
                    "retry_id": retry_id,
                    "rating_key": archived.get("rating_key"),
                })
                P.logger.warning("GUARD_METADATA_BATCH result=skip_404 retry_id=%s rating_key=%s", retry_id, archived.get("rating_key"))
            else:
                message = result.get("message") or "metadata 요청 실패"
                self._set_batch_state(status="paused", failed=state["failed"] + 1, current_id=None, message=message)
                ModelGuardEvent.record("batch", "METADATA_BATCH", "paused", message, self.batch_status())
                P.logger.warning("GUARD_METADATA_BATCH result=paused retry_id=%s message=%s", retry_id, message)
                return

            if self._batch_stop.wait(interval):
                self._set_batch_state(status="stopped", current_id=None, message="사용자 요청으로 중지했습니다.")
                ModelGuardEvent.record("batch", "METADATA_BATCH", "stopped", "사용자 요청으로 전체 metadata 갱신을 중지했습니다.", self.batch_status())
                P.logger.warning("GUARD_METADATA_BATCH result=stopped")
                return

        self._set_batch_state(status="completed", current_id=None, message="대기 후보의 요청 처리를 마쳤습니다.")
        ModelGuardEvent.record("batch", "METADATA_BATCH", "completed", "전체 metadata 순차 갱신을 완료했습니다.", self.batch_status())
        P.logger.warning("GUARD_METADATA_BATCH result=completed total=%s", len(item_ids))

    def archive_requested_metadata(self):
        count = ModelMetadataRetry.archive_requested()
        message = "%s건의 요청 완료 항목을 삭제하지 않고 이력으로 보관했습니다." % count
        ModelGuardEvent.record("manual", "METADATA_ARCHIVE", "archive_requested", message, {"count": count})
        P.logger.info("GUARD_METADATA_ARCHIVE archived=%s", count)
        return {"success": True, "message": message, "count": count}

    def archive_pending_metadata(self, retry_id):
        status = self.batch_status()
        if status.get("running"):
            return {"success": False, "message": "전체 갱신이 실행 중일 때는 후보를 보관할 수 없습니다. 먼저 중지하거나 완료될 때까지 기다리세요."}
        row = ModelMetadataRetry.archive_pending(retry_id)
        if row is None:
            return {"success": False, "message": "대기 중인 후보를 찾지 못했습니다. 목록을 새로고침해 확인하세요."}
        message = "Plex·파일·Plex DB는 삭제하지 않고 Guard 후보만 이력으로 보관했습니다."
        ModelGuardEvent.record("manual", "METADATA_ARCHIVE", "archive_pending", message, {
            "retry_id": row.get("id"),
            "rating_key": row.get("rating_key"),
        })
        P.logger.info("GUARD_METADATA_ARCHIVE result=pending retry_id=%s rating_key=%s", row.get("id"), row.get("rating_key"))
        return {"success": True, "message": message, "item": row}

    def request_metadata_refresh(self, retry_id):
        row = ModelMetadataRetry.get(retry_id)
        if row is None or row.status != "pending":
            return {"success": False, "message": "재시도 가능한 항목이 아닙니다."}
        snapshot = self.collect("retry_preflight")
        if snapshot["state"] in ("METADATA_BLOCKED", "MIGRATION", "PLEX_UNAVAILABLE"):
            return {"success": False, "message": "현재 Plex 상태에서는 metadata 재시도를 실행하지 않습니다."}
        timeout_seconds = self._integer(P.ModelSetting.get("metadata_refresh_timeout_seconds"), 30, 5, 90)
        response = self._request(
            "/library/metadata/%s/refresh" % urllib.parse.quote(str(row.rating_key)),
            timeout=timeout_seconds,
            method="PUT",
        )
        payload = {
            "retry_id": row.id,
            "rating_key": row.rating_key,
            "http_status": response.get("status"),
            "latency_ms": response.get("latency_ms"),
            "error": response.get("error") or "",
            "timeout_seconds": timeout_seconds,
        }
        if response.get("ok"):
            row.mark_requested()
            message = "선택한 항목의 Plex metadata 갱신을 요청했습니다."
            ModelGuardEvent.record("manual", "METADATA_RETRY", "refresh_request", message, payload)
            P.logger.info("GUARD_METADATA_REFRESH result=accepted rating_key=%s http=%s latency_ms=%s timeout_s=%s", row.rating_key, response.get("status"), response.get("latency_ms"), timeout_seconds)
            return {"success": True, "message": message, "http_status": response.get("status")}
        if response.get("status") == 0 and response.get("error") in ("TimeoutError", "socket.timeout"):
            row.mark_requested()
            message = "metadata 갱신 요청이 %s초 안에 응답하지 않았습니다. Plex가 계속 처리 중일 수 있어 중복 요청은 막았습니다. 1~2분 뒤 대시보드와 Plex 로그를 확인하세요." % timeout_seconds
            ModelGuardEvent.record("manual", "METADATA_RETRY_UNCONFIRMED", "refresh_timeout", message, payload)
            P.logger.warning("GUARD_METADATA_REFRESH result=unconfirmed rating_key=%s error=%s timeout_s=%s", row.rating_key, response.get("error"), timeout_seconds)
            return {"success": False, "message": message, "http_status": response.get("status")}
        if response.get("status") == 404:
            message = "Plex에서 이 ratingKey를 찾지 못했습니다(HTTP 404). 파일을 삭제하지 않았습니다. 후보 이력 보관 후 다음 항목을 처리할 수 있습니다."
            ModelGuardEvent.record("manual", "METADATA_RETRY_MISSING", "http_404", message, payload)
            P.logger.warning("GUARD_METADATA_REFRESH result=missing rating_key=%s http=404 latency_ms=%s", row.rating_key, response.get("latency_ms"))
            return {"success": False, "message": message, "http_status": 404}
        message = "metadata 갱신 요청에 실패했습니다: HTTP %s (%s)" % (response.get("status"), response.get("error") or "unknown")
        ModelGuardEvent.record("manual", "METADATA_RETRY_ERROR", "refresh_request", message, payload)
        P.logger.warning("GUARD_METADATA_REFRESH result=error rating_key=%s http=%s error=%s latency_ms=%s", row.rating_key, response.get("status"), response.get("error") or "-", response.get("latency_ms"))
        return {"success": False, "message": message, "http_status": response.get("status")}
