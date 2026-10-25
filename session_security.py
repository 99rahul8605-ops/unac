import base64
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time
from pathlib import Path

from fastapi import Request
from fastapi.responses import JSONResponse

SECRET = os.environ.get("PLAYBACK_SECRET", "").encode()
DB_PATH = Path("/root/unac/.state/playback_sessions.sqlite3")
ENFORCE_FILE = Path("/root/unac/.state/enforce_playback_sessions")
COOKIE = "__Host-lecture_session"
ALLOWED_ORIGIN = "https://app.bingolink.site"

if len(SECRET) < 32:
    raise RuntimeError("PLAYBACK_SECRET missing or too short")


def b64decode(value):
    return base64.urlsafe_b64decode(
        value + "=" * (-len(value) % 4)
    )


def connect():
    db = sqlite3.connect(str(DB_PATH), timeout=15)
    db.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            session_hash TEXT PRIMARY KEY,
            telegram_user_id TEXT NOT NULL,
            lecture_id TEXT NOT NULL,
            expires_at INTEGER NOT NULL
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS used_tickets (
            nonce TEXT PRIMARY KEY,
            expires_at INTEGER NOT NULL
        )
    """)
    db.commit()
    return db


def verify_ticket(ticket):
    try:
        encoded, signature = ticket.split(".", 1)

        expected = hmac.new(
            SECRET,
            ("session-ticket:" + encoded).encode(),
            hashlib.sha256
        ).hexdigest()

        if not hmac.compare_digest(expected, signature):
            return None

        data = json.loads(b64decode(encoded))

        now = int(time.time())
        expiry = int(data["exp"])

        if expiry < now or expiry > now + 120:
            return None

        if (
            not str(data.get("uid", "")).isdigit()
            or not isinstance(data.get("lecture"), str)
            or not isinstance(data.get("nonce"), str)
            or len(data["nonce"]) < 20
        ):
            return None

        return data
    except Exception:
        return None


def authorized(request, lecture_id):
    sid = request.cookies.get(COOKIE, "")

    if len(sid) < 32:
        return False

    digest = hashlib.sha256(sid.encode()).hexdigest()

    with connect() as db:
        row = db.execute(
            """
            SELECT lecture_id, expires_at
            FROM sessions
            WHERE session_hash = ?
            """,
            (digest,)
        ).fetchone()

    return bool(
        row
        and row[0] == lecture_id
        and row[1] > int(time.time())
    )


def install(app):

    @app.post("/session/bootstrap")
    async def bootstrap(request: Request):
        if request.headers.get("origin") != ALLOWED_ORIGIN:
            return JSONResponse(
                {"error": "Origin not allowed"},
                status_code=403
            )

        try:
            body = await request.json()
            ticket = str(body.get("ticket", ""))
        except Exception:
            return JSONResponse(
                {"error": "Invalid request"},
                status_code=400
            )

        data = verify_ticket(ticket)

        if not data:
            return JSONResponse(
                {"error": "Invalid or expired session ticket"},
                status_code=403
            )

        now = int(time.time())
        sid = secrets.token_urlsafe(48)
        digest = hashlib.sha256(sid.encode()).hexdigest()

        # Each login is limited to a maximum of 24 hours.
        expiry = now + 86400

        try:
            with connect() as db:
                db.execute(
                    """
                    INSERT INTO used_tickets (nonce, expires_at)
                    VALUES (?, ?)
                    """,
                    (data["nonce"], int(data["exp"]))
                )
                db.execute(
                    """
                    INSERT INTO sessions
                    (session_hash, telegram_user_id, lecture_id, expires_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        digest,
                        str(data["uid"]),
                        data["lecture"],
                        expiry
                    )
                )
        except sqlite3.IntegrityError:
            return JSONResponse(
                {"error": "Session ticket already used"},
                status_code=403
            )

        response = JSONResponse({
            "ok": True,
            "expires_at": expiry
        })

        response.set_cookie(
            key=COOKIE,
            value=sid,
            max_age=86400,
            secure=True,
            httponly=True,
            samesite="lax",
            path="/"
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.middleware("http")
    async def protect_lecture_media(request, call_next):
        path = request.url.path

        if not ENFORCE_FILE.exists():
            return await call_next(request)

        lecture_id = None

        if path.startswith("/video/"):
            lecture_id = path.split("/")[2]

        elif path.startswith("/slides/"):
            parts = path.split("/")
            if len(parts) > 2:
                lecture_id = parts[2]

        if (
            lecture_id
            and request.method != "OPTIONS"
            and not authorized(request, lecture_id)
        ):
            return JSONResponse(
                {"detail": "Private playback session required"},
                status_code=403,
                headers={"Cache-Control": "no-store"}
            )

        return await call_next(request)
