"""Collection and manual-control service for Plexmate Guard.

This module has no automatic Plex or FF restart operation.  It observes,
records recommendations, and only performs a Plex restart after an explicit
two-step confirmation in the Guard UI.
"""

import json
import os
import sqlite3
import subprocess
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
            return {"success": False, "message": "재시작 확인이 필요합니다."}
        preflight = self.manual_restart_preflight()
        if not preflight.get("success"):
            ModelGuardEvent.record("manual", "RESTART_BLOCKED", "preflight", preflight["message"], {"deployment": preflight.get("deployment", {})})
            return preflight

        target = preflight["target"]
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
                return {"success": True, "message": message, "target": target}
            message = "Plex 재시작 요청이 실패했습니다. 자동 재시도하지 않았습니다. %s" % (detail or "명령 실패")
            ModelGuardEvent.record("manual", "RESTART_ERROR", target, message, {"detail": detail, "deployment": preflight.get("deployment", {})})
            return {"success": False, "message": message, "target": target}
        except subprocess.TimeoutExpired:
            message = "Plex 재시작 명령의 응답 시간이 초과되었습니다. 자동 재시도하지 않았습니다. DSM 패키지 센터에서 상태를 확인하세요."
            ModelGuardEvent.record("manual", "RESTART_TIMEOUT", target, message, {"deployment": preflight.get("deployment", {})})
            return {"success": False, "message": message, "target": target}
        except Exception as error:
            message = "Plex 재시작 요청을 실행하지 못했습니다: %s" % type(error).__name__
            ModelGuardEvent.record("manual", "RESTART_ERROR", target, message, {"deployment": preflight.get("deployment", {})})
            return {"success": False, "message": message, "target": target}

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
            return {"success": True, "message": message, "previous": current, "requested": limit}
        except Exception as error:
            message = "Plexmate 실행 제한을 변경하지 못했습니다: %s" % type(error).__name__
            ModelGuardEvent.record("manual", "CONTROL_ERROR", action, message, {})
            return {"success": False, "message": message}

    def restore_baseline(self):
        baseline = self._integer(P.ModelSetting.get("baseline_scan_limit"), 2, 1, 20)
        return self.set_scan_limit(baseline, "restore_baseline")

    def request_metadata_refresh(self, retry_id):
        row = ModelMetadataRetry.get(retry_id)
        if row is None or row.status != "pending":
            return {"success": False, "message": "재시도 가능한 항목이 아닙니다."}
        snapshot = self.collect("retry_preflight")
        if snapshot["state"] in ("METADATA_BLOCKED", "MIGRATION", "PLEX_UNAVAILABLE"):
            return {"success": False, "message": "현재 Plex 상태에서는 metadata 재시도를 실행하지 않습니다."}
        response = self._request("/library/metadata/%s/refresh" % urllib.parse.quote(str(row.rating_key)), method="PUT")
        if response.get("ok"):
            row.mark_requested()
            message = "선택한 항목의 Plex metadata 갱신을 요청했습니다."
            ModelGuardEvent.record("manual", "METADATA_RETRY", "refresh_request", message, {"retry_id": row.id, "rating_key": row.rating_key})
            return {"success": True, "message": message}
        return {"success": False, "message": "metadata 갱신 요청에 실패했습니다: HTTP %s" % response.get("status")}
