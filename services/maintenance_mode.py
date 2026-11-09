import logging

from database import SessionLocal, engine
from models.maintenance_setting import MaintenanceSetting

logger = logging.getLogger(__name__)


def ensure_maintenance_schema() -> None:
    try:
        MaintenanceSetting.__table__.create(bind=engine, checkfirst=True)
    except Exception:
        logger.exception("Could not initialize maintenance-mode storage; bot will continue")


def is_maintenance_enabled() -> bool:
    db = SessionLocal()
    try:
        setting = db.query(MaintenanceSetting).filter(MaintenanceSetting.id == 1).first()
        return bool(setting and setting.enabled)
    except Exception:
        # A database outage must not silently disable an explicitly enabled
        # maintenance lock. Admins are exempt in the middleware.
        logger.exception("Could not read maintenance-mode setting; failing closed")
        return True
    finally:
        db.close()


def set_maintenance_enabled(enabled: bool) -> None:
    db = SessionLocal()
    try:
        setting = db.query(MaintenanceSetting).filter(MaintenanceSetting.id == 1).first()
        if setting is None:
            setting = MaintenanceSetting(id=1, enabled=enabled)
            db.add(setting)
        else:
            setting.enabled = enabled
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
