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

    def process_ajax(self, command, req):
        try:
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
