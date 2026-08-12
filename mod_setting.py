import re
import traceback

from flask import jsonify, render_template

from .setup import *


class ModuleSetting(PluginModuleBase):
    def __init__(self, plugin):
        super().__init__(plugin, name="setting")

    @property
    def service(self):
        return P.plexmate_guard_service

    def process_menu(self, page, req):
        return render_template(f"{P.package_name}_{self.name}.html", arg=P.ModelSetting.to_dict())

    def _sync_guard_scheduler(self):
        try:
            P.logic.scheduler_stop("main")
        except Exception:
            pass
        if P.ModelSetting.get_bool("guard_enabled") and P.ModelSetting.get_bool("main_auto_start"):
            P.logic.scheduler_start("main")

    _BOOL_KEYS = ("guard_enabled", "main_auto_start", "detailed_log_enabled", "auto_brake_enabled")
    _TEXT_KEYS = ("docker_container", "plex_log_path", "plexmate_log_path")
    _INT_RANGES = {
        "http_timeout_seconds": (1, 15),
        "metadata_refresh_timeout_seconds": (5, 90),
        "metadata_batch_interval_seconds": (2, 60),
        "metadata_batch_ready_wait_seconds": (30, 1800),
        "agent_wait_threshold_seconds": (30, 300),
        "timeout_threshold": (1, 10),
        "db_lock_window_minutes": (1, 60),
        "db_lock_threshold": (2, 20),
        "db_lock_burst_window_seconds": (10, 600),
        "db_lock_burst_threshold": (2, 20),
        "auto_brake_blocked_required": (2, 10),
        "auto_brake_unavailable_required": (2, 10),
        "auto_brake_db_lock_required": (2, 10),
        "auto_brake_recovery_required": (2, 10),
        "log_tail_bytes": (65536, 5242880),
        "baseline_scan_limit": (1, 20),
    }

    def _save_all_settings(self, req):
        """Save the Guard form without relying on FF's global setting handler."""
        pending = {}
        errors = []
        for key in self._BOOL_KEYS:
            pending[key] = "True" if str(req.form.get(key, "False")).lower() in ("true", "1", "on", "yes") else "False"

        deployment_mode = str(req.form.get("deployment_mode", "auto")).strip().lower()
        if deployment_mode not in ("auto", "native", "docker", "external"):
            errors.append("설치 방식")
        else:
            pending["deployment_mode"] = deployment_mode

        for key in self._TEXT_KEYS:
            value = str(req.form.get(key, "")).strip()
            if len(value) > 1000:
                errors.append(key)
            else:
                pending[key] = value

        main_interval = str(req.form.get("main_interval", "2")).strip()
        if not main_interval or len(main_interval) > 80 or not re.match(r"^[0-9*?,/\- ]+$", main_interval):
            errors.append("관찰 주기")
        else:
            pending["main_interval"] = main_interval

        for key, (minimum, maximum) in self._INT_RANGES.items():
            raw = str(req.form.get(key, "")).strip()
            try:
                value = int(raw)
            except (TypeError, ValueError):
                errors.append(key)
                continue
            if value < minimum or value > maximum:
                errors.append(key)
                continue
            pending[key] = str(value)

        if errors:
            return {"success": False, "message": "입력 범위를 확인해 주세요: " + ", ".join(errors), "errors": errors}

        changed = []
        for key, value in pending.items():
            if P.ModelSetting.get(key) != value:
                P.ModelSetting.set(key, value)
                changed.append(key)
        self._sync_guard_scheduler()
        P.logger.info("GUARD_SETTING_EXPLICIT_SAVED changed=%s", ",".join(sorted(changed)) or "-")
        return {"success": True, "message": "Guard 설정을 저장했습니다.", "changed": changed,
                "settings": {key: P.ModelSetting.get(key) for key in pending}}

    def process_ajax(self, command, req):
        try:
            if command == "settings_save":
                data = self._save_all_settings(req)
                return jsonify({"ret": "success" if data.get("success") else "warning",
                                "msg": data.get("message"), "data": data})
            if command == "operation_mode_save":
                changed = []
                for key in ("guard_enabled", "main_auto_start", "detailed_log_enabled", "auto_brake_enabled"):
                    value = "True" if str(req.form.get(key, "False")).lower() in ("true", "1", "on", "yes") else "False"
                    if P.ModelSetting.get(key) != value:
                        P.ModelSetting.set(key, value)
                        changed.append(key)
                self._sync_guard_scheduler()
                P.logger.info(
                    "GUARD_MODE_SAVED changed=%s guard_enabled=%s main_auto_start=%s detailed_log=%s",
                    ",".join(changed) or "-", P.ModelSetting.get("guard_enabled"),
                    P.ModelSetting.get("main_auto_start"), P.ModelSetting.get("detailed_log_enabled"),
                )
                return jsonify({
                    "ret": "success",
                    "msg": "운영 모드를 즉시 저장했습니다.",
                    "data": {key: P.ModelSetting.get(key) for key in ("guard_enabled", "main_auto_start", "detailed_log_enabled", "auto_brake_enabled")},
                })
            if command == "connection_test":
                identity = self.service._request("/identity")
                P.logger.info("GUARD_CONNECTION_TEST status=%s latency_ms=%s error=%s", identity.get("status"), identity.get("latency_ms"), identity.get("error") or "-")
                return jsonify({
                    "ret": "success" if identity.get("ok") else "warning",
                    "msg": "Plex 연결을 확인했습니다." if identity.get("ok") else "Plex 연결을 확인하지 못했습니다.",
                    "data": {key: identity.get(key) for key in ("ok", "status", "latency_ms", "error")},
                })
            if command == "deployment_detect":
                data = self.service.deployment()
                P.logger.info("GUARD_DEPLOYMENT detected=%s control=%s reason=%s", data.get("detected"), data.get("control"), data.get("reason") or "-")
                return jsonify({"ret": "success", "msg": "Plex 설치 방식을 점검했습니다.", "data": data})
            return jsonify({"ret": "warning", "msg": "지원하지 않는 요청입니다."}), 400
        except Exception as error:
            P.logger.error("Plexmate Guard 설정 요청 오류: %s", error)
            P.logger.error(traceback.format_exc())
            return jsonify({"ret": "danger", "msg": "설정 요청 처리에 실패했습니다."}), 500
