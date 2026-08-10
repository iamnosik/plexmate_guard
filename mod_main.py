import time
import traceback

from flask import jsonify, render_template

from .guard_service import PlexmateGuardService
from .model_event import ModelGuardEvent
from .model_retry import ModelMetadataRetry
from .setup import *


class ModuleMain(PluginModuleBase):
    def __init__(self, plugin):
        super().__init__(plugin, name="main", first_menu="dashboard", scheduler_desc="Plexmate Guard 관찰")
        self.db_default = {
            "guard_enabled": "False",
            "main_auto_start": "True",
            "main_interval": "2",
            "deployment_mode": "auto",
            "docker_container": "",
            "plex_log_path": "/host/volume1/PlexMediaServer/AppData/Plex Media Server/Logs/Plex Media Server.log",
            "http_timeout_seconds": "3",
            "metadata_refresh_timeout_seconds": "30",
            "metadata_batch_interval_seconds": "5",
            "metadata_batch_ready_wait_seconds": "300",
            "log_tail_bytes": "1048576",
            "agent_wait_threshold_seconds": "60",
            "timeout_threshold": "2",
            "baseline_scan_limit": "2",
            "desired_scan_limit": "",
            "detailed_log_enabled": "True",
        }
        P.plexmate_guard_service = PlexmateGuardService(P)

    @property
    def service(self):
        return P.plexmate_guard_service

    def process_menu(self, page, req):
        page = page if page in {"dashboard", "retries", "history"} else "dashboard"
        return render_template(f"{P.package_name}_{self.name}_{page}.html", arg=P.ModelSetting.to_dict())

    def process_ajax(self, command, req):
        try:
            if command == "report":
                return jsonify({"ret": "success", "data": self.service.collect("dashboard")})
            if command == "pause":
                data = self.service.set_scan_limit(0, "manual_pause")
            elif command == "limit_one":
                data = self.service.set_scan_limit(1, "manual_limit_one")
            elif command == "resume_one":
                data = self.service.set_scan_limit(1, "manual_resume_one")
            elif command == "restore_baseline":
                data = self.service.restore_baseline()
            elif command == "ack_restart":
                ModelGuardEvent.record("manual", "RESTART_RECOMMENDATION", "acknowledged", "사용자가 재시작 권고를 확인했습니다.", {})
                data = {"success": True, "message": "재시작 권고 확인을 기록했습니다. Plex 재시작은 사용자가 수행합니다."}
            elif command == "manual_restart":
                data = self.service.manual_restart(req.form.get("confirm") == "yes")
            elif command == "retries":
                return jsonify({"ret": "success", "data": {
                    "pending": ModelMetadataRetry.recent(statuses=["pending"]),
                    "history": ModelMetadataRetry.recent(statuses=["refresh_requested", "archived"]),
                    "counts": ModelMetadataRetry.status_counts(),
                    "batch": self.service.batch_status(),
                }})
            elif command == "batch_start":
                data = self.service.start_metadata_batch()
            elif command == "batch_stop":
                data = self.service.stop_metadata_batch()
            elif command == "archive_requested":
                data = self.service.archive_requested_metadata()
            elif command == "retry_refresh":
                data = self.service.request_metadata_refresh(req.form.get("id"))
            elif command == "history":
                return jsonify({"ret": "success", "data": ModelGuardEvent.recent(req.form.get("limit", 50))})
            else:
                return jsonify({"ret": "warning", "msg": "지원하지 않는 요청입니다."}), 400
            return jsonify({"ret": "success" if data.get("success") else "warning", "msg": data.get("message"), "data": data})
        except Exception as error:
            P.logger.error("Plexmate Guard AJAX 오류: %s", error)
            P.logger.error(traceback.format_exc())
            return jsonify({"ret": "danger", "msg": "요청 처리에 실패했습니다. Guard 로그를 확인해 주세요."}), 500

    def scheduler_function(self):
        if not P.ModelSetting.get_bool("guard_enabled"):
            return
        try:
            self.service.collect("schedule")
        except Exception as error:
            P.logger.error("Plexmate Guard 관찰 실패: %s", error)
            P.logger.error(traceback.format_exc())

    def setting_save_after(self, change_list):
        P.logger.info("GUARD_SETTING saved_keys=%s", ",".join(sorted(change_list)))
        if not any(key in change_list for key in ("guard_enabled", "main_auto_start", "main_interval")):
            return
        try:
            P.logic.scheduler_stop(self.name)
        except Exception:
            pass
        if P.ModelSetting.get_bool("guard_enabled") and P.ModelSetting.get_bool("main_auto_start"):
            P.logic.scheduler_start(self.name)
