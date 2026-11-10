import logging

from database import engine
from models.order import Order
from models.order_rating import OrderRating

logger = logging.getLogger(__name__)


def ensure_order_rating_schema() -> None:
    try:
        OrderRating.__table__.create(bind=engine, checkfirst=True)
    except Exception:
        from sqlalchemy import inspect
        if not inspect(engine).has_table(OrderRating.__tablename__):
            raise
