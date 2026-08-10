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

    def process_ajax(self, command, req):
        try:
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
