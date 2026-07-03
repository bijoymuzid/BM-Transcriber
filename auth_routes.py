"""Flask Blueprint — authentication system (IP-based locking, no email verification)."""

from datetime import datetime

from flask import (
    Blueprint, flash, redirect, render_template, request,
    session as flask_session, url_for,
)
from flask_bcrypt import Bcrypt
from flask_limiter import Limiter
from flask_limiter.errors import RateLimitExceeded
from flask_limiter.util import get_remote_address
from flask_login import (
    LoginManager, current_user, login_required as fl_login_required,
    login_user, logout_user,
)
from flask_wtf.csrf import CSRFProtect, generate_csrf

from auth_models import ContactMessage, LoginLog, UnlockRequest, User, db
from auth_utils import (
    admin_required, log_action, login_required,
    send_contact_notification, send_unlock_notification,
)

# ---------------------------------------------------------------------------
# Extensions (initialised in app.py)
# ---------------------------------------------------------------------------

bcrypt = Bcrypt()
login_manager = LoginManager()
limiter = Limiter(key_func=get_remote_address, default_limits=["200 per day"])
csrf = CSRFProtect()

auth_bp = Blueprint("auth", __name__, url_prefix="")

# Hardcoded admin email — only this email gets admin role
ADMIN_EMAIL = ""


# ---------------------------------------------------------------------------
# Flask-Login loader
# ---------------------------------------------------------------------------

@login_manager.user_loader
def load_user(user_id: str):
    return db.session.get(User, int(user_id))


@login_manager.unauthorized_handler
def unauthorized():
    flash("Please log in to access this page.", "warning")
    return redirect(url_for("auth.login"))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_user_by_email(email: str) -> User | None:
    return User.query.filter_by(email=email.lower().strip()).first()


# ---------------------------------------------------------------------------
# Register — no email verification needed
# ---------------------------------------------------------------------------

@auth_bp.route("/register", methods=["GET", "POST"])
@limiter.limit("3 per hour")
def register():
    if current_user.is_authenticated:
        return redirect(url_for("index"))

    if request.method == "GET":
        return render_template("register.html", csrf_token=generate_csrf())

    # POST
    email = request.form.get("email", "").lower().strip()
    password = request.form.get("password", "")
    confirm = request.form.get("confirm_password", "")

    if not email or "@" not in email:
        flash("Valid email address is required.", "danger")
        return render_template("register.html", csrf_token=generate_csrf())

    if len(password) < 8:
        flash("Password must be at least 8 characters.", "danger")
        return render_template("register.html", csrf_token=generate_csrf())

    if password != confirm:
        flash("Passwords do not match.", "danger")
        return render_template("register.html", csrf_token=generate_csrf())

    if _get_user_by_email(email):
        flash("Email address is already registered.", "danger")
        return render_template("register.html", csrf_token=generate_csrf())

    # Create user — hardcoded admin check
    is_admin = email == ADMIN_EMAIL
    user = User(
        email=email,
        password_hash=bcrypt.generate_password_hash(password).decode("utf-8"),
        is_admin=is_admin,
        is_locked=False,
        ip_address="",  # will be set on first login
        created_at=datetime.utcnow(),
    )
    db.session.add(user)
    db.session.commit()

    log_action(
        user_id=user.id,
        action="register",
        details=f"Admin: {is_admin}",
    )

    if is_admin:
        flash("Admin account created! You can now log in.", "success")
    else:
        flash("Account created! You can now log in.", "success")

    return redirect(url_for("auth.login"))


# ---------------------------------------------------------------------------
# Login — with IP locking check
# ---------------------------------------------------------------------------

@auth_bp.route("/login", methods=["GET", "POST"])
@limiter.limit("5 per minute")
def login():
    if current_user.is_authenticated:
        return redirect(url_for("index"))

    if request.method == "GET":
        return render_template("login.html", csrf_token=generate_csrf())

    # POST
    email = request.form.get("email", "").lower().strip()
    password = request.form.get("password", "")

    user = _get_user_by_email(email)
    if not user:
        flash("Invalid email or password.", "danger")
        log_action(user_id=0, action="login_failed",
                   details=f"Unknown email: {email}")
        return render_template("login.html", csrf_token=generate_csrf())

    # --- Verify password ---
    if not bcrypt.check_password_hash(user.password_hash, password):
        flash("Invalid email or password.", "danger")
        log_action(user_id=user.id, action="login_failed",
                   details="Wrong password")
        return render_template("login.html", csrf_token=generate_csrf())

    # --- Check if locked ---
    if user.is_locked:
        flash(
            "Your account has been locked due to login from an unrecognised IP "
            "address. Use the 'Request Unlock' option below to contact the admin.",
            "danger",
        )
        return render_template("login.html", csrf_token=generate_csrf())

    # --- IP lock check ---
    client_ip = request.remote_addr or ""
    if user.ip_address and user.ip_address != client_ip:
        # Different IP → lock immediately
        user.is_locked = True
        db.session.commit()

        log_action(
            user_id=user.id,
            action="lock",
            ip_address=client_ip,
            details=(
                f"Auto-locked: login from new IP {client_ip} "
                f"(original was {user.ip_address})"
            ),
        )
        flash(
            "Your account has been locked due to login from an unrecognised "
            "IP address. Use the 'Request Unlock' option below to contact "
            "the admin.",
            "danger",
        )
        return render_template("login.html", csrf_token=generate_csrf())

    # --- First successful login: store IP ---
    if not user.ip_address:
        user.ip_address = client_ip
        db.session.commit()

    # Log in
    login_user(user, remember=False)

    log_action(
        user_id=user.id,
        action="login",
        ip_address=client_ip,
    )

    flash("Logged in successfully.", "success")

    next_page = request.args.get("next")
    if next_page:
        return redirect(next_page)
    return redirect(url_for("index"))


# ---------------------------------------------------------------------------
# Logout
# ---------------------------------------------------------------------------

@auth_bp.route("/logout")
@fl_login_required
def logout():
    log_action(
        user_id=current_user.id,
        action="logout",
        ip_address=request.remote_addr or "",
    )
    logout_user()
    flash("You have been logged out.", "info")
    return redirect(url_for("auth.login"))


# ---------------------------------------------------------------------------
# Unlock request (user-facing)
# ---------------------------------------------------------------------------

@auth_bp.route("/request-unlock", methods=["GET", "POST"])
def request_unlock():
    if request.method == "GET":
        return render_template(
            "unlock_request.html",
            csrf_token=generate_csrf(),
            admin_email=ADMIN_EMAIL,
        )

    # POST
    email = request.form.get("email", "").lower().strip()
    message = request.form.get("message", "").strip()

    user = _get_user_by_email(email)
    if not user:
        flash("No account found with that email address.", "danger")
        return redirect(url_for("auth.request_unlock"))

    if not user.is_locked:
        flash("Your account is not locked. You can try logging in.", "info")
        return redirect(url_for("auth.login"))

    # Create unlock request
    ureq = UnlockRequest(
        user_id=user.id,
        status="pending",
        message=message or "No message provided.",
    )
    db.session.add(ureq)
    db.session.commit()

    log_action(
        user_id=user.id,
        action="unlock_requested",
        ip_address=request.remote_addr or "",
        details=f"Unlock request #{ureq.id} created: {message[:200]}",
    )

    # Notify admin via console
    try:
        send_unlock_notification(ADMIN_EMAIL, user.email, ureq.id, message)
    except Exception:
        pass  # non-blocking

    return redirect(url_for("auth.unlock_request_sent"))


@auth_bp.route("/unlock-request-sent")
def unlock_request_sent():
    return render_template("unlock_request_sent.html", admin_email=ADMIN_EMAIL)


# ---------------------------------------------------------------------------
# Contact — Public contact form
# ---------------------------------------------------------------------------

@auth_bp.route("/contact", methods=["GET", "POST"])
@limiter.limit("3 per hour")
def contact():
    if request.method == "GET":
        return render_template("contact.html", csrf_token=generate_csrf())

    # POST
    name = request.form.get("name", "").strip()
    email = request.form.get("email", "").strip()
    subject = request.form.get("subject", "").strip()
    message = request.form.get("message", "").strip()

    # Validation
    errors = []
    if not name or len(name) > 255:
        errors.append("Name is required (max 255 characters).")
    if not email or "@" not in email:
        errors.append("A valid email address is required.")
    if not message or len(message) < 10:
        errors.append("Message must be at least 10 characters.")
    if len(subject) > 255:
        errors.append("Subject must be under 255 characters.")

    if errors:
        for err in errors:
            flash(err, "danger")
        return render_template("contact.html", csrf_token=generate_csrf())

    # Save to database
    contact_msg = ContactMessage(
        name=name,
        email=email,
        subject=subject,
        message=message,
        is_read=False,
    )
    db.session.add(contact_msg)
    db.session.commit()

    # Notify admin via email (non-blocking)
    try:
        send_contact_notification(contact_msg)
    except Exception:
        pass

    log_action(
        user_id=current_user.id if current_user.is_authenticated else 0,
        action="contact",
        ip_address=request.remote_addr or "",
        details=f"Contact from {name} ({email}): {subject[:100]}",
    )

    flash("Your message has been sent! We'll get back to you shortly.", "success")
    return redirect(url_for("auth.contact"))


# ---------------------------------------------------------------------------
# Admin — Dashboard
# ---------------------------------------------------------------------------

@auth_bp.route("/admin/dashboard")
@login_required
@admin_required
def admin_dashboard():
    total_users = User.query.count()
    locked_users = User.query.filter_by(is_locked=True).count()
    pending_requests = UnlockRequest.query.filter_by(status="pending").count()
    unread_messages = ContactMessage.query.filter_by(is_read=False).count()
    recent_logs = (
        LoginLog.query
        .order_by(LoginLog.timestamp.desc())
        .limit(20)
        .all()
    )

    import os
    api_key = os.getenv("OPENROUTER_API_KEY", "").strip()
    api_key_configured = bool(api_key)
    api_key_masked = api_key[:8] + "..." + api_key[-4:] if len(api_key) > 12 else ""

    return render_template(
        "admin/dashboard.html",
        total_users=total_users,
        locked_users=locked_users,
        pending_requests=pending_requests,
        unread_messages=unread_messages,
        recent_logs=recent_logs,
        api_key_configured=api_key_configured,
        api_key_masked=api_key_masked,
    )


# ---------------------------------------------------------------------------
# Admin — User management
# ---------------------------------------------------------------------------

@auth_bp.route("/admin/users")
@login_required
@admin_required
def admin_users():
    all_users = User.query.order_by(User.created_at.asc()).all()
    return render_template(
        "admin/users.html",
        users=all_users,
        csrf_token=generate_csrf(),
    )


@auth_bp.route("/admin/users/<int:user_id>/unlock", methods=["POST"])
@login_required
@admin_required
def admin_unlock_user(user_id: int):
    user = db.session.get(User, user_id)
    if not user:
        flash("User not found.", "danger")
        return redirect(url_for("auth.admin_users"))

    if not user.is_locked:
        flash(f"User {user.email} is not locked.", "info")
        return redirect(url_for("auth.admin_users"))

    user.is_locked = False
    db.session.commit()

    log_action(
        user_id=user.id,
        action="unlock",
        ip_address=request.remote_addr or "",
        details=f"Unlocked by admin {current_user.email}",
    )
    flash(f"User {user.email} has been unlocked.", "success")
    return redirect(url_for("auth.admin_users"))


# ---------------------------------------------------------------------------
# Admin — Set user API key
# ---------------------------------------------------------------------------

@auth_bp.route("/admin/users/<int:user_id>/set-api-key", methods=["POST"])
@login_required
@admin_required
def admin_set_user_api_key(user_id: int):
    user = db.session.get(User, user_id)
    if not user:
        flash("User not found.", "danger")
        return redirect(url_for("auth.admin_users"))

    new_key = request.form.get("api_key", "").strip()
    user.api_key = new_key
    db.session.commit()

    log_action(
        user_id=user.id,
        action="api_key_updated",
        ip_address=request.remote_addr or "",
        details=f"API key set by admin {current_user.email}",
    )

    if new_key:
        flash(f"API key set for {user.email}.", "success")
    else:
        flash(f"API key cleared for {user.email}.", "info")
    return redirect(url_for("auth.admin_users"))


# ---------------------------------------------------------------------------
# User — Profile (view personal API key)
# ---------------------------------------------------------------------------

@auth_bp.route("/profile")
@login_required
def profile():
    return render_template("profile.html")


# ---------------------------------------------------------------------------
# Admin — Unlock requests
# ---------------------------------------------------------------------------

@auth_bp.route("/admin/unlock-requests")
@login_required
@admin_required
def admin_unlock_requests():
    pending = (
        UnlockRequest.query
        .filter_by(status="pending")
        .order_by(UnlockRequest.created_at.desc())
        .all()
    )
    history = (
        UnlockRequest.query
        .filter(UnlockRequest.status.in_(["approved", "denied"]))
        .order_by(UnlockRequest.created_at.desc())
        .limit(50)
        .all()
    )
    return render_template("admin/unlock_requests.html", pending=pending, history=history)


@auth_bp.route("/admin/unlock/<int:request_id>/approve", methods=["POST"])
@login_required
@admin_required
def admin_approve_unlock(request_id: int):
    ureq = db.session.get(UnlockRequest, request_id)
    if not ureq or ureq.status != "pending":
        flash("Unlock request not found or already processed.", "danger")
        return redirect(url_for("auth.admin_unlock_requests"))

    ureq.status = "approved"
    ureq.admin_response = f"Approved by {current_user.email}"
    ureq.user.is_locked = False
    db.session.commit()

    log_action(
        user_id=ureq.user_id,
        action="unlock",
        ip_address=request.remote_addr or "",
        details=f"Approved via unlock request #{ureq.id} by {current_user.email}",
    )
    flash(f"User {ureq.user.email} has been unlocked.", "success")
    return redirect(url_for("auth.admin_unlock_requests"))


@auth_bp.route("/admin/unlock/<int:request_id>/deny", methods=["POST"])
@login_required
@admin_required
def admin_deny_unlock(request_id: int):
    ureq = db.session.get(UnlockRequest, request_id)
    if not ureq or ureq.status != "pending":
        flash("Unlock request not found or already processed.", "danger")
        return redirect(url_for("auth.admin_unlock_requests"))

    ureq.status = "denied"
    ureq.admin_response = f"Denied by {current_user.email}"
    db.session.commit()

    log_action(
        user_id=ureq.user_id,
        action="unlock_denied",
        ip_address=request.remote_addr or "",
        details=f"Unlock request #{ureq.id} denied by {current_user.email}",
    )
    flash(f"Unlock request #{ureq.id} has been denied.", "info")
    return redirect(url_for("auth.admin_unlock_requests"))


# ---------------------------------------------------------------------------
# Admin — Login logs
# ---------------------------------------------------------------------------

@auth_bp.route("/admin/logs")
@login_required
@admin_required
def admin_logs():
    page = request.args.get("page", 1, type=int)
    per_page = 50
    logs_pagination = (
        LoginLog.query
        .order_by(LoginLog.timestamp.desc())
        .paginate(page=page, per_page=per_page, error_out=False)
    )
    return render_template("admin/logs.html", logs=logs_pagination)


# ---------------------------------------------------------------------------
# Admin — Contact messages
# ---------------------------------------------------------------------------

@auth_bp.route("/admin/contact-messages")
@login_required
@admin_required
def admin_contact_messages():
    page = request.args.get("page", 1, type=int)
    per_page = 20
    messages_pagination = (
        ContactMessage.query
        .order_by(ContactMessage.created_at.desc())
        .paginate(page=page, per_page=per_page, error_out=False)
    )

    # Mark all fetched messages as read
    for msg in messages_pagination.items:
        if not msg.is_read:
            msg.is_read = True
    db.session.commit()

    return render_template(
        "admin/contact_messages.html",
        messages=messages_pagination,
    )


# ---------------------------------------------------------------------------
# Rate-limit exceeded handler
# ---------------------------------------------------------------------------

@auth_bp.errorhandler(RateLimitExceeded)
def handle_rate_limit(exc):
    flash("Too many attempts. Please wait before trying again.", "warning")
    # Redirect back to the page they were on (or to login as fallback)
    return redirect(request.referrer or url_for("auth.login"))
