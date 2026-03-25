# ============================================================
# auth.py — Login, session management, and route protection
# ============================================================

from __future__ import annotations

import os
import functools
import logging

import bcrypt
from flask import session, redirect, url_for, request

log = logging.getLogger(__name__)

DASHBOARD_USERNAME    = os.getenv("DASHBOARD_USERNAME", "admin")
DASHBOARD_PASSWORD_HASH = os.getenv("DASHBOARD_PASSWORD_HASH", "")


def check_password(plain: str) -> bool:
    """Verifies a plain-text password against the stored bcrypt hash."""
    if not DASHBOARD_PASSWORD_HASH:
        log.warning("No password hash configured — login disabled")
        return False
    try:
        return bcrypt.checkpw(plain.encode(), DASHBOARD_PASSWORD_HASH.encode())
    except Exception as e:
        log.error(f"Password check error: {e}")
        return False


def is_logged_in() -> bool:
    """Returns True if the current session is authenticated."""
    return session.get("logged_in") is True


def login_required(f):
    """
    Flask route decorator — redirects to /login if not authenticated.
    Usage:
        @app.route("/dashboard")
        @login_required
        def dashboard():
            ...
    """
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        if not is_logged_in():
            return redirect(url_for("login", next=request.path))
        return f(*args, **kwargs)
    return decorated
