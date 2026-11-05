"""One-time Telegram-generated login codes and revocable browser sessions."""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from database import SessionLocal
from models.manager_auth import (
    ManagerAuthSession, ManagerAuthState, ManagerLoginAttempt, ManagerLoginChallenge,
)

CHALLENGE_TTL_SECONDS = 300
LOGIN_FAILURE_LIMIT = 5
LOGIN_WINDOW_SECONDS = 15 * 60


def ensure_auth_state() -> None:
    """Initialize the singleton session pointer during manager startup."""
    db = SessionLocal()
    try:
        if db.query(ManagerAuthState.id).filter(ManagerAuthState.id == 1).first() is None:
            db.add(ManagerAuthState(id=1, active_token_hash=None, updated_at=_now()))
            try:
                db.commit()
            except Exception:
                db.rollback()
                # Another web worker may have inserted the singleton concurrently.
                if db.query(ManagerAuthState.id).filter(ManagerAuthState.id == 1).first() is None:
                    raise
    finally:
        db.close()


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _login_buckets(client_host: str, username: str) -> tuple[str, str]:
    host = client_host or "unknown"
    normalized_user = (username or "").strip().casefold()
    return (_hash("ip:" + host), _hash("pair:" + host + "\0" + normalized_user))


def login_is_limited(client_host: str, username: str) -> bool:
    now = _now()
    db = SessionLocal()
    try:
        rows = db.query(ManagerLoginAttempt).filter(
            ManagerLoginAttempt.bucket_hash.in_(_login_buckets(client_host, username))
        ).all()
        return any(row.locked_until and row.locked_until > now for row in rows)
    finally:
        db.close()


def record_login_failure(client_host: str, username: str) -> None:
    now = _now()
    db = SessionLocal()
    try:
        for bucket in _login_buckets(client_host, username):
            row = db.query(ManagerLoginAttempt).filter(
                ManagerLoginAttempt.bucket_hash == bucket
            ).with_for_update().first()
            if row is None:
                row = ManagerLoginAttempt(bucket_hash=bucket, failures=0, window_started=now)
                db.add(row)
            if row.window_started + timedelta(seconds=LOGIN_WINDOW_SECONDS) <= now:
                row.failures = 0
                row.window_started = now
                row.locked_until = None
            row.failures += 1
            if row.failures >= LOGIN_FAILURE_LIMIT:
                row.locked_until = now + timedelta(seconds=LOGIN_WINDOW_SECONDS)
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def clear_login_failures(client_host: str, username: str) -> None:
    db = SessionLocal()
    try:
        db.query(ManagerLoginAttempt).filter(
            ManagerLoginAttempt.bucket_hash.in_(_login_buckets(client_host, username))
        ).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()


def create_challenge(telegram_id: int) -> tuple[str, datetime]:
    """Generate a private, one-time login code for an authorized bot admin."""
    raw = secrets.token_urlsafe(24)  # 192 random bits, rendered as 32 characters.
    now = _now()
    expires = now + timedelta(seconds=CHALLENGE_TTL_SECONDS)
    db = SessionLocal()
    try:
        # Keep only one live code per admin. Older codes are invalidated.
        db.query(ManagerLoginChallenge).filter(
            ManagerLoginChallenge.approved_by == telegram_id,
            ManagerLoginChallenge.consumed_at.is_(None),
        ).update({ManagerLoginChallenge.consumed_at: now}, synchronize_session=False)
        db.add(ManagerLoginChallenge(
            id=secrets.token_hex(16), token_hash=_hash(raw), created_at=now,
            expires_at=expires, approved_at=now, approved_by=telegram_id,
        ))
        db.commit()
    finally:
        db.close()
    return raw, expires


def create_login_challenge() -> tuple[str, datetime]:
    """Create a browser-bound Telegram approval request; only its hash is stored."""
    raw = secrets.token_urlsafe(24)
    now = _now()
    expires = now + timedelta(seconds=CHALLENGE_TTL_SECONDS)
    db = SessionLocal()
    try:
        db.add(ManagerLoginChallenge(
            id=secrets.token_hex(16), token_hash=_hash(raw), created_at=now,
            expires_at=expires,
        ))
        db.commit()
    finally:
        db.close()
    return raw, expires


def approve_login_challenge(raw: str, telegram_id: int, allowed_telegram_ids) -> bool:
    """Approve a pending browser request only from an explicitly allowed admin."""
    if telegram_id not in allowed_telegram_ids:
        return False
    now = _now()
    db = SessionLocal()
    try:
        challenge = db.query(ManagerLoginChallenge).filter(
            ManagerLoginChallenge.token_hash == _hash(raw)
        ).with_for_update().first()
        if (not challenge or challenge.approved_at or challenge.consumed_at
                or challenge.expires_at <= now):
            return False
        challenge.approved_at = now
        challenge.approved_by = telegram_id
        db.commit()
        return True
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def cancel_login_challenge(raw: str) -> None:
    if not raw:
        return
    db = SessionLocal()
    try:
        challenge = db.query(ManagerLoginChallenge).filter(
            ManagerLoginChallenge.token_hash == _hash(raw),
            ManagerLoginChallenge.consumed_at.is_(None),
        ).with_for_update().first()
        if challenge:
            challenge.consumed_at = _now()
            db.commit()
    finally:
        db.close()


SESSION_DURATIONS_MINUTES = (45, 60, 120, 180)


def issue_session_by_code(raw: str, allowed_telegram_ids=None, duration_minutes: int = 45,
                          client_ip: str = "unknown") -> tuple[str, int] | None:
    """Consume a generated code once and return a new opaque session token."""
    if duration_minutes not in SESSION_DURATIONS_MINUTES:
        raise ValueError("Unsupported manager session duration.")
    token = secrets.token_urlsafe(32)
    now = _now()
    db = SessionLocal()
    try:
        challenge = db.query(ManagerLoginChallenge).filter(
            ManagerLoginChallenge.token_hash == _hash(raw)
        ).with_for_update().first()
        if (not challenge or not challenge.approved_at or challenge.consumed_at
                or challenge.expires_at <= now or not challenge.approved_by):
            return None
        if allowed_telegram_ids is not None and challenge.approved_by not in allowed_telegram_ids:
            return None
        challenge.consumed_at = now
        state = db.query(ManagerAuthState).filter(
            ManagerAuthState.id == 1
        ).with_for_update().first()
        if state is None:
            state = ManagerAuthState(id=1, active_token_hash=None, updated_at=now)
            db.add(state)
            db.flush()
        # Revoke all older sessions. Tabs in the same browser keep working because
        # the browser shares one cookie; other browsers immediately lose access.
        db.query(ManagerAuthSession).filter(
            ManagerAuthSession.revoked_at.is_(None)
        ).update({ManagerAuthSession.revoked_at: now}, synchronize_session=False)
        db.add(ManagerAuthSession(
            token_hash=_hash(token), telegram_id=challenge.approved_by,
            client_ip=client_ip, created_at=now,
            expires_at=now + timedelta(minutes=duration_minutes),
        ))
        state.active_token_hash = _hash(token)
        state.updated_at = now
        db.commit()
        return token, challenge.approved_by
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def session_status(token: str, client_ip: str) -> tuple[str, datetime | None]:
    """Return valid, expired, ip_changed, or invalid for the active browser token."""
    if not token:
        return "invalid", None
    db = SessionLocal()
    try:
        row = db.query(ManagerAuthSession.id, ManagerAuthSession.client_ip,
                       ManagerAuthSession.expires_at).join(
            ManagerAuthState,
            ManagerAuthState.active_token_hash == ManagerAuthSession.token_hash,
        ).filter(
            ManagerAuthState.id == 1,
            ManagerAuthSession.token_hash == _hash(token),
            ManagerAuthSession.revoked_at.is_(None),
        ).first()
        if not row:
            return "invalid", None
        if row.expires_at <= _now():
            revoke = True
            result = ("expired", row.expires_at)
        elif row.client_ip != client_ip:
            revoke = True
            result = ("ip_changed", row.expires_at)
        else:
            revoke = False
            result = ("valid", row.expires_at)
    finally:
        db.close()
    if revoke:
        revoke_session(token)
    return result


def valid_session(token: str, client_ip: str = "unknown") -> bool:
    return session_status(token, client_ip)[0] == "valid"


def session_owner(token: str, client_ip: str):
    """Server-side actor identity for fulfillment; never trust a submitted admin ID."""
    if not valid_session(token, client_ip):
        return None
    with SessionLocal() as db:
        row = db.query(ManagerAuthSession.telegram_id).filter(
            ManagerAuthSession.token_hash == _hash(token), ManagerAuthSession.revoked_at.is_(None),
            ManagerAuthSession.expires_at > _now()).first()
        return row.telegram_id if row else None


def revoke_session(token: str) -> None:
    if not token:
        return
    db = SessionLocal()
    try:
        token_hash = _hash(token)
        state = db.query(ManagerAuthState).filter(
            ManagerAuthState.id == 1,
            ManagerAuthState.active_token_hash == token_hash,
        ).with_for_update().first()
        item = db.query(ManagerAuthSession).filter(
            ManagerAuthSession.token_hash == token_hash,
            ManagerAuthSession.revoked_at.is_(None),
        ).with_for_update().first()
        if item:
            item.revoked_at = _now()
            if state:
                state.active_token_hash = None
                state.updated_at = _now()
            db.commit()
    finally:
        db.close()
