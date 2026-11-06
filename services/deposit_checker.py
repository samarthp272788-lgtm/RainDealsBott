# services/deposit_checker.py

"""
services/deposit_checker.py

BINANCE-RECEIPT CRYPTO DEPOSIT CHECKER

Crypto:
    Binance signed deposit-history API only

Supported:
    USDT

Networks:
    BSC / BEP20
    Polygon

Binance verifies:
    - txId
    - coin
    - network
    - destination address
    - amount
    - status

The sender is not restricted: Binance's successful deposit record is the
source of truth for funds that Binance has actually credited.

UPI:
    Existing IMAP verification

Binance Pay:
    Existing Binance Pay verification
"""

from __future__ import annotations

import asyncio
import email
import hashlib
import hmac
import imaplib
import json
import logging
import os
import re
import time as time_module

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from email.header import decode_header
from typing import Optional, Union

import requests

from binance.client import Client
from binance.exceptions import BinanceAPIException

from web3 import Web3
from web3.exceptions import TransactionNotFound

from database import SessionLocal
from services.notification_outbox import queue_message
from models.deposit import Deposit
from models.user import User

import config

logger = logging.getLogger(__name__)

# ================================================================
# CONFIGURATION
# ================================================================

BINANCE_API_KEY = getattr(config, "BINANCE_API_KEY", "")
BINANCE_API_SECRET = getattr(config, "BINANCE_API_SECRET", "")

DEPOSIT_COINS = {"USDT"} & {
    str(coin).upper()
    for coin in getattr(config, "BINANCE_DEPOSIT_COINS", ["USDT"])
}
BINANCE_CLAIM_WINDOW = timedelta(
    hours=max(1, int(getattr(config, "BINANCE_DEPOSIT_CLAIM_WINDOW_HOURS", 72)))
)

# ================================================================
# WALLET ADDRESSES
# ================================================================

BEP20_ADDRESS = str(
    getattr(config, "BEP20_ADDRESS", "") or ""
).strip().lower()

POLYGON_ADDRESS = str(
    getattr(config, "POLYGON_ADDRESS", "") or ""
).strip().lower()

# ================================================================
# RPC CONFIGURATION
# ================================================================

BSC_RPC_URLS = getattr(
    config,
    "BSC_RPC_URLS",
    [
        getattr(
            config,
            "BSC_RPC_URL",
            "https://bsc-dataseed.binance.org/",
        )
    ],
)

POLYGON_RPC_URLS = getattr(
    config,
    "POLYGON_RPC_URLS",
    [
        getattr(
            config,
            "POLYGON_RPC_URL",
            "https://polygon-rpc.com/",
        )
    ],
)


def _normalise_rpc_urls(value) -> list[str]:
    if isinstance(value, str):
        return [
            x.strip()
            for x in value.split(",")
            if x.strip()
        ]

    if isinstance(value, (list, tuple)):
        return [
            str(x).strip()
            for x in value
            if str(x).strip()
        ]

    return []


BSC_RPC_URLS = _normalise_rpc_urls(BSC_RPC_URLS)
POLYGON_RPC_URLS = _normalise_rpc_urls(POLYGON_RPC_URLS)

# ================================================================
# CONFIRMATIONS
# ================================================================

RPC_CONFIRMATIONS = {
    "BEP20": int(
        getattr(config, "BSC_REQUIRED_CONFIRMATIONS", 3)
    ),
    "POLYGON": int(
        getattr(config, "POLYGON_REQUIRED_CONFIRMATIONS", 3)
    ),
}

# ================================================================
# RPC TIMEOUT
# ================================================================

RPC_TIMEOUT = int(
    getattr(config, "RPC_TIMEOUT_SECONDS", 10)
)

# ================================================================
# RPC CACHE
# ================================================================

RPC_CACHE_TTL = int(
    getattr(config, "RPC_CACHE_TTL_SECONDS", 5)
)

_rpc_cache = {}

# ================================================================
# TOKEN CONTRACTS
# ================================================================

OFFICIAL_CONTRACTS = {
    "BEP20": {
        "USDT": "0x55d398326f99059ff775485246999027b3197955",
        "BUSD": "0xe9e7cea3dedca5984780bafc599bd69add087d56",
        "USDC": "0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d",
    },

    "POLYGON": {
        "USDT": "0xc2132d05d31c914a87c6611c10748aeb04b58e8f",
        "USDC": "0x3c499c542cef5e3811e1192ce70d8cc03d5c3359",
    },
}

# ================================================================
# TOKEN DECIMALS
# ================================================================

TOKEN_DECIMALS = {
    "BEP20": {
        "USDT": 18,
        "BUSD": 18,
        "USDC": 18,
    },

    "POLYGON": {
        "USDT": 6,
        "USDC": 6,
    },
}


# ================================================================
# NETWORKS
# ================================================================

@dataclass
class Chain:
    name: str
    address: str
    binance_network: str
    rpc_urls: list[str]
    contracts: dict = field(default_factory=dict)
    chain_id: int = 0


CHAINS = {
    "BEP20": Chain(
        name="BEP20",
        address=BEP20_ADDRESS,
        binance_network=getattr(
            config,
            "BINANCE_BEP20_NETWORK",
            "BSC",
        ),
        rpc_urls=BSC_RPC_URLS,
        contracts=OFFICIAL_CONTRACTS["BEP20"],
        chain_id=56,
    ),

    "POLYGON": Chain(
        name="POLYGON",
        address=POLYGON_ADDRESS,
        binance_network=getattr(
            config,
            "BINANCE_POLYGON_NETWORK",
            "MATIC",
        ),
        rpc_urls=POLYGON_RPC_URLS,
        contracts=OFFICIAL_CONTRACTS["POLYGON"],
        chain_id=137,
    ),
}

# ================================================================
# UPI
# ================================================================

UPI_NETWORK = "UPI"
UPI_ID = getattr(config, "UPI_ID", "")

IMAP_HOST = getattr(
    config,
    "IMAP_HOST",
    "imap.gmail.com",
)

IMAP_EMAIL = getattr(config, "IMAP_EMAIL", "")
IMAP_APP_PASSWORD = getattr(
    config,
    "IMAP_APP_PASSWORD",
    "",
)

FAMAPP_SENDER_EMAIL = getattr(
    config,
    "FAMAPP_SENDER_EMAIL",
    "",
)

IMAP_LOOKBACK_DAYS = int(
    getattr(config, "IMAP_LOOKBACK_DAYS", 1)
)

MAX_EMAILS_TO_SCAN = int(
    getattr(config,
            "UPI_MAX_EMAILS_TO_SCAN",
            40)
)

# ================================================================
# BINANCE PAY
# ================================================================

BINANCE_PAY_NETWORK = "BINANCE"

PAY_TRANSACTIONS_URL = (
    "https://api.binance.com/sapi/v1/pay/transactions"
)

PAY_TRANSACTION_MATCH_FIELDS = (
    "transactionId",
    "orderId",
    "id",
    "referenceId",
    "merchantTradeNo",
)

BINANCE_PAY_ACCEPTED_CURRENCIES = getattr(
    config,
    "BINANCE_PAY_ACCEPTED_CURRENCIES",
    ["USDT", "BUSD", "USDC"],
)

try:
    BINANCE_PAY_LOOKBACK_DAYS = int(
        getattr(
            config,
            "BINANCE_PAY_LOOKBACK_DAYS",
            7,
        )
    )
except (TypeError, ValueError):
    BINANCE_PAY_LOOKBACK_DAYS = 7

# ================================================================
# DEPOSIT SETTINGS
# ================================================================

try:
    AMOUNT_TOLERANCE = Decimal(
        str(
            getattr(
                config,
                "DEPOSIT_AMOUNT_TOLERANCE",
                "0.000001",
            )
        )
    )
except (InvalidOperation, ValueError):
    AMOUNT_TOLERANCE = Decimal("0.000001")

try:
    MAX_CHECK_ATTEMPTS = int(
        getattr(
            config,
            "DEPOSIT_MAX_CHECK_ATTEMPTS",
            180,
        )
    )
except (TypeError, ValueError):
    MAX_CHECK_ATTEMPTS = 10

MAX_CHECK_ATTEMPTS = max(1, MAX_CHECK_ATTEMPTS)

CHECK_INTERVAL = int(
    getattr(
        config,
        "DEPOSIT_CHECK_INTERVAL",
        10,
    )
)

MIN_DEPOSIT_USD = Decimal(
    str(
        getattr(
            config,
            "MIN_DEPOSIT_USD",
            "0.01",
        )
    )
)

# ================================================================
# UPI REGEX
# ================================================================

UTR_RE = re.compile(r"^\d{12}$")

TXN_ID_RE = re.compile(
    r"^(?:FMPIB)?\d{8,15}$",
    re.IGNORECASE,
)

UTR_SPECIFIC_RE = re.compile(
    r"(?:UTR(?:\s*No\.?)?|UPI\s*Ref(?:erence)?"
    r"(?:\s*No\.?)?|RRN)"
    r"[\s:\-]{0,10}(\d{12})",
    re.IGNORECASE,
)

TXN_ID_SPECIFIC_RE = re.compile(
    r"(?:Txn\s*(?:ID|Ref(?:erence)?)|"
    r"Transaction\s*ID|Reference\s*ID)"
    r"[\s:\-]{0,10}"
    r"([A-Za-z]{3,10}\d{6,15})",
    re.IGNORECASE,
)

UTR_FALLBACK_RE = re.compile(
    r"\b(\d{12})\b"
)

TXN_ID_FALLBACK_RE = re.compile(
    r"\b((?:FMPIB)?\d{8,15})\b",
    re.IGNORECASE,
)

AMOUNT_RE = re.compile(
    r"(?:₹|Rs\.?|INR)\s*"
    r"([\d,]+(?:\.\d{1,2})?)",
    re.IGNORECASE,
)

RECEIVED_AMOUNT_RE = re.compile(
    r"(?:Received|Credited|Payment\s+of|"
    r"Amount\s+Received|Paid)"
    r"[:\s]*"
    r"(?:₹|Rs\.?|INR)?\s*"
    r"([\d,]+(?:\.\d{1,2})?)",
    re.IGNORECASE,
)

TX_HASH_RE = re.compile(
    r"^0x[0-9a-fA-F]{64}$"
)

ORDER_ID_RE = re.compile(
    r"^[A-Za-z0-9]{8,32}$"
)

# ================================================================
# ERC20 TRANSFER EVENT
# ================================================================

TRANSFER_TOPIC = Web3.keccak(
    text="Transfer(address,address,uint256)"
).hex().lower()

# ================================================================
# STATE
# ================================================================

_check_attempts: dict[int, int] = {}

_binance_client: Optional[Client] = None

# Binance may reject all API requests from the server's jurisdiction.
_binance_access_restricted = False

# Avoid repeating the same manual-review warning on every checker cycle.
_binance_pending_skip_logged = False


def _is_binance_location_restriction(error) -> bool:
    """Return True only for Binance's documented eligibility restriction."""
    message = str(error or "").lower()

    return (
            "restricted location" in message
            or "eligibility" in message
            or "service unavailable from a restricted" in message
    )


def _mark_binance_access_restricted(error, context: str) -> None:
    """Open the process-local circuit breaker and log this terminal error once."""
    global _binance_access_restricted

    if _binance_access_restricted:
        return

    _binance_access_restricted = True

    logger.error(
        "Binance API disabled for this process after %s: %s. "
        "The server location or account is not eligible for Binance API use; "
        "Binance-only crypto and Binance Pay deposits will remain pending.",
        context,
        error,
    )


# ================================================================
# VALIDATORS
# ================================================================

def valid_hash(tx_hash: str) -> bool:
    return (
            isinstance(tx_hash, str)
            and bool(TX_HASH_RE.fullmatch(tx_hash.strip()))
    )


def valid_order_id(order_id: str) -> bool:
    return (
            isinstance(order_id, str)
            and bool(ORDER_ID_RE.fullmatch(order_id.strip()))
    )


def _normalize_upi_id(val: str) -> str:
    val = (val or "").strip()
    if val.isdigit() and len(val) != 12:
        return f"FMPIB{val}"
    return val.upper()


def valid_utr(utr: str) -> bool:
    return (
            isinstance(utr, str)
            and bool(
        UTR_RE.fullmatch(utr.strip())
        or TXN_ID_RE.fullmatch(utr.strip())
    )
    )


# ================================================================
# BINANCE CLIENT
# ================================================================

def _get_binance_client() -> Optional[Client]:
    global _binance_client

    if _binance_access_restricted:
        return None

    if not BINANCE_API_KEY or not BINANCE_API_SECRET:
        logger.warning(
            "Binance API credentials not configured"
        )
        return None

    if _binance_client is not None:
        return _binance_client

    try:
        client = Client(
            BINANCE_API_KEY,
            BINANCE_API_SECRET,
        )

        try:
            server_time = client.get_server_time()[
                "serverTime"
            ]

            client.timestamp_offset = (
                    server_time
                    - int(time_module.time() * 1000)
            )

        except Exception as exc:

            if _is_binance_location_restriction(exc):
                _mark_binance_access_restricted(
                    exc,
                    "server-time initialization",
                )

                return None

            logger.warning(
                "Binance server-time synchronization failed: %s",
                exc,
            )

        _binance_client = client

        logger.info("Binance client initialized")

        return _binance_client

    except Exception as exc:

        if _is_binance_location_restriction(exc):

            _mark_binance_access_restricted(
                exc,
                "client initialization",
            )

        else:

            logger.error(
                "Binance initialization failed: %s",
                exc,
            )

        _binance_client = None

        return None


# ================================================================
# RPC
# ================================================================

def _get_web3(chain: Chain) -> Optional[Web3]:
    """
    Return a working RPC connection.

    RPC URLs are tried in order.
    """

    if not chain.rpc_urls:
        logger.error(
            "[%s] No RPC URLs configured",
            chain.name,
        )
        return None

    cache_key = f"rpc:{chain.name}"

    cached = _rpc_cache.get(cache_key)

    if cached:
        created = cached.get("time", 0)

        if (
                time_module.time() - created
                < RPC_CACHE_TTL
        ):
            return cached["web3"]

    for rpc_url in chain.rpc_urls:

        try:
            provider = Web3.HTTPProvider(
                rpc_url,
                request_kwargs={
                    "timeout": RPC_TIMEOUT
                },
            )

            w3 = Web3(provider)

            if not w3.is_connected():
                logger.warning(
                    "[%s] RPC unavailable: %s",
                    chain.name,
                    rpc_url,
                )
                continue

            actual_chain_id = w3.eth.chain_id

            if actual_chain_id != chain.chain_id:
                logger.error(
                    "[%s] Wrong chain ID from RPC %s: %s",
                    chain.name,
                    rpc_url,
                    actual_chain_id,
                )
                continue

            _rpc_cache[cache_key] = {
                "web3": w3,
                "time": time_module.time(),
            }

            return w3

        except Exception as exc:
            logger.warning(
                "[%s] RPC failed %s: %s",
                chain.name,
                rpc_url,
                exc,
            )

    return None


# ================================================================
# RPC TRANSACTION VERIFICATION
# ================================================================

def _decode_transfer_logs(
        w3: Web3,
        receipt,
        chain: Chain,
        tx_hash: str,
        expected_recipient: str,
        expected_amount: Optional[Decimal],
):
    """
    Find the ERC20 Transfer event corresponding to
    the user's deposit.

    Does not trust token symbol/name.
    """

    expected_recipient = (
            expected_recipient or ""
    ).lower()

    transfers = []

    for log in receipt["logs"]:

        try:
            topics = log["topics"]

            if not topics:
                continue

            topic0 = topics[0].hex().lower()

            if topic0 != TRANSFER_TOPIC:
                continue

            token_contract = (
                log["address"].lower()
            )

            if token_contract not in {
                x.lower()
                for x in chain.contracts.values()
            }:
                continue

            if len(topics) < 3:
                continue

            from_address = (
                    "0x"
                    + topics[1].hex()[-40:]
            ).lower()

            to_address = (
                    "0x"
                    + topics[2].hex()[-40:]
            ).lower()

            if to_address != expected_recipient:
                continue

            raw_value = int(
                log["data"].hex(),
                16,
            )

            transfers.append(
                {
                    "token_contract": token_contract,
                    "from": from_address,
                    "to": to_address,
                    "raw_amount": raw_value,
                    "log_index": int(
                        log["logIndex"]
                    ),
                }
            )

        except Exception:
            logger.exception(
                "[%s] Failed decoding Transfer log",
                chain.name,
            )

    if not transfers:
        return None

    # ------------------------------------------------------------
    # Match amount if requested
    # ------------------------------------------------------------

    if expected_amount is not None:

        for transfer in transfers:

            contract = transfer[
                "token_contract"
            ]

            coin = None

            for name, address in chain.contracts.items():

                if address.lower() == contract:
                    coin = name
                    break

            if coin is None:
                continue

            decimals = TOKEN_DECIMALS[
                chain.name
            ].get(coin)

            if decimals is None:
                continue

            actual_amount = (
                    Decimal(
                        transfer["raw_amount"]
                    )
                    / (
                            Decimal(10)
                            ** decimals
                    )
            )

            if actual_amount >= (
                    expected_amount
                    - AMOUNT_TOLERANCE
            ):
                transfer[
                    "amount"
                ] = actual_amount

                transfer[
                    "coin"
                ] = coin

                return transfer

        return None

    # No requested amount.
    # Select the first valid transfer.

    transfer = transfers[0]

    contract = transfer[
        "token_contract"
    ]

    for name, address in chain.contracts.items():

        if address.lower() == contract:
            coin = name

            decimals = TOKEN_DECIMALS[
                chain.name
            ][coin]

            transfer["coin"] = coin

            transfer["amount"] = (
                    Decimal(
                        transfer["raw_amount"]
                    )
                    / (
                            Decimal(10)
                            ** decimals
                    )
            )

            return transfer

    return None


def _verify_rpc_transaction_sync(
        chain: Chain,
        tx_hash: str,
        requested_amount: Optional[Decimal],
):
    """
    Complete on-chain verification.

    Returns:
        dict  -> valid transaction
        None  -> not yet available / RPC temporary failure
        False -> permanently invalid
    """

    w3 = _get_web3(chain)

    if w3 is None:
        return None

    try:

        # --------------------------------------------------------
        # Transaction
        # --------------------------------------------------------

        try:
            tx = w3.eth.get_transaction(
                tx_hash
            )
        except TransactionNotFound:
            logger.info(
                "[%s] TX not found: %s",
                chain.name,
                tx_hash[:18],
            )
            return None

        # --------------------------------------------------------
        # Receipt
        # --------------------------------------------------------

        try:
            receipt = (
                w3.eth.get_transaction_receipt(
                    tx_hash
                )
            )

        except TransactionNotFound:
            logger.info(
                "[%s] TX not mined yet: %s",
                chain.name,
                tx_hash[:18],
            )
            return None

        # --------------------------------------------------------
        # Transaction status
        # --------------------------------------------------------

        status = int(
            receipt.get("status", 0)
        )

        if status != 1:
            logger.warning(
                "[%s] TX reverted: %s",
                chain.name,
                tx_hash[:18],
            )

            return False

        # --------------------------------------------------------
        # Block
        # --------------------------------------------------------

        tx_block = int(
            receipt["blockNumber"]
        )

        latest_block = int(
            w3.eth.block_number
        )

        confirmations = (
                latest_block
                - tx_block
                + 1
        )

        # --------------------------------------------------------
        # Recipient
        # --------------------------------------------------------

        expected_recipient = (
                chain.address or ""
        ).lower()

        if not expected_recipient:
            logger.error(
                "[%s] Deposit address not configured",
                chain.name,
            )

            return False

        # --------------------------------------------------------
        # Decode ERC20 Transfer
        # --------------------------------------------------------

        transfer = _decode_transfer_logs(
            w3=w3,
            receipt=receipt,
            chain=chain,
            tx_hash=tx_hash,
            expected_recipient=expected_recipient,
            expected_amount=requested_amount,
        )

        if transfer is None:
            logger.info(
                "[%s] No valid token transfer found: %s",
                chain.name,
                tx_hash[:18],
            )

            return None

        # --------------------------------------------------------
        # Confirmation requirement
        # --------------------------------------------------------

        required = RPC_CONFIRMATIONS.get(
            chain.name,
            3,
        )

        confirmed = (
                confirmations >= required
        )

        result = {
            "tx_hash": tx_hash,
            "sender": transfer["from"],
            "receiver": transfer["to"],
            "amount": transfer["amount"],
            "coin": transfer["coin"],
            "contract": transfer[
                "token_contract"
            ],
            "network": chain.name,
            "block_number": tx_block,
            "latest_block": latest_block,
            "confirmations": confirmations,
            "required_confirmations": required,
            "confirmed": confirmed,
            "rpc_verified": True,
            "tx_status": status,
            "transaction": tx,
            "receipt": receipt,
        }

        logger.info(
            "[%s] RPC | tx=%s | coin=%s | amount=%s | "
            "block=%s | confirmations=%s/%s | confirmed=%s",
            chain.name,
            tx_hash[:18],
            transfer["coin"],
            transfer["amount"],
            tx_block,
            confirmations,
            required,
            confirmed,
        )

        return result

    except Exception as exc:

        logger.warning(
            "[%s] RPC verification error: %s",
            chain.name,
            exc,
        )

        # Temporary RPC error.
        return None


async def verify_rpc_transaction(
        chain: Chain,
        tx_hash: str,
        requested_amount: Optional[Decimal],
):
    return await asyncio.to_thread(
        _verify_rpc_transaction_sync,
        chain,
        tx_hash,
        requested_amount,
    )


# ================================================================
# BINANCE DEPOSIT HISTORY
# ================================================================

def _fetch_binance_deposits_sync(
        tx_hash: str,
        claim_started_at: datetime,
) -> list[dict]:
    """Fetch only the Binance deposit record for this transaction hash."""
    client = _get_binance_client()

    if client is None or not tx_hash:
        return []

    end_time = int(
        time_module.time() * 1000
    )

    # Search from the start of the allowed claim window.
    # Binance requires a requested history window shorter than 90 days.
    claim_time = claim_started_at
    if claim_time.tzinfo is not None:
        claim_time = claim_time.astimezone(timezone.utc).replace(tzinfo=None)
    window_start = claim_time - BINANCE_CLAIM_WINDOW
    claim_start_time = int(window_start.replace(tzinfo=timezone.utc).timestamp() * 1000)
    max_window_start = end_time - 89 * 24 * 60 * 60 * 1000
    start_time = max(claim_start_time, max_window_start)

    try:
        rows = client.get_deposit_history(
            txId=tx_hash,
            startTime=start_time,
            endTime=end_time,
        ) or []
        return rows if isinstance(rows, list) else []
    except BinanceAPIException as exc:
        if _is_binance_location_restriction(exc):
            _mark_binance_access_restricted(exc, "deposit-history request")
        else:
            logger.warning("Binance deposit lookup failed for tx=%s: %s", tx_hash[:18], exc)
        return []
    except Exception as exc:
        if _is_binance_location_restriction(exc):
            _mark_binance_access_restricted(exc, "deposit-history request")
        else:
            logger.warning("Binance deposit lookup failed for tx=%s: %s", tx_hash[:18], exc)
        return []


# ================================================================
# BINANCE MATCH
# ================================================================

def _match_binance_row(
        chain: Chain,
        tx_hash: str,
        rows: list[dict],
        requested_amount: Optional[Decimal],
        claim_started_at: Optional[datetime] = None,
):
    row = next((item for item in rows if str(
        item.get("txId") or item.get("txid") or ""
    ).lower() == tx_hash.lower()), None)

    if row is None:
        return None

    # ------------------------------------------------------------
    # Status
    # ------------------------------------------------------------

    try:
        status = int(
            row.get("status", -1)
        )
    except Exception:
        status = -1

    if status in {0, 6, 8}:
        logger.info(
            "[%s] Binance deposit is not fully successful yet | tx=%s status=%s",
            chain.name,
            tx_hash[:18],
            status,
        )
        return None

    if status != 1:
        logger.warning("[%s] Binance rejected deposit tx=%s status=%s", chain.name, tx_hash[:18], status)
        return False

    # A recent deposit may be claimed for up to 72 hours after it was made,
    # measured against the time this user submitted its TXID.
    if claim_started_at is None:
        logger.warning(
            "[%s] Cannot verify Binance deposit without TXID submission time | tx=%s",
            chain.name,
            tx_hash[:18],
        )
        return False
    try:
        insert_time_ms = int(row.get("insertTime"))
        record_time = datetime.fromtimestamp(insert_time_ms / 1000, tz=timezone.utc).replace(tzinfo=None)
        claim_time = claim_started_at
        if claim_time.tzinfo is not None:
            claim_time = claim_time.astimezone(timezone.utc).replace(tzinfo=None)
        if record_time < claim_time - BINANCE_CLAIM_WINDOW:
            logger.warning(
                "[%s] Binance deposit is older than the claim window | tx=%s",
                chain.name,
                tx_hash[:18],
            )
            return False
    except (TypeError, ValueError, OverflowError, OSError):
        logger.warning(
            "[%s] Binance deposit record has no valid timestamp | tx=%s",
            chain.name,
            tx_hash[:18],
        )
        return False

    # ------------------------------------------------------------
    # Coin
    # ------------------------------------------------------------

    binance_coin = str(
        row.get("coin")
        or row.get("asset")
        or ""
    ).upper()
    allowed_coins = {str(coin).upper() for coin in DEPOSIT_COINS}
    if not binance_coin or binance_coin not in allowed_coins:
        logger.warning(
            "[%s] Binance deposit asset is not allowed | tx=%s coin=%s",
            chain.name,
            tx_hash[:18],
            binance_coin or "missing",
        )
        return False

    # ------------------------------------------------------------
    # Network
    # ------------------------------------------------------------

    row_network = str(
        row.get("network")
        or ""
    ).upper()

    expected_network = str(
        chain.binance_network
        or ""
    ).upper()

    if not expected_network or row_network != expected_network:
        logger.warning(
            "[%s] NETWORK MISMATCH | Binance=%s expected=%s",
            chain.name,
            row_network,
            expected_network,
        )

        return False

    # ------------------------------------------------------------
    # Address
    # ------------------------------------------------------------

    binance_address = str(
        row.get("address")
        or ""
    ).lower()

    expected_address = (
            chain.address or ""
    ).lower()

    if not expected_address or not binance_address or binance_address != expected_address:
        logger.warning(
            "[%s] ADDRESS MISMATCH",
            chain.name,
        )

        return False

    # ------------------------------------------------------------
    # Amount
    # ------------------------------------------------------------

    try:

        binance_amount = Decimal(
            str(
                row.get(
                    "amount",
                    "0",
                )
            )
        )

    except Exception:

        return False

    if binance_amount <= 0:
        logger.warning("[%s] Binance reported non-positive deposit amount | tx=%s", chain.name, tx_hash[:18])
        return False

    if requested_amount is not None and requested_amount > 0 and binance_amount < requested_amount - AMOUNT_TOLERANCE:
        logger.warning(
            "[%s] Binance deposit underpaid | tx=%s received=%s requested=%s",
            chain.name,
            tx_hash[:18],
            binance_amount,
            requested_amount,
        )
        return False

    # ------------------------------------------------------------
    # Everything matches
    # ------------------------------------------------------------

    logger.info(
        "[%s] BINANCE VERIFIED | tx=%s | coin=%s | amount=%s | status=%s",
        chain.name,
        tx_hash[:18],
        binance_coin,
        binance_amount,
        status,
    )

    return {
        "binance_verified": True,
        "binance_status": status,
        "binance_amount": binance_amount,
        "binance_coin": binance_coin,
        "binance_network": row_network,
        "binance_address": binance_address,
        "insert_time": row.get("insertTime"),
        "source_address": row.get("sourceAddress"),
    }


# ================================================================
# BINANCE-ONLY VERIFICATION
# ================================================================

async def verify_transaction(
        chain: Chain,
        tx_hash: str,
        requested_amount: Optional[Decimal] = None,
        claim_started_at: Optional[datetime] = None,
):
    """Verify an exchange deposit using Binance's signed history record only.

    A success record means Binance has recognized and credited the deposit.
    This intentionally does not query BSC/Polygon RPCs. It therefore only
    works for receiving addresses whose deposits appear in this Binance
    account's history.
    """

    if not valid_hash(tx_hash):
        return False

    # ------------------------------------------------------------
    if claim_started_at is None:
        logger.warning(
            "[%s] Cannot query Binance deposit without TXID submission time | tx=%s",
            chain.name,
            tx_hash[:18],
        )
        return False

    rows = await asyncio.to_thread(
        _fetch_binance_deposits_sync,
        tx_hash,
        claim_started_at,
    )

    binance_result = _match_binance_row(
        chain,
        tx_hash,
        rows,
        requested_amount,
        claim_started_at,
    )

    if binance_result is None:
        logger.info(
            "[%s] Binance deposit not indexed as successful yet | tx=%s",
            chain.name,
            tx_hash[:18],
        )

        return None

    if binance_result is False:
        return False

    # ------------------------------------------------------------
    # FINAL RESULT
    # ------------------------------------------------------------

    return {
        "sender": binance_result.get("source_address"),
        "receiver": binance_result["binance_address"],
        "amount": binance_result["binance_amount"],
        "coin": binance_result["binance_coin"],
        "network": binance_result["binance_network"],
        "contract": None,
        "confirmations": None,
        "required_confirmations": None,
        "rpc_verified": False,
        "binance_verified": True,
        "verified_by": [
            "BINANCE_DEPOSIT_HISTORY_TXID",
            "BINANCE_ASSET_NETWORK_ADDRESS_AMOUNT",
            "BINANCE_STATUS_SUCCESS",
        ],
        "binance_status": binance_result["binance_status"],
    }


# ================================================================
# UPI HELPERS
# ================================================================

def _decode_text(text) -> str:
    if text is None:
        return ""

    result = ""

    for value, encoding in decode_header(text):

        if isinstance(value, bytes):

            result += value.decode(
                encoding or "utf-8",
                errors="ignore",
            )

        else:

            result += value

    return result


def _get_body(msg) -> str:
    if msg.is_multipart():

        for part in msg.walk():

            if (
                    part.get_content_type()
                    == "text/plain"
                    and "attachment"
                    not in str(
                part.get(
                    "Content-Disposition"
                )
            )
            ):

                payload = part.get_payload(
                    decode=True
                )

                if payload:
                    return payload.decode(
                        errors="ignore"
                    )

        return ""

    payload = msg.get_payload(
        decode=True
    )

    return (
        payload.decode(errors="ignore")
        if payload
        else ""
    )


def _extract_amount(
        text: str,
        near_pos: Optional[int] = None,
):
    if not text:
        return None

    def to_decimal(raw):

        try:
            return Decimal(
                raw.replace(",", "")
            )

        except InvalidOperation:
            return None

    matches = list(
        RECEIVED_AMOUNT_RE.finditer(text)
    )

    if matches:

        if near_pos is not None:

            best = min(
                matches,
                key=lambda m: abs(
                    m.start(1)
                    - near_pos
                ),
            )

            value = to_decimal(
                best.group(1)
            )

            if value is not None:
                return value

        value = to_decimal(
            matches[0].group(1)
        )

        if value is not None:
            return value

    matches = list(
        AMOUNT_RE.finditer(text)
    )

    if matches:

        if near_pos is not None:

            best = min(
                matches,
                key=lambda m: abs(
                    m.start()
                    - near_pos
                ),
            )

            value = to_decimal(
                best.group(1)
            )

            if value is not None:
                return value

        return to_decimal(
            matches[0].group(1)
        )

    return None


def _extract_upi_identifiers(
        text: str,
) -> dict:
    result = {
        "utr": None,
        "txn_id": None,
        "amount": None,
    }

    if not text:
        return result

    utr_match = UTR_SPECIFIC_RE.search(
        text
    )

    if utr_match:
        result["utr"] = (
            utr_match.group(1)
        )

    txn_match = TXN_ID_SPECIFIC_RE.search(
        text
    )

    if txn_match:
        result["txn_id"] = (
            txn_match.group(1)
            if txn_match.group(1).upper().startswith("FMPIB")
            else "FMPIB" + txn_match.group(1)
        )

    if not result["utr"]:

        fallback = UTR_FALLBACK_RE.search(
            text
        )

        if fallback:
            result["utr"] = (
                fallback.group(1)
            )

    if not result["txn_id"]:

        fallback = TXN_ID_FALLBACK_RE.search(
            text
        )

        if fallback:
            result["txn_id"] = (
                fallback.group(1)
                if fallback.group(1).upper().startswith("FMPIB")
                else "FMPIB" + fallback.group(1)
            )

    pos = (
        utr_match.start()
        if utr_match
        else (
            txn_match.start()
            if txn_match
            else None
        )
    )

    result["amount"] = _extract_amount(
        text,
        pos,
    )

    return result


def _fetch_famapp_matches() -> dict:
    if (
            not IMAP_EMAIL
            or not IMAP_APP_PASSWORD
            or not FAMAPP_SENDER_EMAIL
    ):
        return {}

    matches = {}
    mail = None

    try:

        mail = imaplib.IMAP4_SSL(
            IMAP_HOST
        )

        mail.login(
            IMAP_EMAIL,
            IMAP_APP_PASSWORD,
        )

        mail.select("INBOX")

        since = (
                datetime.now()
                - timedelta(
            days=IMAP_LOOKBACK_DAYS
        )
        ).strftime("%d-%b-%Y")

        status, data = mail.search(
            None,
            f'(FROM "{FAMAPP_SENDER_EMAIL}" '
            f'SINCE {since})',
        )

        if status != "OK":
            return {}

        ids = data[0].split()

        ids = ids[
              -MAX_EMAILS_TO_SCAN:
              ]

        if not ids:
            return {}

        status, msg_data = mail.fetch(
            b",".join(ids),
            "(RFC822)",
        )

        if status != "OK":
            return {}

        for item in msg_data:

            if not isinstance(
                    item,
                    tuple,
            ):
                continue

            msg = email.message_from_bytes(
                item[1]
            )

            full = (
                f"{_decode_text(msg.get('Subject'))}\n"
                f"{_get_body(msg)}"
            )

            data = _extract_upi_identifiers(
                full
            )

            if data["amount"] is None:
                continue

            utr = data["utr"]
            txn_id = data["txn_id"]

            if not utr and not txn_id:
                continue

            payment = {
                "amount": data["amount"],
                "utr": utr,
                "txn_id": txn_id,
            }

            if utr:
                matches[
                    utr.upper()
                ] = payment

            if txn_id:
                matches[
                    txn_id.upper()
                ] = payment

        return matches

    except Exception:

        logger.exception(
            "UPI IMAP failed"
        )

        return {}

    finally:

        if mail:

            try:
                mail.logout()
            except Exception:
                pass


def _match_upi(
        deposit: Deposit,
        matches: dict,
):
    user_input = (
            deposit.tx_hash or ""
    ).strip()

    if not valid_utr(user_input):
        return False

    norm_input = _normalize_upi_id(user_input)

    payment = matches.get(
        norm_input
    )

    if payment is None:
        return None

    return {
        "sender": "UPI",
        "receiver": UPI_ID,
        "amount": payment["amount"],
        "inr_amount": payment["amount"],
        "utr": (
                payment.get("utr")
                or norm_input
        ),
        "txn_id": payment.get(
            "txn_id"
        ),
    }


async def verify_upi(
        deposit: Deposit,
):
    try:

        matches = await asyncio.to_thread(
            _fetch_famapp_matches
        )

        return _match_upi(
            deposit,
            matches,
        )

    except Exception:

        logger.exception(
            "UPI verification failed"
        )

        return None


# ================================================================
# BINANCE PAY
# ================================================================

def _sapi_sign(
        params: dict,
        secret: str,
) -> str:
    query = "&".join(
        f"{k}={v}"
        for k, v in params.items()
    )

    return hmac.new(
        secret.encode(),
        query.encode(),
        hashlib.sha256,
    ).hexdigest()


def _query_pay_trade_history_sync(
        lookback_days: int,
):
    if (
            not BINANCE_API_KEY
            or not BINANCE_API_SECRET
    ):
        return []

    client = _get_binance_client()

    if client is None:
        return []

    try:

        server_time = client.get_server_time()[
            "serverTime"
        ]

    except Exception as exc:

        if _is_binance_location_restriction(exc):
            _mark_binance_access_restricted(
                exc,
                "Binance Pay server-time request",
            )

        return []

    end_time = server_time

    start_time = (
            end_time
            - lookback_days
            * 24
            * 60
            * 60
            * 1000
    )

    params = {
        "startTime": start_time,
        "endTime": end_time,
        "recvWindow": 5000,
        "timestamp": server_time,
    }

    params["signature"] = _sapi_sign(
        params,
        BINANCE_API_SECRET,
    )

    try:

        response = requests.get(
            PAY_TRANSACTIONS_URL,
            headers={
                "X-MBX-APIKEY":
                    BINANCE_API_KEY
            },
            params=params,
            timeout=15,
        )

        if response.status_code != 200:

            if _is_binance_location_restriction(response.text):
                _mark_binance_access_restricted(
                    response.text,
                    "Binance Pay transaction-history request",
                )

            logger.warning(
                "Binance Pay transaction-history request failed: HTTP %s",
                response.status_code,
            )

            return []

        payload = response.json()

        rows = payload.get(
            "data",
            [],
        )

        return (
            rows
            if isinstance(rows, list)
            else []
        )

    except Exception:

        logger.exception(
            "Binance Pay request failed"
        )

        return []


def _match_pay_transaction(
        order_id: str,
        rows: list[dict],
):
    for row in rows:

        matched = any(
            str(
                row.get(field)
            )
            == str(order_id)
            for field in PAY_TRANSACTION_MATCH_FIELDS
            if row.get(field)
            is not None
        )

        if not matched:
            continue

        try:

            amount = Decimal(
                str(
                    row.get(
                        "amount",
                        "0",
                    )
                )
            )

            if amount > 0:
                return row

        except Exception:
            continue

    return None


async def verify_binance_pay_order(
        order_id: str,
):
    rows = await asyncio.to_thread(
        _query_pay_trade_history_sync,
        BINANCE_PAY_LOOKBACK_DAYS,
    )

    match = _match_pay_transaction(
        order_id,
        rows,
    )

    if not match:
        return None

    currency = str(
        match.get(
            "currency",
            "",
        )
    ).upper()

    accepted = {
        str(x).upper()
        for x in BINANCE_PAY_ACCEPTED_CURRENCIES
    }

    if currency not in accepted:
        return False

    return {
        "sender": "BINANCE_PAY",
        "receiver": "",
        "amount": Decimal(
            str(
                match.get(
                    "amount",
                    "0",
                )
            )
        ).copy_abs(),
        "currency": currency,
        "order_id": order_id,
    }


# ================================================================
# INR / UPI CONVERSION (Render Hosted Service Integration)
# ================================================================

_usdt_inr_rate_cache = {
    "rate": None,
    "last_updated": None,
    "ttl_seconds": 180,  # Refresh cache every 3 minutes
}

_current_api_key_index = 0


def _get_usdt_inr_rate() -> Decimal:
    """
    Fetches the live exchange rate from samarthpricefetch.onrender.com.
    Falls back sequentially to open exchange APIs and config defaults.
    """
    global _current_api_key_index
    now = time_module.time()
    cache = _usdt_inr_rate_cache

    # 1. Return cached rate if fresh
    if (
            cache["rate"] is not None
            and cache["last_updated"] is not None
            and (now - cache["last_updated"]) < cache["ttl_seconds"]
    ):
        return cache["rate"]

    # 2. Try primary live fetcher service hosted on Render
    render_service_url = getattr(
        config,
        "RENDER_RATE_FETCHER_URL",
        "https://samarthpricefetch.onrender.com/"
    )

    try:
        response = requests.get(
            render_service_url,
            headers={"Accept": "text/html,application/json"},
            timeout=8
        )
        if response.status_code == 200:
            text = response.text

            # Try parsing raw JSON response
            try:
                json_data = json.loads(text)
                if "rate" in json_data:
                    rate_val = float(json_data["rate"])
                    if rate_val > 0:
                        rate = Decimal(str(rate_val))
                        cache["rate"] = rate
                        cache["last_updated"] = now
                        logger.info("Fetched USD/INR rate from Render service JSON: %s", rate)
                        return rate
            except (json.JSONDecodeError, TypeError, ValueError):
                pass

            # Try parsing HTML output from rendered webpage
            match = re.search(r'₹\s*([\d\.]+)', text)
            if not match:
                match = re.search(r'([\d\.]+)\s*INR', text, re.IGNORECASE)
            if match:
                rate_val = float(match.group(1))
                if 70.0 < rate_val < 150.0:
                    rate = Decimal(str(rate_val))
                    cache["rate"] = rate
                    cache["last_updated"] = now
                    logger.info("Fetched USD/INR rate from Render service HTML: %s", rate)
                    return rate
    except Exception as exc:
        logger.warning("Render rate fetcher service unavailable: %s", exc)

    # 3. Try CurrencyAPI keys if configured
    api_keys = getattr(config, "CURRENCY_API_KEYS", [])
    base_url = getattr(config, "CURRENCY_API_URL", "https://api.currencyapi.com/v3/latest")

    if api_keys:
        num_keys = len(api_keys)
        for _ in range(num_keys):
            current_key = api_keys[_current_api_key_index]
            try:
                response = requests.get(
                    base_url,
                    params={
                        "apikey": current_key,
                        "base_currency": "USD",
                        "currencies": "INR"
                    },
                    timeout=getattr(config, "CURRENCY_REQUEST_TIMEOUT", 10)
                )

                if response.status_code == 200:
                    data = response.json()
                    inr_data = data.get("data", {}).get("INR", {})
                    rate_val = inr_data.get("value")

                    if rate_val:
                        rate = Decimal(str(rate_val))
                        if rate > 0:
                            cache["rate"] = rate
                            cache["last_updated"] = now
                            return rate
                elif response.status_code in (401, 403, 429) and getattr(config, "CURRENCY_ROTATE_KEYS_ON_ERROR", True):
                    _current_api_key_index = (_current_api_key_index + 1) % num_keys
            except Exception:
                pass

            if getattr(config, "CURRENCY_ROTATE_KEYS_ON_ERROR", True):
                _current_api_key_index = (_current_api_key_index + 1) % num_keys

    # 4. Try Free Open Rates API (open.er-api.com)
    try:
        response = requests.get("https://open.er-api.com/v6/latest/USD", timeout=5)
        if response.status_code == 200:
            rate_val = response.json().get("rates", {}).get("INR")
            if rate_val:
                rate = Decimal(str(rate_val))
                if rate > 0:
                    cache["rate"] = rate
                    cache["last_updated"] = now
                    return rate
    except Exception:
        pass

    # 5. Try Frankfurter API
    try:
        response = requests.get("https://api.frankfurter.app/latest?from=USD&to=INR", timeout=5)
        if response.status_code == 200:
            rate_val = response.json().get("rates", {}).get("INR")
            if rate_val:
                rate = Decimal(str(rate_val))
                if rate > 0:
                    cache["rate"] = rate
                    cache["last_updated"] = now
                    return rate
    except Exception:
        pass

    # 6. Fallback to Binance ticker
    client = _get_binance_client()
    if client is not None:
        try:
            rate = Decimal(str(client.get_symbol_ticker(symbol="USDTINR")["price"]))
            if rate > 0:
                cache["rate"] = rate
                cache["last_updated"] = now
                return rate
        except Exception:
            pass

    # 7. Final static configuration fallback
    return Decimal(str(getattr(config, "UPI_USDT_INR_RATE", 95.85)))


def convert_inr_to_usdt(
        inr_amount: Decimal,
        rate: Optional[Decimal] = None,
) -> Decimal:
    """Convert INR using one verified rate snapshot and retain accounting precision."""
    rate = rate if rate is not None else _get_usdt_inr_rate()
    if rate <= 0:
        return Decimal("0")

    return (inr_amount / rate).quantize(
        Decimal("0.00000001"),
        rounding=ROUND_HALF_UP,
    )


# ================================================================
# CREDIT USER
# ================================================================

def credit_user(
        db,
        deposit: Deposit,
        credited_amount: Decimal,
        requested_amount: Optional[Decimal],
        inr_amount=None,
        usdt_inr_rate=None,
) -> bool:
    from services.payment_submissions import credit_verified
    return credit_verified(db, deposit, credited_amount, inr_amount, usdt_inr_rate)


# ================================================================
# ATTEMPTS
# ================================================================

def _record_pending_attempt(
        deposit_id: int,
) -> int:
    count = (
            _check_attempts.get(
                deposit_id,
                0,
            )
            + 1
    )

    _check_attempts[
        deposit_id
    ] = count

    return count


def _clear_pending_attempts(
        deposit_id: int,
):
    _check_attempts.pop(
        deposit_id,
        None,
    )


def _keep_pending_or_cancel(
        db,
        deposit: Deposit,
        result_info: dict | None = None,
) -> bool:
    """Count a pending verification and cancel it at the configured limit."""
    attempts = _record_pending_attempt(deposit.id)

    if attempts < MAX_CHECK_ATTEMPTS:
        logger.info(
            "[%s] Deposit %s still pending (attempt %s/%s)",
            deposit.network,
            deposit.id,
            attempts,
            MAX_CHECK_ATTEMPTS,
        )
        return True

    _finalize_failed(
        db,
        deposit,
        f"verification attempt limit reached ({attempts}/{MAX_CHECK_ATTEMPTS})",
    )
    logger.warning("Deposit %s cancelled after %s verification attempts", deposit.id, attempts)

    if result_info is not None:
        result_info.update({
            "reason": "attempt_limit",
            "deposit_id": deposit.id,
            "attempts": attempts,
            "telegram_id": deposit.telegram_id,
            "network": str(deposit.network or ""),
        })

    return False


async def _notify_attempt_limit(bot, result_info: dict) -> None:
    """Tell the customer that automatic checking stopped exactly once."""
    if not bot or result_info.get("reason") != "attempt_limit":
        return

    telegram_id = result_info.get("telegram_id")
    if not telegram_id:
        return

    try:
        await queue_message(bot, f"deposit-attempt-limit:{result_info.get('deposit_id')}",
            telegram_id,
            "⚠️ <b>Deposit Verification Cancelled</b>\n\n"
            f"We could not verify your payment after {result_info.get('attempts', MAX_CHECK_ATTEMPTS)} checks, "
            "so automatic checking has stopped.\n\n"
            "The admin has been notified. If you made the payment, contact support and include your "
            "transaction reference and deposit ID.",
            parse_mode="HTML",
        )
    except Exception:
        logger.exception("Failed to notify user %s about cancelled deposit", telegram_id)

    notification_channel = getattr(config, "NOTIFICATION_CHANNEL_ID", "")
    if notification_channel:
        try:
            await queue_message(bot, f"deposit-attempt-limit:{result_info.get('deposit_id')}",
                notification_channel,
                "⚠️ <b>Deposit Verification Cancelled</b>\n\n"
                f"🆔 Deposit: <code>#{result_info.get('deposit_id')}</code>\n"
                f"👤 User: <code>{telegram_id}</code>\n"
                f"📡 Network: <code>{result_info.get('network') or 'Unknown'}</code>\n"
                f"🔁 Checks used: <code>{result_info.get('attempts', MAX_CHECK_ATTEMPTS)}</code>",
                parse_mode="HTML",
            )
        except Exception:
            logger.exception("Failed to post cancelled deposit alert to %s", notification_channel)


# ================================================================
# FAILED DEPOSIT
# ================================================================

DELETE_FAILED_DEPOSITS = getattr(
    config,
    "DEPOSIT_DELETE_FAILED",
    False,
)


def _finalize_failed(
        db,
        deposit: Deposit,
        reason: str,
):
    deposit = db.query(Deposit).filter(Deposit.id == deposit.id).populate_existing().with_for_update().first()
    if not deposit or deposit.status in ('completed', 'failed'):
        db.rollback()
        return
    if DELETE_FAILED_DEPOSITS:
        db.delete(deposit)
    else:
        deposit.status = "failed"

    db.commit()

    logger.warning(
        "Deposit %s FAILED: %s",
        deposit.id,
        reason,
    )

    _clear_pending_attempts(
        deposit.id
    )


def _fail_deposit(
        db,
        deposit_id: int,
        reason: str,
):
    try:

        dep = db.get(
            Deposit,
            deposit_id,
        )

        if (
                dep
                and dep.status
                not in (
                "completed",
                "failed",
        )
        ):
            _finalize_failed(
                db,
                dep,
                reason,
            )

    except Exception:

        db.rollback()

    finally:

        _clear_pending_attempts(
            deposit_id
        )


# ================================================================
# VERIFY ONE DEPOSIT
# ================================================================

async def verify_deposit(
        deposit_or_id,
        upi_matches=None,
        result_info=None,
):
    db = SessionLocal()

    try:

        deposit_id = (
            deposit_or_id
            if isinstance(
                deposit_or_id,
                int,
            )
            else deposit_or_id.id
        )

        deposit = db.get(
            Deposit,
            deposit_id,
        )

        if deposit is None:
            _clear_pending_attempts(
                deposit_id
            )

            return False

        # --------------------------------------------------------
        # Already processed
        # --------------------------------------------------------

        if deposit.status in (
                "completed",
                "failed",
        ):
            _clear_pending_attempts(
                deposit.id
            )

            return (
                    deposit.status
                    == "completed"
            )

        # --------------------------------------------------------
        # Requested amount
        # --------------------------------------------------------

        try:

            requested_amount = Decimal(
                str(
                    deposit.amount
                )
            )

            if requested_amount <= 0:
                requested_amount = None

        except (
                InvalidOperation,
                TypeError,
        ):

            requested_amount = None

        network = str(
            deposit.network or ""
        ).upper()

        # ========================================================
        # UPI
        # ========================================================

        if network == UPI_NETWORK:

            if not valid_utr(
                    deposit.tx_hash or ""
            ):
                _fail_deposit(
                    db,
                    deposit.id,
                    "invalid UTR",
                )

                return False

            if upi_matches is not None:

                verification = _match_upi(
                    deposit,
                    upi_matches,
                )

            else:
                db.close()
                db = None

                verification = (
                    await verify_upi(
                        deposit
                    )
                )

                db = SessionLocal()
                deposit = db.get(Deposit, deposit_id)
                if deposit is None:
                    _clear_pending_attempts(deposit_id)
                    return False
                if deposit.status in ("completed", "failed"):
                    _clear_pending_attempts(deposit.id)
                    return deposit.status == "completed"

            if verification is None:
                return None if _keep_pending_or_cancel(db, deposit, result_info) else False

            if verification is False:
                _fail_deposit(
                    db,
                    deposit.id,
                    "UPI verification failed",
                )

                return False

            inr_amount = (
                verification[
                    "inr_amount"
                ]
            )

            rate = _get_usdt_inr_rate()

            received_amount = (
                convert_inr_to_usdt(
                    inr_amount,
                    rate,
                )
            )

            if requested_amount is not None and requested_amount > Decimal("0"):

                requested_usdt = (
                    convert_inr_to_usdt(
                        requested_amount,
                        rate,
                    )
                )

                if (
                        received_amount
                        < requested_usdt
                        - AMOUNT_TOLERANCE
                ):
                    _fail_deposit(
                        db,
                        deposit.id,
                        "UPI underpaid",
                    )

                    return False

            _clear_pending_attempts(
                deposit.id
            )

            return credit_user(
                db,
                deposit,
                received_amount,
                requested_amount,
                inr_amount,
                rate,
            )

        # ========================================================
        # BINANCE PAY
        # ========================================================

        if network == BINANCE_PAY_NETWORK:

            if not valid_order_id(
                    deposit.tx_hash or ""
            ):
                _fail_deposit(
                    db,
                    deposit.id,
                    "invalid Binance Pay order ID",
                )

                return False

            db.close()
            db = None

            verification = (
                await verify_binance_pay_order(
                    deposit.tx_hash
                )
            )

            db = SessionLocal()
            deposit = db.get(Deposit, deposit_id)
            if deposit is None:
                _clear_pending_attempts(deposit_id)
                return False
            if deposit.status in ("completed", "failed"):
                _clear_pending_attempts(deposit.id)
                return deposit.status == "completed"

            if verification is None:
                return None if _keep_pending_or_cancel(db, deposit, result_info) else False

            if verification is False:
                _fail_deposit(
                    db,
                    deposit.id,
                    "Binance Pay verification failed",
                )

                return False

            received_amount = (
                verification["amount"]
            )

            if requested_amount is not None and requested_amount > Decimal("0"):

                if (
                        received_amount
                        < requested_amount
                        - AMOUNT_TOLERANCE
                ):
                    _fail_deposit(
                        db,
                        deposit.id,
                        "Binance Pay underpaid",
                    )

                    return False

            _clear_pending_attempts(
                deposit.id
            )

            return credit_user(
                db,
                deposit,
                received_amount,
                requested_amount,
            )

        # ========================================================
        # CRYPTO
        # ========================================================

        if not valid_hash(
                deposit.tx_hash or ""
        ):
            _fail_deposit(
                db,
                deposit.id,
                "invalid transaction hash",
            )

            return False

        if network not in CHAINS:
            _fail_deposit(
                db,
                deposit.id,
                f"unknown network {network}",
            )

            return False

        chain = CHAINS[network]

        # Persist when this TXID was first accepted. This is the anchor for
        # the three-day Binance-history claim window, including after restart.
        claim_started_at = deposit.verification_started_at
        if claim_started_at is None:
            claim_started_at = datetime.utcnow()
            deposit.verification_started_at = claim_started_at
            db.commit()

        # --------------------------------------------------------
        # Binance signed deposit-history lookup only
        # --------------------------------------------------------

        db.close()
        db = None

        verification = (
            await verify_transaction(
                chain,
                deposit.tx_hash,
                requested_amount,
                claim_started_at,
            )
        )

        db = SessionLocal()
        deposit = db.get(Deposit, deposit_id)
        if deposit is None:
            _clear_pending_attempts(deposit_id)
            return False
        if deposit.status in ("completed", "failed"):
            _clear_pending_attempts(deposit.id)
            return deposit.status == "completed"

        # --------------------------------------------------------
        # Still pending
        # --------------------------------------------------------

        if verification is None:
            # Binance may index a deposit late, or its API may be temporarily
            # unavailable. Never expire/decline a crypto deposit just because
            # a retry counter ran out; keep it pending until Binance confirms
            # it or an administrator reviews it.
            if result_info is not None:
                result_info["reason"] = "pending"
            return None

        # --------------------------------------------------------
        # Permanently invalid
        # --------------------------------------------------------

        if verification is False:

            if result_info is not None:
                result_info[
                    "reason"
                ] = "verification_failed"

            _finalize_failed(
                db,
                deposit,
                "Binance deposit record rejected or did not match",
            )

            return False

        # --------------------------------------------------------
        # Final amount verification
        # --------------------------------------------------------

        received_amount = Decimal(
            str(
                verification[
                    "amount"
                ]
            )
        )

        if requested_amount is not None and requested_amount > Decimal("0"):

            if (
                    received_amount
                    < requested_amount
                    - AMOUNT_TOLERANCE
            ):
                _finalize_failed(
                    db,
                    deposit,
                    "underpaid",
                )

                return False

        # --------------------------------------------------------
        # Final duplicate protection
        # --------------------------------------------------------

        duplicate = (
            db.query(Deposit)
            .filter(
                Deposit.tx_hash
                == deposit.tx_hash,
                Deposit.status
                == "completed",
                Deposit.id
                != deposit.id,
            )
            .first()
        )

        if duplicate:
            _finalize_failed(
                db,
                deposit,
                "duplicate transaction",
            )

            return False

        # --------------------------------------------------------
        # CREDIT
        # --------------------------------------------------------

        _clear_pending_attempts(
            deposit.id
        )

        success = credit_user(
            db,
            deposit,
            received_amount,
            requested_amount,
        )

        if success:
            logger.info(
                "[%s] DEPOSIT VERIFIED + CREDITED | "
                "tx=%s | coin=%s | amount=%s | "
                "Binance status=%s",
                network,
                deposit.tx_hash[:18],
                verification["coin"],
                received_amount,
                verification["binance_status"],
            )

        return success

    except Exception:

        if db is not None:
            db.rollback()

        logger.exception(
            "Error verifying deposit"
        )

        return False

    finally:

        if db is not None:
            db.close()


# ================================================================
# CHECK PENDING DEPOSITS
# ================================================================

async def check_pending_deposits(bot=None):
    global _binance_pending_skip_logged

    db = SessionLocal()

    try:

        rows = (
            db.query(
                Deposit.id,
                Deposit.network,
            )
            .filter(
                Deposit.status
                == "pending"
            )
            .all()
        )

    finally:

        db.close()

    crypto_ids = []
    pay_ids = []
    upi_ids = []

    for deposit_id, network in rows:

        network = str(
            network or ""
        ).upper()

        if network in CHAINS:

            crypto_ids.append(
                deposit_id
            )

        elif network == BINANCE_PAY_NETWORK:

            pay_ids.append(
                deposit_id
            )

        elif network == UPI_NETWORK:

            upi_ids.append(
                deposit_id
            )

    logger.info(
        "Checking %s pending | crypto=%s | "
        "Binance Pay=%s | UPI=%s",
        len(rows),
        len(crypto_ids),
        len(pay_ids),
        len(upi_ids),
    )

    binance_credentials_missing = not BINANCE_API_KEY or not BINANCE_API_SECRET
    binance_unavailable = _binance_access_restricted or binance_credentials_missing
    if binance_unavailable and (crypto_ids or pay_ids) and not _binance_pending_skip_logged:
        logger.warning(
            "Skipping Binance-only verification for %s crypto and %s Pay deposits: "
            "Binance credentials/API access are unavailable. Deposits remain pending; "
            "no alternate chain RPC verification is configured.",
            len(crypto_ids),
            len(pay_ids),
        )

        _binance_pending_skip_logged = True

    # ------------------------------------------------------------
    # CRYPTO
    # ------------------------------------------------------------

    for deposit_id in ([] if binance_credentials_missing or _binance_access_restricted else crypto_ids):
        if _binance_access_restricted:
            break

        try:
            result_info: dict = {}
            await verify_deposit(
                deposit_id,
                result_info=result_info,
            )
            await _notify_attempt_limit(bot, result_info)

        except Exception:

            logger.exception(
                "Crypto deposit %s failed",
                deposit_id,
            )

    # ------------------------------------------------------------
    # BINANCE PAY
    # ------------------------------------------------------------

    for deposit_id in ([] if binance_credentials_missing or _binance_access_restricted else pay_ids):
        if _binance_access_restricted:
            break

        try:
            result_info: dict = {}
            await verify_deposit(
                deposit_id,
                result_info=result_info,
            )
            await _notify_attempt_limit(bot, result_info)

        except Exception:

            logger.exception(
                "Binance Pay %s failed",
                deposit_id,
            )

    # ------------------------------------------------------------
    # UPI
    # ------------------------------------------------------------

    if upi_ids:

        try:

            upi_matches = await asyncio.to_thread(
                _fetch_famapp_matches
            )

        except Exception:

            upi_matches = {}

        for deposit_id in upi_ids:

            try:
                result_info: dict = {}
                await verify_deposit(
                    deposit_id,
                    upi_matches=upi_matches,
                    result_info=result_info,
                )
                await _notify_attempt_limit(bot, result_info)

            except Exception:

                logger.exception(
                    "UPI deposit %s failed",
                    deposit_id,
                )


# ================================================================
# BACKGROUND LOOP
# ================================================================

async def deposit_checker_loop(bot=None):
    logger.info("=" * 65)

    logger.info(
        "DEPOSIT CHECKER STARTED"
    )

    logger.info(
        "Crypto verification: Binance signed deposit-history only (USDT)"
    )

    logger.info(
        "Check interval: %ss",
        CHECK_INTERVAL,
    )

    logger.info("=" * 65)

    while True:

        try:

            await check_pending_deposits(bot)

        except Exception:

            logger.exception(
                "Deposit checker crashed"
            )

        await asyncio.sleep(
            CHECK_INTERVAL
        )


def start_checker():
    return asyncio.create_task(
        deposit_checker_loop()
    )
