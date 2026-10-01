"""
Phase 9 - Admin console & policy server.

The one always-on, organization-visible piece Phase 9 adds. Two audiences
share this process:

  1. Hosts (desktop/admin_client.py, wired in via host_p9.py) - a small
     JSON API under /api/v1/ that a host uses to enroll once, fetch its
     effective policy (refreshed periodically without a restart), and
     report session attempt/start/end events for the dashboard.
  2. Admins - a server-rendered web console (session-cookie login) to
     manage operator accounts, group hosts under a policy, and browse
     session history.

This is deliberately not a full IT-deployment story - no MSI packaging,
no auto-update, no branding (that's Phase 10, see docs/roadmap.md). It's
the visibility and policy layer those later phases attach to.

Run it with:
    pip install -r requirements.txt
    python3 server.py --port 8443

On first run it prompts (on the console, once) for the first operator
account and prints a one-time org enrollment key - hosts need that key
for their *first* --admin-url run only; after that they hold their own
report_token and never need the enrollment key again.

Not production-hardened: Flask's built-in server is fine for a LAN or a
single small deployment behind your own TLS-terminating reverse proxy,
same spirit as relay.py being a "dumb" always-works fallback rather than
an optimized one. Don't expose this directly to the public internet
without putting real TLS and rate limiting in front of it.
"""

import argparse
import getpass
import hmac
import os
import secrets
import sys
import time
from functools import wraps
from urllib.parse import urlparse

from flask import (Flask, g, make_response, redirect, render_template, request, session,
                    url_for, jsonify, abort, flash)
from markupsafe import Markup, escape

import i18n
import store
import policy as policy_mod
import relayauth
import wol

app = Flask(__name__)

# Hosting behind a TLS-terminating proxy (Render, Fly, nginx...): trust one hop of X-Forwarded-*
# so request.remote_addr / url_for() see the real client and https, and mark the session cookie
# Secure. Both are opt-in via env so a LAN deployment behaves exactly as before.
if os.environ.get("REMOTEBRIDGE_BEHIND_PROXY") == "1":
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("REMOTEBRIDGE_SECURE_COOKIES") == "1",
)
_DB_PATH = store.DB_PATH
# Phase 11: where Wake-on-LAN magic packets are broadcast. The limited-broadcast default only
# leaves via the machine's default interface; on a multi-homed server, pass the target subnet's
# directed broadcast (e.g. 192.168.1.255) with --wol-broadcast.
_WOL_BROADCAST = "255.255.255.255"
_WOL_PORT = 9


@app.route("/healthz")
def healthz():
    """Liveness probe for Render & friends. Deliberately does NOT touch the database: a probe
    that queries Neon every few seconds would keep a scale-to-zero database awake 24/7."""
    return "ok", 200, {"Content-Type": "text/plain", "Cache-Control": "no-store"}


# --- Per-request DB connection (Flask's standard per-request pattern) ---

def get_db():
    if "db" not in g:
        g.db = store.get_conn(_DB_PATH)
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


# --- Web console auth ------------------------------------------------------

def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login", next=request.path))
        if session.get("role") != "admin":
            abort(403)
        return view(*args, **kwargs)
    return wrapped


@app.context_processor
def inject_user():
    return {"current_user": {"username": session.get("username"), "role": session.get("role")},
            "branding": store.get_branding(get_db())}


# --- Phase 12: language, theme, CSRF -------------------------------------------

THEMES = ("auto", "contrast")     # auto = the default look, upgraded by prefers-contrast


def current_language() -> str:
    """Cookie choice, else the browser's Accept-Language, else English."""
    cookie = request.cookies.get("lang")
    if cookie:
        return i18n.normalize(cookie)
    return i18n.negotiate(request.headers.get("Accept-Language", ""))


def tr(message: str, **values) -> str:
    """Translate into the current request's language (for flash messages)."""
    return i18n.translate(message, current_language(), **values)


def csrf_token() -> str:
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_urlsafe(32)
    return session["csrf_token"]


def _html_translate(message: str, lang: str, values: dict) -> Markup:
    """For templates. The message is escaped, then placeholders are filled with
    Markup.format, which escapes plain values and passes Markup through - so a
    device name can never inject HTML, and a translation can still carry
    deliberate markup (e.g. <code>) supplied by the template itself."""
    try:
        return Markup(escape(i18n.lookup(message, lang))).format(**values)
    except (KeyError, IndexError, ValueError):
        return Markup(escape(message)).format(**values) if values else Markup(escape(message))


@app.context_processor
def inject_i18n_theme_csrf():
    lang = current_language()
    theme = request.cookies.get("theme", "auto")
    open_feedback = store.count_open_feedback(get_db()) if "user_id" in session else 0
    return {
        "_": lambda message, **values: _html_translate(message, lang, values),
        "locale": lang,
        "languages": i18n.available_languages(),
        "theme": theme if theme in THEMES else "auto",
        "csrf_input": lambda: Markup(
            f'<input type="hidden" name="csrf_token" value="{csrf_token()}">'),
        "open_feedback": open_feedback,
    }


@app.before_request
def enforce_csrf():
    """Every state-changing browser form must carry the session's token. The
    JSON API under /api/ is exempt on purpose: it authenticates with a
    bearer token in a header, which a cross-site form post cannot set (that
    is what CSRF protection is *for*). Before Phase 12 none of the console's
    own POST forms were protected."""
    if request.method != "POST" or request.path.startswith("/api/"):
        return None
    sent = request.form.get("csrf_token", "")
    expected = session.get("csrf_token", "")
    if not expected or not secrets.compare_digest(sent, expected):
        abort(400, description="Missing or invalid CSRF token - reload the page and try again.")
    return None


def _safe_next(target: str) -> str:
    """Only same-site relative paths - never bounce someone to another host."""
    # Browsers treat a backslash like a slash, so "/\\evil.com" is "//evil.com" to them.
    if (target and target.startswith("/") and not target.startswith("//") and "\\" not in target
            and not urlparse(target).netloc):
        return target
    return url_for("dashboard")


@app.route("/set-language")
def set_language():
    code = request.args.get("lang", "")
    response = redirect(_safe_next(request.args.get("next", "")))
    if code in i18n.available_languages() or code == i18n.PSEUDO_LANGUAGE:
        response.set_cookie("lang", code, max_age=365 * 86400, samesite="Lax")
    return response


@app.route("/set-theme")
def set_theme():
    choice = request.args.get("theme", "")
    response = redirect(_safe_next(request.args.get("next", "")))
    if choice in THEMES:
        response.set_cookie("theme", choice, max_age=365 * 86400, samesite="Lax")
    return response


# --- Sign-in: lockout, optional second factor ------------------------------------

REQUIRE_2FA = os.environ.get("REMOTEBRIDGE_REQUIRE_2FA") == "1"
PENDING_2FA_SECONDS = 300      # time allowed between a correct password and a correct code
PENDING_2FA_MAX_BAD = 5        # wrong codes before the person must start over with the password


def _client_address() -> str:
    return request.remote_addr or "unknown"


def _locked_response(template: str, wait: int, who: str):
    app.logger.warning("sign-in locked out: %s from %s for %ss", who, _client_address(), wait)
    flash(tr("Too many failed attempts. Try again in {minutes} min.", minutes=max(1, -(-wait // 60))), "error")
    response = make_response(render_template(template), 429)
    response.headers["Retry-After"] = str(wait)
    return response


def _finish_login(user: dict, next_url: str = ""):
    """Start the signed-in session. The old session is dropped first so nothing from before the
    sign-in (a planted cookie, a half-finished 2FA step) carries over."""
    session.clear()
    session["user_id"] = user["id"]
    session["username"] = user["username"]
    session["role"] = user["role"]
    if REQUIRE_2FA and not user.get("totp_enabled"):
        session["must_enroll_2fa"] = True
        return redirect(url_for("account_security"))
    return redirect(_safe_next(next_url))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method != "POST":
        return render_template("login.html")
    conn = get_db()
    username = request.form.get("username", "")
    keys = store.throttle_keys(username, _client_address())
    wait = store.throttle_retry_after(conn, keys)
    if wait:                       # checked before the password, so a lock can't be probed with guesses
        return _locked_response("login.html", wait, username[:60])
    user = store.verify_login(conn, username, request.form.get("password", ""))
    if user is None:
        store.throttle_record_failure(conn, keys)
        app.logger.info("failed sign-in for %r from %s", username[:60], _client_address())
        flash(tr("Incorrect username or password."), "error")
        return render_template("login.html")
    row = store.get_admin_user(conn, user["id"])
    if row and row["totp_enabled"]:
        # Password was right; the account isn't signed in until the code is too. The failure
        # counters are NOT reset here - only a complete sign-in clears them.
        session.clear()
        session["pending_2fa"] = {"uid": user["id"], "username": user["username"], "role": user["role"],
                                  "ts": time.time(), "bad": 0, "next": request.args.get("next", "")}
        return redirect(url_for("login_2fa"))
    store.throttle_record_success(conn, username)
    return _finish_login({**user, "totp_enabled": bool(row and row["totp_enabled"])},
                         request.args.get("next", ""))


@app.route("/login/2fa", methods=["GET", "POST"])
def login_2fa():
    pending = session.get("pending_2fa")
    if not pending or time.time() - pending["ts"] > PENDING_2FA_SECONDS:
        session.pop("pending_2fa", None)
        flash(tr("Your sign-in timed out. Enter your password again."), "error")
        return redirect(url_for("login"))
    if request.method != "POST":
        return render_template("login_2fa.html")
    conn = get_db()
    keys = store.throttle_keys(pending["username"], _client_address())
    wait = store.throttle_retry_after(conn, keys)
    if wait:
        return _locked_response("login_2fa.html", wait, pending["username"])
    how = store.verify_second_factor(conn, pending["uid"], request.form.get("code", ""))
    if not how:
        store.throttle_record_failure(conn, keys)
        pending["bad"] += 1
        app.logger.info("failed 2FA code for %r from %s", pending["username"], _client_address())
        if pending["bad"] >= PENDING_2FA_MAX_BAD:
            session.pop("pending_2fa", None)
            flash(tr("Too many incorrect codes. Enter your password again."), "error")
            return redirect(url_for("login"))
        session["pending_2fa"] = pending
        flash(tr("Incorrect code."), "error")
        return render_template("login_2fa.html")
    store.throttle_record_success(conn, pending["username"])
    response = _finish_login({"id": pending["uid"], "username": pending["username"],
                              "role": pending["role"], "totp_enabled": True}, pending["next"])
    if how == "recovery":
        flash(tr("You signed in with a recovery code. {count} left. To get a fresh set, turn two-factor "
                 "authentication off and on again in Account security.", count=store.recovery_codes_left(conn, pending["uid"])), "ok")
    return response


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


# --- Dashboard --------------------------------------------------------------

@app.route("/")
@login_required
def dashboard():
    conn = get_db()
    summary = store.usage_summary(conn)
    recent = store.list_events(conn, limit=15)
    return render_template("dashboard.html", summary=summary, recent=recent)


# --- Devices -----------------------------------------------------------

@app.route("/devices")
@login_required
def devices():
    conn = get_db()
    return render_template("devices.html", devices=store.list_devices(conn),
                            groups=store.list_groups(conn))


@app.route("/devices/<device_id>/group", methods=["POST"])
@admin_required
def set_device_group(device_id):
    conn = get_db()
    group_id = int(request.form["group_id"])
    store.assign_device_group(conn, device_id, group_id)
    flash(tr("Moved '{device}' to a new group.", device=device_id), "ok")
    return redirect(url_for("devices"))


@app.route("/devices/<device_id>/revoke", methods=["POST"])
@admin_required
def revoke_device_ui(device_id):
    """Cut a lost/compromised device off from the relay without rotating the shared secret."""
    if not store.set_device_revoked(get_db(), device_id, True):
        abort(404)
    flash(tr("Relay access revoked for '{device}'. Relays apply this within about a minute.", device=device_id), "ok")
    return redirect(url_for("devices"))


@app.route("/devices/<device_id>/restore", methods=["POST"])
@admin_required
def restore_device_ui(device_id):
    if not store.set_device_revoked(get_db(), device_id, False):
        abort(404)
    flash(tr("Relay access restored for '{device}'.", device=device_id), "ok")
    return redirect(url_for("devices"))


@app.route("/devices/<device_id>/rename", methods=["POST"])
@admin_required
def rename_device(device_id):
    conn = get_db()
    store.rename_device(conn, device_id, request.form.get("display_name", "").strip() or device_id)
    return redirect(url_for("devices"))


@app.route("/devices/<device_id>/mac", methods=["POST"])
@admin_required
def set_device_mac(device_id):
    conn = get_db()
    store.set_device_mac_address(conn, device_id, request.form.get("mac_address", ""))
    return redirect(url_for("devices"))


@app.route("/devices/<device_id>/wake", methods=["POST"])
@admin_required
def wake_device_ui(device_id):
    """Phase 11: the Devices page's own Wake button, backed by the exact
    same logic as the REST API's ops_wake_device below - one
    implementation, two ways in (a browser click, or a script's API call)."""
    conn = get_db()
    device = store.get_device_by_device_id(conn, device_id)
    if device is None:
        abort(404)
    if not device.get("mac_address"):
        flash(tr("No MAC address on file for '{device}'.", device=device_id), "error")
    else:
        try:
            wol.send_magic_packet(device["mac_address"], _WOL_BROADCAST, _WOL_PORT)
            flash(tr("Sent a wake packet to '{device}' ({mac}).", device=device_id,
                     mac=device["mac_address"]), "ok")
        except (ValueError, OSError) as e:
            flash(tr("Could not send the wake packet: {error}", error=e), "error")
    return redirect(url_for("devices"))


# --- Groups / policy -----------------------------------------------------

@app.route("/groups")
@login_required
def groups():
    conn = get_db()
    rows = store.list_groups(conn)
    for row in rows:
        row["allowed_viewer_ids"], row["blocked_viewer_ids"] = store.get_group_viewer_lists(conn, row["id"])
    return render_template("groups.html", groups=rows)


@app.route("/groups/new", methods=["GET", "POST"])
@admin_required
def new_group():
    if request.method == "POST":
        conn = get_db()
        try:
            group_id = store.create_group(conn, request.form["name"].strip(), _policy_from_form(request.form))
        except ValueError as e:
            flash(str(e), "error")
            return render_template("group_form.html", group=None)
        _apply_viewer_lists_from_form(conn, group_id, request.form)
        flash(tr("Group created."), "ok")
        return redirect(url_for("groups"))
    return render_template("group_form.html", group=None)


@app.route("/groups/<int:group_id>/edit", methods=["GET", "POST"])
@admin_required
def edit_group(group_id):
    conn = get_db()
    group = store.get_group(conn, group_id)
    if group is None:
        abort(404)
    if request.method == "POST":
        new_name = request.form["name"].strip()
        if new_name != group["name"]:
            try:
                store.rename_group(conn, group_id, new_name)
            except ValueError as e:
                flash(str(e), "error")
                return redirect(url_for("edit_group", group_id=group_id))
        store.update_group_policy(conn, group_id, _policy_from_form(request.form))
        _apply_viewer_lists_from_form(conn, group_id, request.form)
        flash(tr("Policy updated."), "ok")
        return redirect(url_for("groups"))
    group["allowed_viewer_ids"], group["blocked_viewer_ids"] = store.get_group_viewer_lists(conn, group_id)
    return render_template("group_form.html", group=group)


@app.route("/groups/<int:group_id>/delete", methods=["POST"])
@admin_required
def delete_group(group_id):
    conn = get_db()
    try:
        store.delete_group(conn, group_id)
        flash(tr("Group deleted; its devices moved to Default."), "ok")
    except ValueError as e:
        flash(str(e), "error")
    return redirect(url_for("groups"))


def _policy_from_form(form) -> dict:
    checkboxes = ["allow_unattended_access", "allow_file_transfer", "allow_clipboard",
                  "allow_chat", "allow_whiteboard", "allow_printing", "allow_voice",
                  "require_view_only", "require_whitelist", "auto_update"]
    p = {name: (name in form) for name in checkboxes}
    p["max_viewers"] = form.get("max_viewers", "10")
    p["max_session_minutes"] = form.get("max_session_minutes", "0")
    return p


def _apply_viewer_lists_from_form(conn, group_id: int, form) -> None:
    allowed = form.get("allowed_viewer_ids", "").replace(",", "\n").splitlines()
    blocked = form.get("blocked_viewer_ids", "").replace(",", "\n").splitlines()
    store.set_group_viewer_list(conn, group_id, "allowed", allowed)
    store.set_group_viewer_list(conn, group_id, "blocked", blocked)


# --- Session history -------------------------------------------------------

@app.route("/sessions")
@login_required
def sessions():
    conn = get_db()
    device_filter = request.args.get("device") or None
    event_filter = request.args.get("event") or None
    since_iso = None
    days = request.args.get("days")
    if days and days.isdigit():
        import time
        from datetime import datetime, timezone
        since_iso = datetime.fromtimestamp(time.time() - int(days) * 86400, tz=timezone.utc).isoformat()
    events = store.list_events(conn, device_id=device_filter, event=event_filter,
                                since_iso=since_iso, limit=300)
    return render_template("sessions.html", events=events,
                            devices=store.list_devices(conn),
                            filters={"device": device_filter or "", "event": event_filter or "",
                                     "days": days or ""})


# --- Support / feedback inbox (Phase 12) ---------------------------------------

@app.route("/feedback")
@login_required
def feedback_inbox():
    status = request.args.get("status", "open")
    if status not in ("open", "resolved", "all"):
        status = "open"
    items = store.list_feedback(get_db(), status=None if status == "all" else status, limit=300)
    return render_template("feedback.html", items=items, status=status)


@app.route("/feedback/<int:feedback_id>")
@login_required
def feedback_detail(feedback_id):
    item = store.get_feedback(get_db(), feedback_id)
    if item is None:
        abort(404)
    return render_template("feedback_detail.html", item=item)


@app.route("/feedback/<int:feedback_id>/reply", methods=["POST"])
@admin_required
def feedback_reply(feedback_id):
    try:
        store.reply_to_feedback(get_db(), feedback_id, request.form.get("reply", ""),
                                session.get("username"), resolve=bool(request.form.get("resolve")))
        flash(tr("Reply saved."), "ok")
    except store.FeedbackError as e:
        flash(str(e), "error")
    return redirect(url_for("feedback_detail", feedback_id=feedback_id))


@app.route("/feedback/<int:feedback_id>/status", methods=["POST"])
@admin_required
def feedback_status(feedback_id):
    try:
        store.set_feedback_status(get_db(), feedback_id, request.form.get("status", ""))
    except store.FeedbackError as e:
        flash(str(e), "error")
    return redirect(url_for("feedback_detail", feedback_id=feedback_id))


# --- Account security: the signed-in operator's own two-factor setup --------------

_ENROLL_ALLOWED = {"account_security", "account_2fa_start", "account_2fa_confirm", "logout",
                   "set_language", "set_theme", "static", "healthz", "login"}


@app.before_request
def force_2fa_enrollment():
    """With REMOTEBRIDGE_REQUIRE_2FA=1, an operator without 2FA can reach nothing but the page that
    sets it up."""
    if session.get("must_enroll_2fa") and not request.path.startswith("/api/") \
            and request.endpoint not in _ENROLL_ALLOWED:
        return redirect(url_for("account_security"))
    return None


def _group_secret(secret: str) -> str:
    return " ".join(secret[i:i + 4] for i in range(0, len(secret), 4))


@app.route("/account/security")
@login_required
def account_security():
    conn = get_db()
    me = store.get_admin_user(conn, session["user_id"])
    secret = session.get("totp_setup")
    setup = None
    if secret and not me["totp_enabled"]:
        issuer = store.get_branding(conn)["display_name"]
        setup = {"secret": _group_secret(secret), "uri": store.totp_uri(secret, me["username"], issuer)}
    return render_template("account_security.html", enabled=bool(me["totp_enabled"]), setup=setup,
                           recovery_left=store.recovery_codes_left(conn, me["id"]),
                           new_codes=session.pop("_recovery_codes", None),
                           required=REQUIRE_2FA)


@app.route("/account/security/2fa/start", methods=["POST"])
@login_required
def account_2fa_start():
    if not store.get_admin_user(get_db(), session["user_id"])["totp_enabled"]:
        session["totp_setup"] = store.new_totp_secret()
    return redirect(url_for("account_security"))


@app.route("/account/security/2fa/confirm", methods=["POST"])
@login_required
def account_2fa_confirm():
    conn = get_db()
    secret = session.get("totp_setup")
    if not secret:
        return redirect(url_for("account_security"))
    codes = store.enable_totp(conn, session["user_id"], secret, request.form.get("code", ""))
    if codes is None:
        flash(tr("That code didn't match. Check the clock on your phone and try again."), "error")
        return redirect(url_for("account_security"))
    session.pop("totp_setup", None)
    session.pop("must_enroll_2fa", None)
    session["_recovery_codes"] = codes
    flash(tr("Two-factor authentication is on."), "ok")
    return redirect(url_for("account_security"))


@app.route("/account/security/2fa/disable", methods=["POST"])
@login_required
def account_2fa_disable():
    conn = get_db()
    if REQUIRE_2FA:
        flash(tr("This console requires two-factor authentication, so it can't be turned off."), "error")
        return redirect(url_for("account_security"))
    me = store.get_admin_user(conn, session["user_id"])
    keys = store.throttle_keys(me["username"], _client_address())
    wait = store.throttle_retry_after(conn, keys)
    if wait:
        flash(tr("Too many failed attempts. Try again in {minutes} min.", minutes=max(1, -(-wait // 60))), "error")
        return redirect(url_for("account_security"))
    if not (store.change_password_check(conn, me["id"], request.form.get("password", ""))
            and store.verify_second_factor(conn, me["id"], request.form.get("code", ""))):
        store.throttle_record_failure(conn, keys)
        flash(tr("Password or code was incorrect."), "error")
        return redirect(url_for("account_security"))
    store.disable_totp(conn, me["id"])
    flash(tr("Two-factor authentication is off."), "ok")
    return redirect(url_for("account_security"))


# --- Console operator accounts ---------------------------------------------

@app.route("/users")
@admin_required
def users():
    conn = get_db()
    # Phase 11: a freshly created key is passed through session-flash-like
    # one-shot state (see new_api_key below) so it can be shown exactly
    # once, then never again - only its hash is kept after this render.
    new_key = session.pop("_new_api_key", None)
    return render_template("users.html", users=store.list_admin_users(conn),
                            api_keys=store.list_api_keys(conn), new_api_key=new_key)


@app.route("/api-keys/new", methods=["POST"])
@admin_required
def new_api_key():
    raw_key = store.create_api_key(get_db(), request.form.get("label", ""))
    session["_new_api_key"] = raw_key
    return redirect(url_for("users"))


@app.route("/api-keys/<int:key_id>/delete", methods=["POST"])
@admin_required
def delete_api_key(key_id):
    store.revoke_api_key(get_db(), key_id)
    flash(tr("API key revoked."), "ok")
    return redirect(url_for("users"))


@app.route("/users/new", methods=["POST"])
@admin_required
def new_user():
    conn = get_db()
    try:
        store.create_admin_user(conn, request.form["username"].strip(),
                                 request.form["password"], request.form.get("role", "admin"))
        flash(tr("Operator account created."), "ok")
    except ValueError as e:
        flash(str(e), "error")
    return redirect(url_for("users"))


@app.route("/users/<int:user_id>/reset-2fa", methods=["POST"])
@admin_required
def reset_user_2fa(user_id):
    """For an operator who lost their phone and their recovery codes."""
    conn = get_db()
    target = store.get_admin_user(conn, user_id)
    if target is None or user_id == session["user_id"]:
        abort(404)
    store.disable_totp(conn, user_id)
    app.logger.warning("2FA reset for %r by %r", target["username"], session.get("username"))
    flash(tr("Two-factor authentication reset for {name}. They can set it up again after signing in.",
             name=target["username"]), "ok")
    return redirect(url_for("users"))


@app.route("/users/<int:user_id>/delete", methods=["POST"])
@admin_required
def delete_user(user_id):
    conn = get_db()
    try:
        store.delete_admin_user(conn, user_id)
    except ValueError as e:
        flash(str(e), "error")
    return redirect(url_for("users"))


# --- Phase 10: branding + release, one settings page for both ------------

@app.route("/deployment", methods=["GET", "POST"])
@admin_required
def deployment():
    conn = get_db()
    if request.method == "POST":
        form_id = request.form.get("form_id")
        if form_id == "branding":
            store.set_branding(conn, request.form.get("display_name", ""),
                                request.form.get("support_url", ""))
            flash(tr("Branding updated."), "ok")
        elif form_id == "release":
            store.set_release(conn, request.form.get("version", ""),
                               request.form.get("download_url", ""),
                               request.form.get("sha256", ""),
                               request.form.get("notes", ""))
            flash(tr("Release published - enrolled hosts with auto_update on will "
                     "pick it up on their next policy refresh."), "ok")
        return redirect(url_for("deployment"))

    return render_template("deployment.html", branding=store.get_branding(conn),
                            release=store.get_release(conn),
                            org_enrollment_key=store.get_setting(conn, "org_enrollment_key"))


# --- Device-facing JSON API -------------------------------------------------
# Bearer-token authenticated (the device's report_token from enrollment),
# not session-cookie authenticated - hosts are scripts, not browsers.

def _authenticate_device():
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return None
    token = header[len("Bearer "):].strip()
    return store.get_device_by_token(get_db(), token)


def _authenticate_api_key():
    """Phase 11: for /api/v1/ops/ - scripts and other systems, not a
    person's browser session (which uses the cookie-based login instead).
    Deliberately a separate credential from a device's report_token: an
    API key can query/act on *any* device, a report_token only ever
    proves "I am this one device"."""
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return None
    return store.verify_api_key(get_db(), header[len("Bearer "):].strip())


@app.route("/api/v1/enroll", methods=["POST"])
def api_enroll():
    conn = get_db()
    body = request.get_json(silent=True) or {}
    device_id = (body.get("device_id") or "").strip()
    if not device_id:
        return jsonify({"error": "device_id is required"}), 400

    org_key = store.get_setting(conn, "org_enrollment_key")
    if org_key and body.get("enrollment_key") != org_key:
        return jsonify({"error": "invalid enrollment_key"}), 403

    device = store.enroll_device(conn, device_id, body.get("display_name"))
    group = store.get_group(conn, device["group_id"])
    return jsonify({"device_id": device["device_id"], "report_token": device["report_token"],
                     "group": group["name"]})


@app.route("/api/v1/policy", methods=["GET"])
def api_policy():
    device = _authenticate_device()
    if device is None:
        return jsonify({"error": "invalid or missing report token"}), 401
    conn = get_db()
    store.touch_last_seen(conn, device["device_id"], source_address=request.remote_addr)
    client_version = request.args.get("client_version")
    if client_version:
        store.record_version(conn, device["device_id"], client_version)
    mac_address = request.args.get("mac_address")
    if mac_address:
        store.set_device_mac_address(conn, device["device_id"], mac_address)
    return jsonify(policy_mod.resolve_policy_for_device(conn, device))


@app.route("/api/v1/relay-token", methods=["GET"])
def api_relay_token():
    """Phase 13: the token this enrolled device presents to an authenticating relay. Only a device
    that enrolled here can get one, and only for its own ID - which is what stops anyone else from
    registering (squatting) that device ID on the relay. `token` is null when RELAY_SECRET isn't
    set (the relay is open and needs none).

    With $RELAY_TOKEN_TTL set the token expires (`expires_in` says in how many seconds) and the host asks
    again before then; unset, it is the original non-expiring token and `expires_in` is null."""
    device = _authenticate_device()
    if device is None:
        return jsonify({"error": "invalid or missing report token"}), 401
    if device.get("revoked_at"):
        return jsonify({"error": "this device's relay access has been revoked"}), 403
    secret = relayauth.current_secret()
    ttl = relayauth.token_ttl() if secret else 0
    if secret and ttl:
        # `expires_in` is relative, so a host with a wrong clock can still schedule its refresh correctly.
        token = relayauth.make_expiring_token(secret, device["device_id"], int(time.time()) + ttl)
    else:
        token = relayauth.make_token(secret, device["device_id"]) if secret else None
    return jsonify({"device_id": device["device_id"], "token": token,
                    "expires_in": ttl if (secret and ttl) else None}), 200, {"Cache-Control": "no-store"}


@app.route("/api/v1/relay/revoked", methods=["GET"])
def api_relay_revoked():
    """The devices a relay must refuse. Polled by relays (server/relay.py --revoked-url), which authenticate
    with a credential derived from the shared RELAY_SECRET - so only a relay that holds it can read the list,
    and nothing new has to be provisioned. Unavailable when RELAY_SECRET is unset (an open relay has no
    identity to authenticate)."""
    secrets_ = relayauth.all_secrets()
    if not secrets_:
        return jsonify({"error": "RELAY_SECRET is not set on this console"}), 403
    header = request.headers.get("Authorization", "")
    presented = header[len("Bearer "):].strip() if header.startswith("Bearer ") else ""
    if not presented or not any(hmac.compare_digest(presented, relayauth.revocation_bearer(sec))
                                for sec in secrets_):
        return jsonify({"error": "invalid or missing relay credential"}), 401
    return jsonify({"revoked": store.list_revoked_device_ids(get_db())}), 200, {"Cache-Control": "no-store"}


@app.route("/api/v1/events", methods=["POST"])
def api_events():
    device = _authenticate_device()
    if device is None:
        return jsonify({"error": "invalid or missing report token"}), 401
    conn = get_db()
    body = request.get_json(silent=True) or {}
    if body.get("event") not in ("attempt", "start", "end"):
        return jsonify({"error": "event must be attempt, start, or end"}), 400
    store.record_event(conn, device["device_id"], body["event"],
                        viewer_id=body.get("viewer_id"), decision=body.get("decision"),
                        reason=body.get("reason"), address=body.get("address"),
                        duration_seconds=body.get("duration_seconds"))
    store.touch_last_seen(conn, device["device_id"])
    return jsonify({"ok": True})


@app.route("/api/v1/branding", methods=["GET"])
def api_branding():
    """Phase 10: unauthenticated on purpose - a host or its installer may
    want to show org branding before enrollment has happened at all (e.g.
    a first-run screen), and none of this is sensitive."""
    return jsonify(store.get_branding(get_db()))


@app.route("/api/v1/release", methods=["GET"])
def api_release():
    device = _authenticate_device()
    if device is None:
        return jsonify({"error": "invalid or missing report token"}), 401
    release = store.get_release(get_db())
    return jsonify(release or {})


@app.route("/api/v1/feedback", methods=["POST"])
def api_feedback_submit():
    """Phase 12: a host files a support ticket - its own, or one relayed on
    behalf of a connected viewer (viewer_id says which). Device-authenticated
    and rate-limited per device (store.FEEDBACK_RATE_LIMIT per hour)."""
    device = _authenticate_device()
    if device is None:
        return jsonify({"error": "invalid or missing report token"}), 401
    body = request.get_json(silent=True) or {}
    conn = get_db()
    try:
        ticket_id = store.create_feedback(
            conn, device["device_id"], body.get("message"), body.get("category", "other"),
            viewer_id=body.get("viewer_id"), client_version=body.get("client_version"))
    except store.FeedbackError as e:
        code = 429 if "too many" in str(e) else 400
        return jsonify({"error": str(e)}), code
    store.touch_last_seen(conn, device["device_id"])
    return jsonify({"ok": True, "ticket_id": ticket_id}), 201


@app.route("/api/v1/feedback", methods=["GET"])
def api_feedback_list():
    """A device reads back its OWN tickets (status + any admin reply) - never another device's."""
    device = _authenticate_device()
    if device is None:
        return jsonify({"error": "invalid or missing report token"}), 401
    items = store.list_feedback(get_db(), device_id=device["device_id"], limit=20)
    keep = ("id", "category", "message", "status", "reply", "replied_at", "created_at", "viewer_id")
    return jsonify([{k: item[k] for k in keep} for item in items])


@app.route("/api/v1/ops/feedback", methods=["GET"])
def ops_list_feedback():
    if _authenticate_ops() is None:
        return jsonify({"error": "invalid or missing API key"}), 401
    status = request.args.get("status")
    return jsonify(store.list_feedback(get_db(), status=status if status in ("open", "resolved") else None,
                                        device_id=request.args.get("device") or None, limit=200))


@app.route("/api/v1/preauth/<viewer_id>", methods=["GET"])
def api_preauth(viewer_id):
    """Phase 11: a host checks this, for one specific connecting viewer_id,
    at the moment auth would otherwise fall back to a blocking console
    prompt (see host_p11.py's perform_host_auth_with_preauth) - the other
    half of POST /api/v1/ops/devices/<id>/connect below. Device-
    authenticated like /api/v1/policy: a host can only ever check preauth
    for itself, never for another device."""
    device = _authenticate_device()
    if device is None:
        return jsonify({"error": "invalid or missing report token"}), 401
    ok = store.check_and_consume_preauth(get_db(), device["device_id"], viewer_id)
    return jsonify({"preauthorized": ok})


# --- Phase 11: operator-facing REST API for automation (cli/, or any
# direct caller) - API-key authenticated, not session-cookie or
# report_token authenticated. See admin/README.md and cli/README.md. ----

def _authenticate_ops():
    key = _authenticate_api_key()
    if key is not None:
        return key
    # A logged-in operator's browser session also works here, so the
    # same endpoints can back a "Wake" button on the Devices page
    # without needing a whole separate implementation for the UI.
    if "user_id" in session:
        return {"label": session.get("username")}
    return None


def _device_or_404(conn, device_id):
    device = store.get_device_by_device_id(conn, device_id)
    if device is None:
        return None, (jsonify({"error": f"no such device: {device_id}"}), 404)
    return device, None


@app.route("/api/v1/ops/devices", methods=["GET"])
def ops_list_devices():
    if _authenticate_ops() is None:
        return jsonify({"error": "invalid or missing API key"}), 401
    return jsonify(store.list_devices(get_db()))


@app.route("/api/v1/ops/devices/<device_id>/sessions", methods=["GET"])
def ops_device_sessions(device_id):
    if _authenticate_ops() is None:
        return jsonify({"error": "invalid or missing API key"}), 401
    conn = get_db()
    device, err = _device_or_404(conn, device_id)
    if err:
        return err
    limit = request.args.get("limit", "50")
    return jsonify(store.list_events(conn, device_id=device_id,
                                       limit=int(limit) if limit.isdigit() else 50))


@app.route("/api/v1/ops/devices/<device_id>/wake", methods=["POST"])
def ops_wake_device(device_id):
    if _authenticate_ops() is None:
        return jsonify({"error": "invalid or missing API key"}), 401
    conn = get_db()
    device, err = _device_or_404(conn, device_id)
    if err:
        return err
    if not device.get("mac_address"):
        return jsonify({"error": f"no MAC address on file for {device_id} - set one on the "
                                  f"Devices page, or upgrade the host so it reports its own"}), 400
    try:
        wol.send_magic_packet(device["mac_address"], _WOL_BROADCAST, _WOL_PORT)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except OSError as e:
        return jsonify({"error": f"could not send the magic packet: {e}"}), 502
    return jsonify({"ok": True, "mac_address": device["mac_address"]})


@app.route("/api/v1/ops/devices/<device_id>/connect", methods=["POST"])
def ops_connect_device(device_id):
    if _authenticate_ops() is None:
        return jsonify({"error": "invalid or missing API key"}), 401
    conn = get_db()
    device, err = _device_or_404(conn, device_id)
    if err:
        return err
    body = request.get_json(silent=True) or {}
    ttl = body.get("ttl_seconds", 300)
    pending = store.create_pending_connection(conn, device_id, ttl_seconds=ttl)
    return jsonify({
        "device_id": device_id,
        "viewer_id": pending["viewer_id"],
        "expires_at": pending["expires_at"],
        "last_seen_address": device.get("last_seen_address"),
        "note": "Connect with: viewer_p12.py <host-address>:<video-port> --id "
                f"{pending['viewer_id']} (no --password) - the host recognizes this "
                "one-time ID via the admin console instead. last_seen_address is a "
                "best-effort hint (where this device last reached the console from), "
                "not a guaranteed reachable address.",
    })


# --- First-run bootstrap ---------------------------------------------------

def bootstrap(conn) -> None:
    """First-run setup. Interactive on a terminal (the original behavior); fully non-interactive
    when configured by environment, which is how a hosted deploy (render.yaml) runs:

        SECRET_KEY          Flask session key (else generated once and kept in the database)
        ORG_ENROLLMENT_KEY  the key hosts present on first enrollment (else generated and printed)
        ADMIN_USERNAME / ADMIN_PASSWORD   creates the first operator if none exists yet
    """
    store.purge_throttle(conn)
    env_secret = os.environ.get("SECRET_KEY")
    if env_secret:
        app.secret_key = env_secret
    else:
        if store.get_setting(conn, "flask_secret_key") is None:
            store.set_setting(conn, "flask_secret_key", secrets.token_hex(32))
        app.secret_key = store.get_setting(conn, "flask_secret_key")

    env_key = os.environ.get("ORG_ENROLLMENT_KEY")
    if env_key:
        if store.get_setting(conn, "org_enrollment_key") != env_key:
            store.set_setting(conn, "org_enrollment_key", env_key)
    elif store.get_setting(conn, "org_enrollment_key") is None:
        key = secrets.token_urlsafe(24)
        store.set_setting(conn, "org_enrollment_key", key)
        print("=" * 70)
        print(" ORG ENROLLMENT KEY (save this - shown only once):")
        print(f"   {key}")
        print(" Hosts pass this on their first --admin-url run to enroll.")
        print("=" * 70)

    if store.count_admin_users(conn) == 0:
        env_password = os.environ.get("ADMIN_PASSWORD")
        if env_password:
            username = os.environ.get("ADMIN_USERNAME", "admin").strip() or "admin"
            store.create_admin_user(conn, username, env_password, role="admin")
            print(f"Created operator '{username}' from ADMIN_USERNAME/ADMIN_PASSWORD.")
        elif sys.stdin is not None and sys.stdin.isatty():
            print("No operator accounts exist yet - let's create the first one.")
            username = input(" Username: ").strip() or "admin"
            password = getpass.getpass(" Password: ")
            store.create_admin_user(conn, username, password, role="admin")
            print(f" Created operator '{username}'.")
        else:
            print("WARNING: no operator account exists and no terminal is attached. Set "
                  "ADMIN_USERNAME and ADMIN_PASSWORD, then restart.", file=sys.stderr)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Phase 9 admin console & policy server")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8443)))
    parser.add_argument("--db", default=store.DB_PATH, help="SQLite file path or postgres:// URL (default: $DATABASE_URL, else admin.db)")
    parser.add_argument("--wol-broadcast", default="255.255.255.255",
                         help="Phase 11: broadcast address Wake-on-LAN packets are sent to")
    parser.add_argument("--wol-port", type=int, default=9, help="Phase 11: UDP port for Wake-on-LAN packets")
    args = parser.parse_args()
    _DB_PATH = args.db
    _WOL_BROADCAST, _WOL_PORT = args.wol_broadcast, args.wol_port

    _bootstrap_conn = store.init_db(_DB_PATH)
    bootstrap(_bootstrap_conn)
    _bootstrap_conn.close()

    app.run(host=args.host, port=args.port, threaded=True)
