"""
app.py — Nexora Pips Telegram CRM v2
Flask web application with:
  - Admin user login (Flask-Login)
  - Image + caption broadcasts
  - Link-in-message support
  - Admin user management
  - cPanel / Passenger compatible
"""

import os
import uuid
from flask import (
    Flask, render_template, request, redirect,
    url_for, flash, jsonify, send_from_directory,
)
from flask_login import (
    LoginManager, UserMixin, login_user,
    logout_user, login_required, current_user,
)
from dotenv import load_dotenv
from database import Database
from telegram_client import TelegramManager

load_dotenv()

# ──────────────────────── App & config ─────────────────────────────

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "nexora-change-this-key")

BASE_DIR      = os.path.dirname(os.path.abspath(__file__))
UPLOAD_FOLDER = os.path.join(BASE_DIR, "uploads")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}
MAX_IMAGE_MB = int(os.getenv("MAX_IMAGE_MB", 10))
app.config["MAX_CONTENT_LENGTH"] = MAX_IMAGE_MB * 1024 * 1024

# ──────────────────────── Database & Telegram ──────────────────────

db = Database(os.path.join(BASE_DIR, "crm.db"))
tg = TelegramManager()

_tg_started = False

@app.before_request
def ensure_telegram_started():
    global _tg_started
    if not _tg_started:
        api_id   = os.getenv("API_ID")
        api_hash = os.getenv("API_HASH")
        session  = os.getenv("SESSION_NAME", "nexora_session")
        session_path = os.path.join(BASE_DIR, session)
        if api_id and api_hash:
            tg.start(session_path, int(api_id), api_hash)
        _tg_started = True

# ──────────────────────── Flask-Login ──────────────────────────────

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"
login_manager.login_message = "Please log in to access the CRM."
login_manager.login_message_category = "warning"

class AdminUser(UserMixin):
    def __init__(self, id, username, role):
        self.id       = str(id)
        self.username = username
        self.role     = role

@login_manager.user_loader
def load_user(user_id):
    admin = db.get_admin_by_id(int(user_id))
    if admin:
        return AdminUser(admin["id"], admin["username"], admin["role"])
    return None

# ──────────────────────── Context processor ────────────────────────

@app.context_processor
def inject_globals():
    try:
        tg_ok = tg.is_authorized()
    except Exception:
        tg_ok = False
    return {"tg_authorized": tg_ok, "max_image_mb": MAX_IMAGE_MB}

# ──────────────────────── Helpers ──────────────────────────────────

def allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS

def save_upload(file) -> str | None:
    """Save uploaded image; return filename or None."""
    if file and file.filename and allowed_file(file.filename):
        ext      = file.filename.rsplit(".", 1)[1].lower()
        filename = f"{uuid.uuid4().hex}.{ext}"
        file.save(os.path.join(UPLOAD_FOLDER, filename))
        return filename
    return None

# ──────────────────────── Auth routes ──────────────────────────────

@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))

    first_run = db.admin_count() == 0

    if request.method == "POST":
        action = request.form.get("action", "")

        if action == "create_superadmin":
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "").strip()
            confirm  = request.form.get("confirm",  "").strip()
            if not username or not password:
                flash("Username and password are required.", "danger")
            elif password != confirm:
                flash("Passwords do not match.", "danger")
            elif len(password) < 6:
                flash("Password must be at least 6 characters.", "danger")
            else:
                db.create_admin(username, password, role="superadmin")
                flash(f"Super-admin '{username}' created. Please log in.", "success")
                return redirect(url_for("login"))

        else:  # normal login
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "").strip()
            admin    = db.verify_admin(username, password)
            if admin:
                user = AdminUser(admin["id"], admin["username"], admin["role"])
                login_user(user, remember=True)
                next_page = request.args.get("next")
                return redirect(next_page or url_for("dashboard"))
            else:
                flash("Invalid username or password.", "danger")

    return render_template("login.html", first_run=first_run)


@app.route("/logout")
@login_required
def logout():
    logout_user()
    flash("Logged out successfully.", "info")
    return redirect(url_for("login"))

# ──────────────────────── Dashboard ────────────────────────────────

@app.route("/")
@login_required
def dashboard():
    stats             = db.get_stats()
    recent_broadcasts = db.get_recent_broadcasts(5)
    return render_template("dashboard.html",
                           stats=stats,
                           recent_broadcasts=recent_broadcasts)

# ──────────────────────── Clients ──────────────────────────────────

@app.route("/clients")
@login_required
def clients():
    rows = db.get_all_clients()
    return render_template("clients.html", clients=rows)


@app.route("/clients/add", methods=["POST"])
@login_required
def add_client():
    raw         = request.form.get("telegram_id", "").strip().lstrip("@")
    name        = request.form.get("name",  "").strip()
    notes       = request.form.get("notes", "").strip()

    if not raw:
        flash("Provide a Telegram username or numeric ID.", "danger")
        return redirect(url_for("clients"))

    # Decide: numeric ID or username?
    if raw.isdigit():
        telegram_id = raw
        username    = ""
    else:
        username    = raw
        telegram_id = ""
        # Try to resolve numeric ID via Telegram
        if tg.is_authorized():
            info = tg.get_entity_info(f"@{username}")
            if info:
                telegram_id = str(info["id"])
                if not name:
                    name = info.get("name") or username
            else:
                flash(f"Could not resolve @{username}. They must have chatted with you before.", "danger")
                return redirect(url_for("clients"))
        else:
            # Store by username only; Telegram resolves at broadcast time
            telegram_id = username

    added = db.add_client(telegram_id, username, name, notes)
    flash(
        f"Client '{name or username or telegram_id}' added." if added else "Client already exists.",
        "success" if added else "warning"
    )
    return redirect(url_for("clients"))


@app.route("/clients/delete/<int:client_id>")
@login_required
def delete_client(client_id):
    db.delete_client(client_id)
    flash("Client removed.", "success")
    return redirect(url_for("clients"))


@app.route("/clients/toggle/<int:client_id>")
@login_required
def toggle_client(client_id):
    db.toggle_client_status(client_id)
    return redirect(url_for("clients"))

# ──────────────────────── Broadcast ────────────────────────────────

@app.route("/broadcast")
@login_required
def broadcast():
    active_clients = db.get_active_client_count()
    return render_template("broadcast.html", active_clients=active_clients)


@app.route("/broadcast/send", methods=["POST"])
@login_required
def send_broadcast():
    message    = request.form.get("message", "").strip()
    image_file = request.files.get("image")

    if not message and (not image_file or not image_file.filename):
        flash("Please add a message or an image (or both).", "danger")
        return redirect(url_for("broadcast"))

    if not tg.is_authorized():
        flash("Telegram is not connected. Complete setup first.", "warning")
        return redirect(url_for("setup"))

    active_clients = db.get_active_clients()
    if not active_clients:
        flash("No active clients to send to.", "warning")
        return redirect(url_for("broadcast"))

    media_filename = save_upload(image_file) if image_file and image_file.filename else None
    media_path     = os.path.join(UPLOAD_FOLDER, media_filename) if media_filename else None

    delay        = float(request.form.get("delay", os.getenv("BROADCAST_DELAY", 4)))
    broadcast_id = db.create_broadcast(
        message    = message,
        total      = len(active_clients),
        media_path = media_filename,
        sent_by    = current_user.username,
    )

    tg.run_broadcast(broadcast_id, active_clients, message, db, delay, media_path)

    flash(
        f"Broadcast #{broadcast_id} started — sending to {len(active_clients)} client(s). "
        + ("📷 Includes image. " if media_path else "")
        + "Check logs for live progress.",
        "success",
    )
    return redirect(url_for("log_detail", broadcast_id=broadcast_id))

# ──────────────────────── Uploaded images ──────────────────────────

@app.route("/uploads/<filename>")
@login_required
def uploaded_file(filename):
    return send_from_directory(UPLOAD_FOLDER, filename)

# ──────────────────────── Logs ─────────────────────────────────────

@app.route("/logs")
@login_required
def logs():
    broadcasts = db.get_all_broadcasts()
    return render_template("logs.html", broadcasts=broadcasts)


@app.route("/logs/<int:broadcast_id>")
@login_required
def log_detail(broadcast_id):
    bc   = db.get_broadcast(broadcast_id)
    logs = db.get_broadcast_details(broadcast_id)
    return render_template("log_detail.html", bc=bc, logs=logs)


@app.route("/api/broadcast/<int:broadcast_id>/progress")
@login_required
def api_broadcast_progress(broadcast_id):
    return jsonify(db.get_broadcast(broadcast_id))

# ──────────────────────── Admin users ──────────────────────────────

@app.route("/admin-users")
@login_required
def admin_users():
    if current_user.role != "superadmin":
        flash("Only super-admins can manage admin accounts.", "danger")
        return redirect(url_for("dashboard"))
    admins = db.get_all_admins()
    return render_template("admin_users.html", admins=admins)


@app.route("/admin-users/add", methods=["POST"])
@login_required
def add_admin():
    if current_user.role != "superadmin":
        flash("Permission denied.", "danger")
        return redirect(url_for("dashboard"))
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "").strip()
    role     = request.form.get("role", "admin")

    if not username or not password:
        flash("Username and password required.", "danger")
    elif len(password) < 6:
        flash("Password must be at least 6 characters.", "danger")
    elif role not in ("admin", "superadmin"):
        flash("Invalid role.", "danger")
    else:
        added = db.create_admin(username, password, role)
        flash(
            f"Admin '{username}' created." if added else f"Username '{username}' already taken.",
            "success" if added else "warning"
        )
    return redirect(url_for("admin_users"))


@app.route("/admin-users/delete/<int:admin_id>")
@login_required
def delete_admin(admin_id):
    if current_user.role != "superadmin":
        flash("Permission denied.", "danger")
        return redirect(url_for("dashboard"))
    if str(admin_id) == current_user.id:
        flash("You cannot delete your own account.", "warning")
    else:
        db.delete_admin(admin_id)
        flash("Admin removed.", "success")
    return redirect(url_for("admin_users"))


@app.route("/admin-users/change-password", methods=["POST"])
@login_required
def change_admin_password():
    if current_user.role != "superadmin":
        flash("Permission denied.", "danger")
        return redirect(url_for("dashboard"))

    admin_id    = int(request.form.get("admin_id", 0))
    new_pw      = request.form.get("new_password", "").strip()
    confirm     = request.form.get("confirm", "").strip()

    if len(new_pw) < 6:
        flash("Password must be at least 6 characters.", "danger")
    elif new_pw != confirm:
        flash("Passwords do not match.", "danger")
    else:
        db.change_password(admin_id, new_pw)
        flash("Password updated.", "success")
    return redirect(url_for("admin_users"))

# ──────────────────────── Setup ────────────────────────────────────

@app.route("/setup")
@login_required
def setup():
    from flask import session as flask_session
    authorized   = tg.is_authorized()
    me           = tg.get_me() if authorized else None
    api_id       = bool(os.getenv("API_ID"))
    api_hash     = bool(os.getenv("API_HASH"))
    session_name = os.getenv("SESSION_NAME", "nexora_session")
    tg_step      = flask_session.get("tg_step", "")
    tg_phone     = flask_session.get("tg_phone", "")
    return render_template("setup.html",
                           authorized=authorized,
                           me=me,
                           api_id=api_id,
                           api_hash=api_hash,
                           session_name=session_name,
                           tg_step=tg_step,
                           tg_phone=tg_phone)


@app.route("/setup/connect/send-code", methods=["POST"])
@login_required
def setup_send_code():
    """Step 1 — user submits phone number, we fire off the OTP."""
    phone = request.form.get("phone", "").strip()
    if not phone:
        flash("Please enter your phone number.", "danger")
        return redirect(url_for("setup"))

    result = tg.send_code(phone)
    if result["ok"]:
        # Store transient data in Flask session (server-side cookie)
        from flask import session as flask_session
        flask_session["tg_phone"]           = phone
        flask_session["tg_phone_code_hash"] = result["phone_code_hash"]
        flask_session["tg_step"]            = "otp"
        flash(f"OTP sent to {phone}. Check your Telegram app.", "success")
    else:
        flash(f"Failed to send code: {result['error']}", "danger")
    return redirect(url_for("setup"))


@app.route("/setup/connect/verify-code", methods=["POST"])
@login_required
def setup_verify_code():
    """Step 2 — user submits the OTP code."""
    from flask import session as flask_session
    phone           = flask_session.get("tg_phone", "")
    phone_code_hash = flask_session.get("tg_phone_code_hash", "")
    code            = request.form.get("code", "").strip()

    if not phone or not phone_code_hash:
        flash("Session expired. Please start over.", "warning")
        return redirect(url_for("setup"))

    result = tg.sign_in(phone, code, phone_code_hash)

    if result["ok"]:
        flask_session.pop("tg_step", None)
        flash("Telegram connected successfully! You can now send broadcasts.", "success")

    elif result.get("need_password"):
        flask_session["tg_step"] = "2fa"
        flash("2FA is enabled on this account. Please enter your Telegram password.", "info")

    else:
        flash(f"Login failed: {result['error']}", "danger")

    return redirect(url_for("setup"))


@app.route("/setup/connect/verify-2fa", methods=["POST"])
@login_required
def setup_verify_2fa():
    """Step 3 (optional) — user submits 2FA password."""
    from flask import session as flask_session
    password = request.form.get("password", "").strip()

    result = tg.sign_in_2fa(password)
    if result["ok"]:
        flask_session.pop("tg_step", None)
        flash("Telegram connected with 2FA. You're all set!", "success")
    else:
        flash(f"2FA failed: {result['error']}", "danger")

    return redirect(url_for("setup"))


@app.route("/setup/logout-telegram", methods=["POST"])
@login_required
def logout_telegram():
    """Disconnect the Telegram session (delete session file)."""
    session_name = os.getenv("SESSION_NAME", "nexora_session")
    session_path = os.path.join(BASE_DIR, session_name + ".session")
    try:
        tg.stop()
        if os.path.exists(session_path):
            os.remove(session_path)
        flash("Telegram session disconnected. Run setup_telegram.py to re-authorize.", "info")
    except Exception as e:
        flash(f"Error disconnecting: {e}", "danger")
    global _tg_started
    _tg_started = False
    return redirect(url_for("setup"))

# ──────────────────────── Entry point ──────────────────────────────

if __name__ == "__main__":
    print("\n  Nexora Pips Telegram CRM v2")
    print("  Running at http://127.0.0.1:5000\n")
    app.run(debug=False, host="127.0.0.1", port=5000)
