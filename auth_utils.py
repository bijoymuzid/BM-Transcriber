"""Utility functions, decorators, and helpers for the authentication system."""

import os
import sys
from datetime import datetime
from functools import wraps

from flask import abort, flash, redirect, request, url_for
from flask_login import current_user

from auth_models import LoginLog


# ---------------------------------------------------------------------------
# Console-based notification helpers
# ---------------------------------------------------------------------------

def send_unlock_notification(admin_email: str, user_email: str,
                             request_id: int, message: str) -> None:
    """Print the unlock request notification to the server console."""
    admin_url = url_for("auth.admin_unlock_requests", _external=True)
    print("=" * 60)
    print("  UNLOCK REQUEST #{}".format(request_id))
    print("=" * 60)
    print(f"  From: {user_email}")
    print(f"  Message: {message or 'No message provided'}")
    print(f"  Admin panel: {admin_url}")
    print("=" * 60)


def send_contact_notification(contact) -> None:
    """Send an email to the admin about a new contact message."""
    from flask_mail import Message
    from extensions import mail

    admin_email = os.getenv("MAIL_USERNAME", "")

    msg = Message(
        subject=f"[Contact Form] {contact.subject or 'No Subject'}",
        recipients=[admin_email],
        reply_to=contact.email,
        body=(
            f"New contact message from {contact.name} ({contact.email})\n\n"
            f"---\n{contact.message}\n---\n\n"
            f"Submitted at: {contact.created_at.strftime('%Y-%m-%d %H:%M:%S UTC')}"
        ),
    )
    try:
        mail.send(msg)
        print(f"[Contact] Email sent to admin from {contact.email}")
    except Exception as e:
        print(f"[Contact] Failed to send email: {e}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Login-log helper
# ---------------------------------------------------------------------------

def log_action(user_id: int, action: str, ip_address: str = "",
               details: str = "") -> None:
    """Persist an auditable login-log entry."""
    from auth_models import db

    entry = LoginLog(
        user_id=user_id,
        action=action,
        ip_address=ip_address or request.remote_addr or "",
        details=details,
        timestamp=datetime.utcnow(),
    )
    db.session.add(entry)
    db.session.commit()


# ---------------------------------------------------------------------------
# Decorators
# ---------------------------------------------------------------------------

def admin_required(fn):
    """Decorator: route only accessible to admin users."""

    @wraps(fn)
    def decorated_view(*args, **kwargs):
        if not current_user.is_authenticated:
            flash("Please log in first.", "warning")
            return redirect(url_for("auth.login"))
        if not current_user.is_admin:
            abort(403)
        return fn(*args, **kwargs)

    return decorated_view


def login_required(fn):
    """Decorator: route only accessible to logged-in users."""

    @wraps(fn)
    def decorated_view(*args, **kwargs):
        if not current_user.is_authenticated:
            flash("Please log in first.", "warning")
            return redirect(url_for("auth.login"))
        return fn(*args, **kwargs)

    return decorated_view
