import json
from datetime import datetime

from .setup import *


class ModelGuardEvent(ModelBase):
    P = P
    __tablename__ = "guard_event"
    __bind_key__ = P.package_name

    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime, default=datetime.now)
    trigger = db.Column(db.String)
    state = db.Column(db.String)
    action = db.Column(db.String)
    message = db.Column(db.String)
    payload_json = db.Column(db.Text)

    @classmethod
    def record(cls, trigger, state, action, message, payload):
        entity = cls()
        entity.created_at = datetime.now()
        entity.trigger = str(trigger or "unknown")[:40]
        entity.state = str(state or "UNKNOWN")[:40]
        entity.action = str(action or "observe")[:40]
        entity.message = str(message or "")[:1000]
        entity.payload_json = json.dumps(payload or {}, ensure_ascii=False, default=str)
        return entity.save()

    @classmethod
    def recent(cls, limit=50):
        limit = max(1, min(int(limit or 50), 200))
        with F.app.app_context():
            rows = F.db.session.query(cls).order_by(cls.id.desc()).limit(limit).all()
        return [row.to_dict() for row in rows]

    def to_dict(self):
        try:
            payload = json.loads(self.payload_json or "{}")
        except (TypeError, ValueError):
            payload = {}
        return {
            "id": self.id,
            "created_at": self.created_at.isoformat(timespec="seconds") if self.created_at else None,
            "trigger": self.trigger,
            "state": self.state,
            "action": self.action,
            "message": self.message,
            "payload": payload,
        }
