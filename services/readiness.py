"""Small readiness probe that reports only database/schema availability."""
from sqlalchemy import inspect, text
from database import SessionLocal, engine


def database_readiness(require_polling_lease: bool = False) -> dict:
    required = {'bot_warranty_request_revisions'}
    if require_polling_lease:
        required.add('bot_polling_leases')
    db = SessionLocal()
    try:
        db.execute(text('SELECT 1'))
        inspector = inspect(engine)
        missing = sorted(table for table in required if not inspector.has_table(table))
        connected = not missing
        return {'status': 'ready' if connected else 'not_ready',
                'database_connected': True, 'schema_ready': connected,
                'missing_schema': missing}
    except Exception:
        return {'status': 'not_ready', 'database_connected': False,
                'schema_ready': False, 'missing_schema': sorted(required)}
    finally:
        db.close()
