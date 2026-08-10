from datetime import datetime

from .setup import *


class ModelMetadataRetry(ModelBase):
    P = P
    __tablename__ = "metadata_retry"
    __bind_key__ = P.package_name

    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime, default=datetime.now)
    updated_at = db.Column(db.DateTime, default=datetime.now)
    rating_key = db.Column(db.String)
    agent = db.Column(db.String)
    media_path = db.Column(db.String)
    reason = db.Column(db.String)
    status = db.Column(db.String)
    requested_at = db.Column(db.DateTime)

    @classmethod
    def upsert_candidate(cls, candidate, reason):
        rating_key = str(candidate.get("rating_key") or "")
        if not rating_key:
            return None
        with F.app.app_context():
            row = F.db.session.query(cls).filter_by(rating_key=rating_key, status="pending").first()
            if row is None:
                row = cls()
                row.created_at = datetime.now()
                row.rating_key = rating_key
                row.status = "pending"
                F.db.session.add(row)
            row.updated_at = datetime.now()
            row.agent = str(candidate.get("agent") or "")[:255]
            row.media_path = str(candidate.get("path") or "")[:2000]
            row.reason = str(reason or "metadata_blocked")[:255]
            F.db.session.commit()
            return row.id

    @classmethod
    def recent(cls, limit=100, statuses=None):
        limit = max(1, min(int(limit or 100), 300))
        with F.app.app_context():
            query = F.db.session.query(cls)
            if statuses:
                query = query.filter(cls.status.in_(list(statuses)))
            rows = query.order_by(cls.id.desc()).limit(limit).all()
        return [row.to_dict() for row in rows]

    @classmethod
    def pending_ids(cls):
        with F.app.app_context():
            rows = F.db.session.query(cls.id).filter_by(status="pending").order_by(cls.id.asc()).all()
        return [row[0] for row in rows]

    @classmethod
    def status_counts(cls):
        with F.app.app_context():
            rows = F.db.session.query(cls.status, db.func.count(cls.id)).group_by(cls.status).all()
        return {str(status or "unknown"): count for status, count in rows}

    @classmethod
    def archive_requested(cls):
        with F.app.app_context():
            rows = F.db.session.query(cls).filter_by(status="refresh_requested").all()
            now = datetime.now()
            for row in rows:
                row.status = "archived"
                row.updated_at = now
            F.db.session.commit()
        return len(rows)

    @classmethod
    def archive_pending(cls, row_id, reason="user_archived"):
        """Hide one stale candidate without touching Plex or its media file."""
        with F.app.app_context():
            row = F.db.session.query(cls).filter_by(id=int(row_id), status="pending").first()
            if row is None:
                return None
            row.status = "archived"
            row.reason = str(reason or "user_archived")[:255]
            row.updated_at = datetime.now()
            F.db.session.commit()
            return row.to_dict()

    @classmethod
    def get(cls, row_id):
        with F.app.app_context():
            return F.db.session.query(cls).filter_by(id=int(row_id)).first()

    def mark_requested(self):
        self.status = "refresh_requested"
        self.requested_at = datetime.now()
        self.updated_at = datetime.now()
        return self.save()

    def to_dict(self):
        def stamp(value):
            return value.isoformat(timespec="seconds") if value else None
        return {
            "id": self.id,
            "created_at": stamp(self.created_at),
            "updated_at": stamp(self.updated_at),
            "rating_key": self.rating_key,
            "agent": self.agent,
            "media_path": self.media_path,
            "reason": self.reason,
            "status": self.status,
            "requested_at": stamp(self.requested_at),
        }
