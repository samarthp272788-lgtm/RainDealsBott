"""Schema setup for archived warranty/refund claim attempts."""
from database import engine
from models.bot_operations import WarrantyRequestRevision


def ensure_warranty_revision_schema() -> None:
    try:
        WarrantyRequestRevision.__table__.create(bind=engine, checkfirst=True)
    except Exception:
        from sqlalchemy import inspect
        if not inspect(engine).has_table(WarrantyRequestRevision.__tablename__):
            raise
