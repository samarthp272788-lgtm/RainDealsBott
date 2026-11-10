import logging
import threading
import time

from database import SessionLocal, engine
from models.maintenance_setting import MaintenanceSetting

logger = logging.getLogger(__name__)
_cache_lock = threading.Lock()
_cached_value: bool | None = None
_cached_at = 0.0
_cache_ttl = 1.0


def ensure_maintenance_schema() -> None:
    try:
        MaintenanceSetting.__table__.create(bind=engine, checkfirst=True)
    except Exception:
        # Multiple replicas can race through checkfirst. Accept that race only
        # when the table is now present; otherwise fail startup visibly.
        from sqlalchemy import inspect
        if not inspect(engine).has_table(MaintenanceSetting.__tablename__):
            raise


def is_maintenance_enabled() -> bool:
    global _cached_value, _cached_at
    now = time.monotonic()
    with _cache_lock:
        if _cached_value is not None and now - _cached_at < _cache_ttl:
            return _cached_value
    db = SessionLocal()
    try:
        setting = db.query(MaintenanceSetting).filter(MaintenanceSetting.id == 1).first()
        value = bool(setting and setting.enabled)
        with _cache_lock:
            _cached_value, _cached_at = value, time.monotonic()
        return value
    except Exception:
        # A database outage must not silently disable an explicitly enabled
        # maintenance lock. Admins are exempt in the middleware.
        logger.exception("Could not read maintenance-mode setting; failing closed")
        with _cache_lock:
            _cached_value, _cached_at = True, time.monotonic()
        return True
    finally:
        db.close()


def set_maintenance_enabled(enabled: bool) -> None:
    global _cached_value, _cached_at
    db = SessionLocal()
    try:
        setting = db.query(MaintenanceSetting).filter(MaintenanceSetting.id == 1).first()
        if setting is None:
            setting = MaintenanceSetting(id=1, enabled=enabled)
            db.add(setting)
        else:
            setting.enabled = enabled
        db.commit()
        with _cache_lock:
            _cached_value, _cached_at = bool(enabled), time.monotonic()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
