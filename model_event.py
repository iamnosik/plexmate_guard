import json
from datetime import datetime, timedelta

from .event_policy import normal_observation_ids_to_delete
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
    def latest_schedule_observation(cls):
        """Return the newest scheduler observation without loading its payload."""
        with F.app.app_context():
            row = (
                F.db.session.query(cls)
                .filter(cls.trigger == "schedule", cls.action == "observe")
                .order_by(cls.id.desc())
                .first()
            )
        if row is None:
            return {"state": None, "created_at": None}
        return {"state": row.state, "created_at": row.created_at}

    @classmethod
    def compact_normal_observations(cls, retention_days=30):
        """Compact redundant NORMAL observations while preserving transitions."""
        try:
            retention_days = max(7, min(int(retention_days), 365))
        except (TypeError, ValueError):
            retention_days = 30
        cutoff = datetime.now() - timedelta(days=retention_days)
        with F.app.app_context():
            rows = (
                F.db.session.query(
                    cls.id, cls.created_at, cls.trigger, cls.state, cls.action
                )
                .filter(cls.action == "observe")
                .order_by(cls.id.asc())
                .all()
            )
            delete_ids = normal_observation_ids_to_delete(rows, cutoff)
            deleted = 0
            # Commit small batches so SQLite write locks are released quickly.
            # This matters most for the one-time upgrade compaction on a large
            # historical DB; normal daily maintenance usually has little or
            # nothing to delete.
            for start in range(0, len(delete_ids), 200):
                chunk = delete_ids[start:start + 200]
                try:
                    deleted += (
                        F.db.session.query(cls)
                        .filter(cls.id.in_(chunk))
                        .delete(synchronize_session=False)
                    )
                    F.db.session.commit()
                except Exception:
                    F.db.session.rollback()
                    raise
        return {"deleted": deleted, "retention_days": retention_days}

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
