"""Database models for the authentication system (IP-based locking, no email verification)."""

from datetime import datetime

from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin

db = SQLAlchemy()


def _utcnow():
    """Return timezone-naive UTC datetime (compatible with SQLite storage)."""
    return datetime.utcnow()


class User(UserMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    is_admin = db.Column(db.Boolean, default=False, nullable=False)
    is_locked = db.Column(db.Boolean, default=False, nullable=False)
    ip_address = db.Column(db.String(45), default="")  # first login IP stored here
    api_key = db.Column(db.String(255), nullable=False, default="")  # personal OpenRouter API key
    created_at = db.Column(db.DateTime, nullable=False, default=_utcnow)

    # relationships
    logs = db.relationship(
        "LoginLog", backref="user", lazy="dynamic",
        cascade="all, delete-orphan",
    )
    unlock_requests = db.relationship(
        "UnlockRequest", backref="user", lazy="dynamic",
        cascade="all, delete-orphan",
    )

    def __repr__(self):
        return (
            f"<User {self.email} admin={self.is_admin} "
            f"locked={self.is_locked}>"
        )


class UnlockRequest(db.Model):
    __tablename__ = "unlock_requests"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    status = db.Column(
        db.String(16), nullable=False, default="pending", index=True,
    )  # pending | approved | denied
    message = db.Column(db.Text, default="")
    admin_response = db.Column(db.Text, default="")
    created_at = db.Column(db.DateTime, nullable=False, default=_utcnow)

    def __repr__(self):
        return f"<UnlockRequest user={self.user_id} status={self.status}>"


class LoginLog(db.Model):
    __tablename__ = "login_logs"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    action = db.Column(db.String(32), nullable=False)
    ip_address = db.Column(db.String(45), default="")
    details = db.Column(db.Text, default="")
    timestamp = db.Column(
        db.DateTime, nullable=False, default=_utcnow, index=True,
    )

    def __repr__(self):
        return f"<LoginLog user={self.user_id} action={self.action}>"


class ContactMessage(db.Model):
    __tablename__ = "contact_messages"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), nullable=False)
    email = db.Column(db.String(255), nullable=False)
    subject = db.Column(db.String(255), nullable=False, default="")
    message = db.Column(db.Text, nullable=False)
    is_read = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=_utcnow)

    def __repr__(self):
        return f"<ContactMessage {self.name} ({self.email}) read={self.is_read}>"


class Transcription(db.Model):
    """
    Stores a transcription with word-level timestamps for
    the synchronized audio player feature.

    ``words_json`` is a JSON string of:
        ``[{"word": str, "start": float, "end": float, "index": int}, ...]``
    """
    __tablename__ = "transcriptions"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    audio_filename = db.Column(db.String(255), nullable=False)
    audio_duration = db.Column(db.Float, nullable=False, default=0.0)
    words_json = db.Column(db.Text, nullable=False, default="[]")
    original_text = db.Column(db.Text, nullable=False, default="")
    edited_text = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=_utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=_utcnow, onupdate=_utcnow)

    # relationship
    user = db.relationship("User", backref="transcriptions", lazy="select")

    def __repr__(self):
        return f"<Transcription #{self.id} file={self.audio_filename}>"
