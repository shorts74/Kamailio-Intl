r"""
Three coexisting auth methods, all on one login page:
  1. Local platform accounts (platform_users, bcrypt)
  2. Homer-shared accounts (validates against homer_config.users
     directly -- Homer stays the single source of identity, we
     never copy or store Homer passwords)
  3. API tokens (Authorization: Bearer <token>, for automation --
     separate from session login)

HOMER AUTH VERIFICATION NOTE: implemented against homer-app's public
source (github.com/sipcapture/homer-app/data/service/user.go), which
confirms bcrypt via golang.org/x/crypto/bcrypt and a `Hash` field on
the user model. The exact column/table name (expected: `users` table,
`hash` column, GORM snake_case default) should be verified against
your actual installed homer-app version before relying on this in
production -- run:
  PGPASSWORD=... psql -U homer -h 127.0.0.1 -d homer_config -c "\d users"
and adjust HOMER_USER_TABLE / HOMER_HASH_COLUMN below if it differs.
"""
import bcrypt
import secrets
import hashlib
import jwt
import datetime
from functools import wraps
from flask import session, redirect, url_for, request, jsonify
import db
import config

HOMER_USER_TABLE = "users"
HOMER_USERNAME_COLUMN = "username"
HOMER_HASH_COLUMN = "hash"


def hash_password(plain):
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt()).decode()


def verify_local(username, plain):
    rows = db.query("SELECT * FROM platform_users WHERE username=%s AND enabled=true", (username,))
    if not rows:
        return None
    user = rows[0]
    try:
        if bcrypt.checkpw(plain.encode(), user["password_hash"].encode()):
            return {"username": username, "role": user["role"], "source": "local"}
    except Exception:
        pass
    return None


def verify_homer(username, plain):
    """
    Validates against homer_config.users directly. Returns None
    gracefully (does not raise) if the table/column doesn't match
    what's expected -- see module docstring verification note.
    """
    try:
        rows = db.query(
            f"SELECT {HOMER_HASH_COLUMN} AS hash FROM {HOMER_USER_TABLE} WHERE {HOMER_USERNAME_COLUMN}=%s",
            (username,), dbname=config.HOMER_PG_DB
        )
    except Exception:
        return None
    if not rows:
        return None
    stored_hash = rows[0]["hash"]
    try:
        if bcrypt.checkpw(plain.encode(), stored_hash.encode()):
            # SECURITY/CORRECTNESS: every Homer-authenticated user was
            # previously hardcoded to role "operator" regardless of
            # which Homer account they actually are -- including the
            # real Homer admin account itself. Since that admin
            # account is currently the ONLY way to log into the
            # platform at all (no local platform_users are seeded by
            # default), this made every admin-only action (adding
            # trunks, Settings, etc.) permanently inaccessible to
            # everyone, confirmed from a real "admin role required"
            # report. The Homer "admin" username is treated as
            # platform admin too; any other Homer account gets the
            # least-privilege "operator" role, matching the original
            # intent for non-admin Homer users.
            platform_role = "admin" if username == "admin" else "operator"
            return {"username": username, "role": platform_role, "source": "homer"}
    except Exception:
        pass
    return None


def authenticate(username, password):
    """Try local first, then Homer. Returns user dict or None."""
    user = verify_local(username, password)
    if user:
        return user
    return verify_homer(username, password)


def login_required(role=None):
    def decorator(f):
        @wraps(f)
        def wrapped(*args, **kwargs):
            # API token path (for automation, no session needed)
            auth_header = request.headers.get("Authorization", "")
            if auth_header.startswith("Bearer "):
                token = auth_header[7:]
                token_hash = hashlib.sha256(token.encode()).hexdigest()
                rows = db.query(
                    "SELECT * FROM platform_api_tokens WHERE token_hash=%s AND revoked=false "
                    "AND (expires_at IS NULL OR expires_at > NOW())", (token_hash,))
                if rows:
                    db.execute("UPDATE platform_api_tokens SET last_used_at=NOW() WHERE id=%s", (rows[0]["id"],))
                    if role == "admin" and rows[0]["scope"] != "readwrite":
                        return jsonify({"error": "token lacks write scope"}), 403
                    return f(*args, **kwargs)
                return jsonify({"error": "invalid or expired token"}), 401

            # API routes never redirect to an HTML login page -- always a
            # clean JSON 401 when no valid token/session is present. Same
            # treatment for any other route when the client explicitly
            # asked for JSON (Accept: application/json) -- confirmed via a
            # real bug report: an AJAX POST/GET from an expired session
            # got redirected to the HTML login page, and the calling JS's
            # response.json() failed with a raw "Unexpected token '<'"
            # parse error instead of a clean, handleable error message.
            wants_json = request.path.startswith("/api/") or \
                request.accept_mimetypes.best == "application/json"
            if wants_json:
                if "username" not in session:
                    return jsonify({"error": "authentication required -- your session may have expired (please log in again), or provide Authorization: Bearer <token> for API access"}), 401
                if role == "admin" and session.get("role") != "admin":
                    return jsonify({"error": "admin role required"}), 403
                return f(*args, **kwargs)

            if "username" not in session:
                return redirect(url_for("web_auth.login", next=request.path))
            if role == "admin" and session.get("role") != "admin":
                # Web UI clicks should never surface a raw JSON blob --
                # confirmed exactly this jarring behavior from a real
                # report (clicking Settings/Add Trunk showed
                # {"error":"admin role required"} instead of a page).
                # Redirect back to the dashboard with a clear message
                # instead, matching how every other web route reports
                # errors in this app.
                return redirect(url_for("web.dashboard", msg="This action requires an admin account", ok=0))
            return f(*args, **kwargs)
        return wrapped
    return decorator


def generate_api_token():
    raw = secrets.token_urlsafe(32)
    return raw, hashlib.sha256(raw.encode()).hexdigest()


def verify_kiosk_token(token, node_id=None):
    """
    Returns True if the token is valid AND its scope matches the
    requested board (global token works for any board incl. per-node
    ones; a per-node token only works for its own node's board, not
    the global one or another node's). Deliberately does NOT touch
    session/API-token auth at all -- kiosk tokens are their own
    narrow, read-only, /board*-only credential type.
    """
    if not token:
        return False
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    rows = db.query("SELECT * FROM platform_kiosk_tokens WHERE token_hash=%s AND revoked=false", (token_hash,))
    if not rows:
        return False
    kt = rows[0]
    if kt["scope_node_id"] is not None and kt["scope_node_id"] != node_id:
        return False
    db.execute("UPDATE platform_kiosk_tokens SET last_used_at=NOW() WHERE id=%s", (kt["id"],))
    return True


def generate_kiosk_token():
    raw = secrets.token_urlsafe(32)
    return raw, hashlib.sha256(raw.encode()).hexdigest()
