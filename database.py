"""
database.py

SQLAlchemy 2.x database configuration for TiDB Cloud
(MySQL wire protocol via PyMySQL).

Designed for:
- TiDB Cloud
- Render web services
- Local development
- SQLAlchemy 2.x

Important:
- Database credentials come from config.py / environment variables.
- Do not hard-code credentials here.
- TiDB transactions are explicitly configured as pessimistic.
- expire_on_commit=False is intentional because handlers may access
  ORM attributes after commit.
- Retry helpers are provided for transient TiDB write conflicts.
"""

from __future__ import annotations

import functools
import logging
import time
from contextlib import contextmanager
from urllib.parse import quote_plus

from sqlalchemy import create_engine, event
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, declarative_base, sessionmaker

from config import (
    MYSQL_USER,
    MYSQL_PASSWORD,
    MYSQL_HOST,
    MYSQL_PORT,
    MYSQL_DB,
    MYSQL_SSL_CA,
    DATABASE_POOL_SIZE,
    DATABASE_MAX_OVERFLOW,
    DATABASE_POOL_RECYCLE,
    DATABASE_POOL_TIMEOUT,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# DATABASE CONNECTION
# ---------------------------------------------------------------------------

# Escape the password so special characters such as @, :, /, #, etc.
# do not break the SQLAlchemy connection URL.
encoded_password = quote_plus(str(MYSQL_PASSWORD))

DATABASE_URL = (
    "mysql+pymysql://"
    f"{MYSQL_USER}:"
    f"{encoded_password}@"
    f"{MYSQL_HOST}:"
    f"{MYSQL_PORT}/"
    f"{MYSQL_DB}"
)

logger.info(
    "Connecting to TiDB Cloud | host=%s port=%s db=%s user=%s",
    MYSQL_HOST,
    MYSQL_PORT,
    MYSQL_DB,
    MYSQL_USER,
)


# ---------------------------------------------------------------------------
# ENGINE
# ---------------------------------------------------------------------------
#
# The pool size is configured through environment variables so it can match
# the database plan and the Render service's expected concurrency.
#

engine = create_engine(
    DATABASE_URL,

    # Verify pooled connections before using them.
    pool_pre_ping=True,

    # Recycle connections before an intermediary/LB/TiDB closes them.
    pool_recycle=3600,

    # Use the deployment-configured pool. The old fixed pool of two caused
    # normal commands to queue behind payment checks that use the database.
    pool_size=DATABASE_POOL_SIZE,
    max_overflow=DATABASE_MAX_OVERFLOW,

    # Do not wait forever for a connection.
    pool_timeout=DATABASE_POOL_TIMEOUT,

    # Roll back any unfinished transaction when a connection returns
    # to the pool.
    pool_reset_on_return="rollback",

    echo=False,
    # Exception logs must not include bound account details or notification bodies.
    hide_parameters=True,

    # TiDB Cloud requires TLS.
    connect_args={
        "ssl_ca": MYSQL_SSL_CA,
        "ssl_verify_cert": True,
        "ssl_verify_identity": True,
    },
)


# ---------------------------------------------------------------------------
# TiDB SESSION OPTIONS
# ---------------------------------------------------------------------------

from services.performance import install_database_observers
install_database_observers(engine)


@event.listens_for(engine, "connect")
def _set_tidb_session_options(
    dbapi_connection,
    connection_record,
):
    """
    Configure every newly-created physical DB connection.

    TiDB supports pessimistic transactions, which are required for
    SELECT ... FOR UPDATE to provide actual row locking.

    This is intentionally configured per connection instead of relying
    on the TiDB cluster's global/default transaction mode.

    If the application is pointed at ordinary MySQL during local
    development, tidb_txn_mode may not exist. In that case we log the
    issue and allow the connection to continue.
    """

    cursor = dbapi_connection.cursor()

    try:
        cursor.execute(
            "SET SESSION tidb_txn_mode = 'pessimistic'"
        )

    except Exception:
        logger.warning(
            "Could not set tidb_txn_mode=pessimistic. "
            "The database may not be TiDB. Continuing with "
            "the database default.",
            exc_info=True,
        )

    finally:
        cursor.close()


# ---------------------------------------------------------------------------
# SESSION FACTORY
# ---------------------------------------------------------------------------

SessionLocal = sessionmaker(
    bind=engine,
    autocommit=False,
    autoflush=False,

    # IMPORTANT:
    #
    # After commit(), SQLAlchemy normally expires ORM attributes.
    # Setting this to False allows handlers to continue accessing
    # attributes such as:
    #
    #     user.balance
    #     order.id
    #     product.stock
    #
    # after db.commit().
    expire_on_commit=False,
)


# ---------------------------------------------------------------------------
# ORM BASE
# ---------------------------------------------------------------------------

Base = declarative_base()


# ---------------------------------------------------------------------------
# DATABASE DEPENDENCY
# ---------------------------------------------------------------------------

def get_db():
    """
    Generator-style database dependency.

    Example:

        db = next(get_db())

    or in code using dependency injection:

        db: Session = Depends(get_db)

    The session is always closed after use.
    """

    db = SessionLocal()

    try:
        yield db

    finally:
        db.close()


# ---------------------------------------------------------------------------
# TRANSACTION HELPER
# ---------------------------------------------------------------------------

@contextmanager
def transaction():
    """
    Execute a complete operation inside one database transaction.

    The transaction:

        - commits when the block exits successfully
        - rolls back when an exception occurs
        - always closes the session

    Example:

        with transaction() as db:
            user = (
                db.query(User)
                .filter(User.id == user_id)
                .with_for_update()
                .first()
            )

            user.balance -= amount

            db.add(order)

    This makes multi-step operations atomic.

    For example:

        balance deduction
        +
        stock deduction
        +
        order creation

    will either all commit or all roll back.
    """

    db: Session = SessionLocal()

    try:
        yield db
        db.commit()

    except Exception:
        db.rollback()
        raise

    finally:
        db.close()


# ---------------------------------------------------------------------------
# RETRYABLE TiDB ERRORS
# ---------------------------------------------------------------------------

#
# TiDB can return transient errors when concurrent transactions collide
# or when schema information changes while requests are running.
#
# Common retryable codes:
#
# 9007 -> Write conflict
# 1105 -> Various transient/schema-related TiDB errors
# 8022 -> TiDB transaction-related transient error
# 8028 -> TiDB transaction-related transient error
# 1213 -> Deadlock
#

_RETRYABLE_TIDB_ERROR_CODES = (
    9007,
    1105,
    8022,
    8028,
    1213,
)


def _is_retryable(exc: Exception) -> bool:
    """
    Return True if the exception is a retryable TiDB OperationalError.
    """

    if not isinstance(exc, OperationalError):
        return False

    original = getattr(exc, "orig", None)

    if original is None:
        return False

    args = getattr(original, "args", ())

    if not args:
        return False

    code = args[0]

    try:
        code = int(code)
    except (TypeError, ValueError):
        return False

    return code in _RETRYABLE_TIDB_ERROR_CODES


# ---------------------------------------------------------------------------
# TRANSACTION RETRY DECORATOR
# ---------------------------------------------------------------------------

def retry_on_write_conflict(
    max_attempts: int = 3,
    base_delay: float = 0.05,
):
    """
    Retry a complete transactional operation when TiDB reports a
    transient write conflict, deadlock, or related retryable error.

    IMPORTANT:

    The decorated function should represent ONE complete transaction
    attempt.

    Good:

        @retry_on_write_conflict()
        def create_order(...):
            with transaction() as db:
                ...
                db.add(order)

    Bad:

        @retry_on_write_conflict()
        def something():
            db.commit()
            do_something_else()
            db.commit()

    The retry must be able to safely repeat the ENTIRE operation.
    """

    if max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")

    if base_delay < 0:
        raise ValueError("base_delay cannot be negative")

    def decorator(func):

        @functools.wraps(func)
        def wrapper(*args, **kwargs):

            last_exception = None

            for attempt in range(1, max_attempts + 1):

                try:
                    return func(*args, **kwargs)

                except OperationalError as exc:

                    # Immediately re-raise non-retryable errors.
                    if not _is_retryable(exc):
                        raise

                    # Do not retry after the final attempt.
                    if attempt >= max_attempts:
                        raise

                    last_exception = exc

                    # Exponential backoff:
                    #
                    # attempt 1 -> base_delay
                    # attempt 2 -> base_delay * 2
                    # attempt 3 -> base_delay * 4
                    #
                    delay = base_delay * (2 ** (attempt - 1))

                    logger.warning(
                        "Retryable TiDB error on attempt "
                        "%s/%s for %s: %s. "
                        "Retrying in %.2f seconds.",
                        attempt,
                        max_attempts,
                        func.__name__,
                        exc,
                        delay,
                    )

                    time.sleep(delay)

            # This should never normally execute.
            if last_exception is not None:
                raise last_exception

            raise RuntimeError(
                f"{func.__name__} failed without an exception"
            )

        return wrapper

    return decorator


# ---------------------------------------------------------------------------
# OPTIONAL SIMPLE RETRY HELPER
# ---------------------------------------------------------------------------

def run_with_retry(
    func,
    *args,
    max_attempts: int = 3,
    base_delay: float = 0.05,
    **kwargs,
):
    """
    Execute a callable with TiDB retry handling.

    The callable must contain the COMPLETE transactional operation.

    Example:

        def create_order():
            with transaction() as db:
                ...

        result = run_with_retry(create_order)
    """

    if max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")

    last_exception = None

    for attempt in range(1, max_attempts + 1):

        try:
            return func(*args, **kwargs)

        except OperationalError as exc:

            if not _is_retryable(exc):
                raise

            if attempt >= max_attempts:
                raise

            last_exception = exc

            delay = base_delay * (2 ** (attempt - 1))

            logger.warning(
                "Retryable TiDB error on attempt "
                "%s/%s for %s: %s. "
                "Retrying in %.2f seconds.",
                attempt,
                max_attempts,
                getattr(func, "__name__", repr(func)),
                exc,
                delay,
            )

            time.sleep(delay)

    if last_exception is not None:
        raise last_exception

    raise RuntimeError("Database operation failed without an exception")
