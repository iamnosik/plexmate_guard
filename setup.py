"""Plexmate Guard FlaskFarm plugin registration."""

setting = {
    "filepath": __file__,
    "use_db": True,
    "use_default_setting": True,
    "home_module": "main",
    "menu": {
        "uri": __package__,
        "name": "Plexmate Guard",
        "list": [
            {
                "uri": "main",
                "name": "상태",
                "list": [
                    {"uri": "dashboard", "name": "대시보드"},
                    {"uri": "retries", "name": "메타데이터 재시도"},
                    {"uri": "history", "name": "판단 이력"},
                ],
            },
            {"uri": "setting", "name": "설정"},
            {"uri": "log", "name": "로그"},
        ],
    },
    "setting_menu": None,
    "default_route": "normal",
}

from plugin import *

P = create_plugin_instance(setting)

from .model_event import ModelGuardEvent
from .model_retry import ModelMetadataRetry
from .mod_main import ModuleMain
from .mod_setting import ModuleSetting

P.guard_event_model = ModelGuardEvent
P.metadata_retry_model = ModelMetadataRetry
P.set_module_list([ModuleMain, ModuleSetting])
logger = P.logger
