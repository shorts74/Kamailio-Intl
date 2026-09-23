"""Login/logout routes -- kept as a top-level module (not blueprints/
subpackage) to match this bundle's current flat app.py structure."""
import logging
import os
from flask import Blueprint, render_template, request, redirect, session, url_for
import auth
import db

bp = Blueprint("web_auth", __name__)

# Dedicated log file, separate from the general app log, so
# fail2ban's filter only has to parse lines that are guaranteed to be
# failed-login events -- no risk of the regex accidentally matching
# something unrelated in a shared/noisier log.
os.makedirs("/var/log/sip-platform", exist_ok=True)
_auth_logger = logging.getLogger("sip_platform_auth")
_auth_logger.setLevel(logging.INFO)
if not _auth_logger.handlers:
    _handler = logging.FileHandler("/var/log/sip-platform/auth.log")
    _handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
    _auth_logger.addHandler(_handler)


def get_settings():
    rows = db.query("SELECT * FROM platform_settings WHERE id=1")
    return rows[0] if rows else None


@bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        user = auth.authenticate(username, password)
        if user:
            session["username"] = user["username"]
            session["role"] = user["role"]
            session["source"] = user["source"]
            # "Remember me on this device" -- extends the session to
            # survive browser restarts (PERMANENT_SESSION_LIFETIME,
            # currently 30 days) rather than ending when the browser
            # closes. Unchecked (the default) leaves session.permanent
            # at its normal False, identical to today's existing
            # behavior -- nothing changes for anyone who doesn't tick it.
            session.permanent = bool(request.form.get("remember_me"))
            # request.args.get("next") is a prefix-STRIPPED path (Werkzeug
            # separates SCRIPT_NAME from PATH_INFO -- see auth.py's
            # next=request.path) -- request.script_root is "" when
            # unprefixed or e.g. "/platform" when proxied under a prefix
            # (see app.py's ProxyFix x_prefix), so prepending it here
            # always lands back under the correct prefix. url_for()'s
            # output (the no-"next" fallback case) is already correctly
            # prefixed on its own and must NOT also get script_root
            # prepended, or it would be doubled.
            next_param = request.args.get("next")
            if next_param:
                return redirect(request.script_root + next_param)
            return redirect(url_for("web.dashboard"))
        # Format is load-bearing -- the fail2ban filter (see
        # infrastructure/fail2ban-platform-auth.conf) matches this
        # exact "FAILED_LOGIN from=<ip>" pattern. request.remote_addr
        # is the REAL client IP here, not nginx's, because of the
        # ProxyFix middleware configured in app.py -- without that,
        # every attempt would log as 127.0.0.1 and fail2ban could
        # never ban anyone.
        _auth_logger.info(f"FAILED_LOGIN from={request.remote_addr} user={username!r}")
        return render_template("login.html", error="Invalid username or password", settings=get_settings())
    return render_template("login.html", settings=get_settings())


@bp.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("web_auth.login"))
