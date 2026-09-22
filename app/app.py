"""SIP Trunk Management Platform v3 -- entry point."""
from flask import Flask, jsonify, request, abort
from werkzeug.middleware.proxy_fix import ProxyFix
import config

app = Flask(__name__)
app.secret_key = config.APP_SECRET


@app.template_filter("timeago")
def timeago_filter(dt):
    """Renders a timestamp as 'Xs/m/h/d ago' -- used for the trunk
    Live-status badge (and anywhere else a checked_at value shouldn't
    look like a real-time indicator when it's actually a snapshot
    from whenever it was last polled)."""
    if not dt:
        return "never"
    import datetime
    now = datetime.datetime.now(dt.tzinfo) if dt.tzinfo else datetime.datetime.now()
    delta = now - dt
    seconds = int(delta.total_seconds())
    if seconds < 0:
        return "just now"
    if seconds < 60:
        return f"{seconds}s ago"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    days = hours // 24
    return f"{days}d ago"

# Trusts exactly one hop of X-Forwarded-For/X-Forwarded-Proto -- the
# nginx reverse proxy in front of this app (see manager-install.sh's
# nginx config, which sets these headers). Without this,
# request.remote_addr is always 127.0.0.1 (nginx's own address), which
# would make IP-based security features (fail2ban failed-login
# banning, ban_log, audit_log source IPs) meaningless -- every request
# would appear to come from localhost regardless of the real client.
# x_for=1 means "trust exactly one proxy hop"; if this app is ever put
# behind an additional load balancer/CDN, this needs to become x_for=2
# and that layer must also be configured to set X-Forwarded-For
# correctly, or client IPs become spoofable.
# x_prefix=1 additionally trusts X-Forwarded-Prefix (set by nginx when
# this app is proxied under a URL prefix, e.g. /platform/ alongside
# Homer at root on the same domain) -- this sets SCRIPT_NAME per
# request, so url_for() and request.script_root correctly account for
# the prefix without any HTML/header rewriting on nginx's side. This
# is the robust fix for the exact class of bug that made the
# post-login redirect escape to Homer earlier: that used sub_filter to
# rewrite HTML bodies, which can't touch Location headers at all,
# whereas this makes Flask generate the CORRECT prefixed URL from the
# start, so there's nothing left needing rewriting.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

app.config.update(
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SECURE=config.FORCE_SECURE_COOKIES,
    # Only applies when session.permanent is explicitly set True (the
    # new "Remember me on this device" checkbox at login) -- an
    # unchecked login is completely unaffected, same as today: the
    # session cookie remains a browser-session-only cookie that ends
    # when the browser closes.
    PERMANENT_SESSION_LIFETIME=60 * 60 * 24 * 30,
)

DESTRUCTIVE_GET_SUFFIXES = ("delete", "toggle", "apply", "discard", "restart")


@app.before_request
def csrf_origin_check():
    """Same CSRF mitigation as v2 -- see that version's comment for the full rationale."""
    if request.path.startswith("/api/"):
        return
    path_tail = request.path.rstrip("/").rsplit("/", 1)[-1]
    is_state_changing = request.method in ("POST", "PUT", "PATCH", "DELETE") or (
        request.method == "GET" and path_tail in DESTRUCTIVE_GET_SUFFIXES
    )
    if not is_state_changing:
        return
    origin = request.headers.get("Origin") or request.headers.get("Referer", "")
    if origin and request.host not in origin:
        abort(403, "Cross-origin request blocked")


from web import bp as web_bp
from blueprints_auth import bp as auth_bp
from api import bp as api_bp

app.register_blueprint(web_bp)
app.register_blueprint(auth_bp)
app.register_blueprint(api_bp)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=config.APP_PORT, debug=False)
