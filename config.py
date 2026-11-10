# config.py

import hashlib
import os
from dotenv import load_dotenv

# Set BOT_ENV_FILE=.env.staging to load an isolated local environment.
load_dotenv(dotenv_path=os.getenv("BOT_ENV_FILE", ".env"))

# Shared password used by Telegram and web maintenance controls.
# Configure MAINTENANCE_PASSWORD in the environment on both services.
_legacy_maintenance_password_hash = "7aa5d3f5bbd51bdb5b8796decd7535ecf2a0b4944885c7dd81a543bcd7885585"
MAINTENANCE_PASSWORD = os.getenv("MAINTENANCE_PASSWORD", "")
MAINTENANCE_PASSWORD_HASH = os.getenv("MAINTENANCE_PASSWORD_HASH", "").strip() or (
    hashlib.sha256(MAINTENANCE_PASSWORD.encode("utf-8")).hexdigest()
    if MAINTENANCE_PASSWORD
    else _legacy_maintenance_password_hash
)


# ==========================================================
# TELEGRAM
# ==========================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "")

# Manual reseller deliveries are sent by the Cloudflare delivery-bot webhook.
# The delivery bot token is configured only as a Cloudflare Worker secret.
DELIVERY_BRIDGE_URL = os.getenv("DELIVERY_BRIDGE_URL", "").strip().rstrip("/")
DELIVERY_BRIDGE_SECRET = os.getenv("DELIVERY_BRIDGE_SECRET", "").strip()

# Public username used to build manager login links. The bot token is stored
# only as the ADMIN_VERIFY_BOT_TOKEN secret on the Cloudflare API Worker.
ADMIN_VERIFY_BOT_USERNAME = os.getenv("ADMIN_VERIFY_BOT_USERNAME", "").lstrip("@").strip()

ADMIN_IDS = [
    1316577060,
    7943742895,
    5327735171
]

CHANNEL_LINK = "https://t.me/RainOrdersGroup"
GROUP_LINK = "https://t.me/RainStockGroup"
ADDITIONAL_CHANNEL_LINK = "https://t.me/rainworlddd"
ADDITIONAL_CHANNEL_ID = -1003726039597
TOS_LINK = "https://your-site.com/tos"

GROUP_ID = -1003541834339
GROUP_NOTIFICATIONS = True

# Notification channels. A bot must be an administrator with permission to
# post messages in each channel. Public @usernames work without a numeric ID.
ORDER_NOTIFICATION_CHANNEL_ID = os.getenv(
    "ORDER_NOTIFICATION_CHANNEL_ID",
    "@RainOrdersGroup",
)
STOCK_NOTIFICATION_CHANNEL_ID = os.getenv(
    "STOCK_NOTIFICATION_CHANNEL_ID",
    "@RainStockGroup",
)
# Dedicated admin order-alert group.  This can be a public @username or a
# private group ID such as -1001234567890.  New orders are read from the
# database and sent here once; stock updates are never sent here.
# NOTIFICATION_CHANNEL_ID remains the fallback so existing deployments do not
# need an immediate environment-variable change.
ADMIN_ORDER_NOTIFICATION_CHANNEL_ID = os.getenv(
    "ADMIN_ORDER_NOTIFICATION_CHANNEL_ID",
    os.getenv("NOTIFICATION_CHANNEL_ID", "@RainNotify"),
)

# Cancelled-deposit alerts use this same admin group unless disabled.
NOTIFICATION_CHANNEL_ID = os.getenv("NOTIFICATION_CHANNEL_ID", "@RainNotify")
ADMIN_ORDER_NOTIFICATION_POLL_SECONDS = max(
    3, int(os.getenv("ADMIN_ORDER_NOTIFICATION_POLL_SECONDS", "5"))
)


# ==========================================================
# VERCEL / WEBHOOK
# ==========================================================

WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").rstrip("/")

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")

CRON_SECRET = os.getenv("CRON_SECRET", "")

WEBHOOK_PATH = "api/telegram"

DEPOSIT_CHECK_PATH = "api/deposit_check"


# ==========================================================
# EXTERNAL RESELLER API
# ==========================================================
# A long random secret used only when hashing customer API keys. It must be
# different from BOT_TOKEN and must never be committed to GitHub.
API_KEY_PEPPER = os.getenv("API_KEY_PEPPER", "")

# Encrypts the active reseller key so its owner can view it again in Telegram.
# This must be a stable Fernet key and must never be committed or changed.
API_KEY_ENCRYPTION_KEY = os.getenv("API_KEY_ENCRYPTION_KEY", "")

# Stable key for resumable Telegram drafts/screens. Set this separately from
# BOT_TOKEN so rotating Telegram credentials does not invalidate saved drafts.
SESSION_RESUME_ENCRYPTION_KEY = os.getenv("SESSION_RESUME_ENCRYPTION_KEY", "")

# The public Wasmer gateway shown to users in the bot. Example:
# https://my-reseller-api.wasmer.app.  Do not put the Vercel/Render bot URL
# here, because it is an internal bridge only.
API_BASE_URL = os.getenv("API_BASE_URL", "").rstrip("/")
MANAGER_WEB_URL = os.getenv(
    "MANAGER_WEB_URL",
    "https://raindeals-manager.samarthp2727-rd.workers.dev",
).rstrip("/")
API_RATE_LIMIT_PER_SECOND = max(1, int(os.getenv("API_RATE_LIMIT_PER_SECOND", "3")))
# Limits are applied per active customer API key. They can be tightened at
# deployment time without a code change if a customer abuses their key.
API_RATE_LIMIT_PER_MINUTE = max(1, int(os.getenv("API_RATE_LIMIT_PER_MINUTE", "120")))
API_ORDER_LIMIT_PER_MINUTE = max(1, int(os.getenv("API_ORDER_LIMIT_PER_MINUTE", "10")))
API_MAX_CONCURRENT_ORDERS = max(1, int(os.getenv("API_MAX_CONCURRENT_ORDERS", "1")))
API_INVALID_AUTH_LIMIT_PER_MINUTE = max(1, int(os.getenv("API_INVALID_AUTH_LIMIT_PER_MINUTE", "20")))
API_MAX_REQUEST_BYTES = max(1024, int(os.getenv("API_MAX_REQUEST_BYTES", "16384")))
# Only trust forwarded client IP headers when the immediate peer belongs to
# one of these explicitly configured reverse-proxy networks.
API_TRUSTED_PROXY_CIDRS = tuple(
    value.strip() for value in os.getenv("API_TRUSTED_PROXY_CIDRS", "").split(",")
    if value.strip()
)
# A reservation older than this is never replayed automatically if no local
# order exists; supplier-side success may be ambiguous and requires review.
API_ORDER_RECONCILE_AFTER_SECONDS = max(
    300, int(os.getenv("API_ORDER_RECONCILE_AFTER_SECONDS", "900"))
)

# The gateway on Wasmer sends this value to the bot for every internal API
# request.  It must be a long random value, identical in Vercel/Render and
# Wasmer secrets, and must never be returned to an API client.
INTERNAL_API_SECRET = os.getenv("INTERNAL_API_SECRET", "")


# ==========================================================
# REDIS
# ==========================================================

REDIS_URL = os.getenv("REDIS_URL", "")


# ==========================================================
# STOCK ALERTS
# ==========================================================

STOCK_GROUP_ID = os.getenv(
    "STOCK_GROUP_ID",
    STOCK_NOTIFICATION_CHANNEL_ID,
)

STOCK_NOTIFICATIONS = True

STOCK_ALERT_THRESHOLDS = [
    40,
    70,
    90,
]


# ==========================================================
# MYSQL / TiDB CLOUD
# ==========================================================

MYSQL_HOST = os.getenv("MYSQL_HOST", "")

MYSQL_PORT = int(
    os.getenv("MYSQL_PORT", "4000")
)

MYSQL_USER = os.getenv("MYSQL_USER", "")

MYSQL_PASSWORD = os.getenv("MYSQL_PASSWORD", "")

MYSQL_DB = os.getenv(
    "MYSQL_DB",
    "telegram_shop",
)

MYSQL_SSL_CA = os.getenv(
    "MYSQL_SSL_CA",
    "ca.pem",
)


# ==========================================================
# DEPOSIT NETWORKS
# ==========================================================

BEP20_ADDRESS = (
    "0x573ff2eec518828332efce4267e585932c21f867"
)

POLYGON_ADDRESS = (
    "0x573ff2eec518828332efce4267e585932c21f867"
)


# Crypto deposits are credited as USDT. Do not treat other assets as USDT.
BINANCE_DEPOSIT_COINS = [
    "USDT",
]

# A submitted TXID may refer to a Binance deposit made during the prior 72 hours.
BINANCE_DEPOSIT_CLAIM_WINDOW_HOURS = 72


# ==========================================================
# RPC NETWORKS
# ==========================================================

BSC_RPC_URL = os.getenv(
    "BSC_RPC_URL",
    "https://bsc-dataseed.binance.org",
)

POLYGON_RPC_URL = os.getenv(
    "POLYGON_RPC_URL",
    "https://polygon-rpc.com",
)


RPC_REQUEST_TIMEOUT = int(
    os.getenv(
        "RPC_REQUEST_TIMEOUT",
        "15",
    )
)

RPC_MAX_RETRIES = int(
    os.getenv(
        "RPC_MAX_RETRIES",
        "3",
    )
)

RPC_RETRY_DELAY = float(
    os.getenv(
        "RPC_RETRY_DELAY",
        "1.0",
    )
)


# ==========================================================
# RPC CHAIN IDs
# ==========================================================

BSC_CHAIN_ID = 56

POLYGON_CHAIN_ID = 137


# ==========================================================
# RPC BLOCK SETTINGS
# ==========================================================

BSC_BLOCK_TIME_SECONDS = 3

POLYGON_BLOCK_TIME_SECONDS = 2


# ==========================================================
# BEP20 TOKEN CONTRACTS
# ==========================================================

USDT_CONTRACT_BEP20 = (
    "0x55d398326f99059ff775485246999027b3197955"
)

BUSD_CONTRACT_BEP20 = (
    "0xe9e7cea3dedca5984780bafc599bd69add087d56"
)

USDC_CONTRACT_BEP20 = (
    "0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d"
)


# ==========================================================
# POLYGON TOKEN CONTRACTS
# ==========================================================

USDT_CONTRACT_POLYGON = (
    "0xc2132d05d31c914a87c6611c10748aeb04b58e8f"
)

USDC_CONTRACT_POLYGON = (
    "0x3c499c542cef5e3811e1192ce70d8cc03d5c3359"
)


# ==========================================================
# TOKEN DECIMALS
# ==========================================================

TOKEN_DECIMALS = {
    "USDT": 6,
    "USDC": 6,
    "BUSD": 18,
}


# ==========================================================
# NETWORK TOKEN CONTRACT MAP
# ==========================================================

TOKEN_CONTRACTS = {
    "BEP20": {
        "USDT": USDT_CONTRACT_BEP20,
        "BUSD": BUSD_CONTRACT_BEP20,
        "USDC": USDC_CONTRACT_BEP20,
    },

    "POLYGON": {
        "USDT": USDT_CONTRACT_POLYGON,
        "USDC": USDC_CONTRACT_POLYGON,
    },
}


# ==========================================================
# NETWORK RPC MAP
# ==========================================================

NETWORK_RPC_URLS = {
    "BEP20": BSC_RPC_URL,
    "POLYGON": POLYGON_RPC_URL,
}


# ==========================================================
# NETWORK CHAIN MAP
# ==========================================================

NETWORK_CHAIN_IDS = {
    "BEP20": BSC_CHAIN_ID,
    "POLYGON": POLYGON_CHAIN_ID,
}


# ==========================================================
# DEPOSIT ADDRESS MAP
# ==========================================================

NETWORK_DEPOSIT_ADDRESSES = {
    "BEP20": BEP20_ADDRESS,
    "POLYGON": POLYGON_ADDRESS,
}


# ==========================================================
# TOKEN CONTRACT REQUIREMENT
# ==========================================================

REQUIRE_CONTRACT_MATCH = True


# ==========================================================
# CONFIRMATIONS
# ==========================================================

MIN_BLOCK_CONFIRMATIONS = 5

BINANCE_CONFIRMATION_MAP = {
    "BEP20": 5,
    "POLYGON": 5,
}

FINALITY_CONFIRMATIONS = {
    "BEP20": 15,
    "POLYGON": 60,
}


# ==========================================================
# BINANCE NETWORK MAP
# ==========================================================

BINANCE_NETWORK_MAP = {
    "BEP20": "BSC",
    "POLYGON": "MATIC",
}


# ==========================================================
# BINANCE API
# ==========================================================

BINANCE_API_KEY = os.getenv(
    "BINANCE_API_KEY",
    "",
)

BINANCE_API_SECRET = os.getenv(
    "BINANCE_API_SECRET",
    "",
)

BINANCE_PAY_ID = "45829163"

BINANCE_PAY_LOOKBACK_DAYS = 7

# ==========================================================
# BINANCE API USAGE CONTROL
# ==========================================================

BINANCE_DISCOVERY_ENABLED = True

BINANCE_USE_FOR_CONFIRMATIONS = False

BINANCE_USE_FOR_RPC_VERIFICATION = False

BINANCE_REQUEST_DELAY_SECONDS = float(
    os.getenv(
        "BINANCE_REQUEST_DELAY_SECONDS",
        "1.0",
    )
)

BINANCE_REQUEST_TIMEOUT = int(
    os.getenv(
        "BINANCE_REQUEST_TIMEOUT",
        "15",
    )
)

BINANCE_MAX_RETRIES = int(
    os.getenv(
        "BINANCE_MAX_RETRIES",
        "2",
    )
)


# ==========================================================
# DEPOSIT CHECKER STRATEGY
# ==========================================================

DEPOSIT_DISCOVERY_PROVIDER = "BINANCE"

DEPOSIT_VERIFICATION_PROVIDER = "RPC"

DEPOSIT_CONFIRMATION_PROVIDER = "RPC"


# ==========================================================
# DEPOSIT — UPI
# ==========================================================

UPI_ID = "luquman@fam"
UPI_QR_FILE = os.getenv("UPI_QR_FILE", "").strip()
BEP20_QR_FILE = os.getenv("BEP20_QR_FILE", "").strip()
POLYGON_QR_FILE = os.getenv("POLYGON_QR_FILE", "").strip()
DELIVERY_BOT_USERNAME = os.getenv("DELIVERY_BOT_USERNAME", "").strip().lstrip("@")

IMAP_HOST = "imap.gmail.com"

IMAP_EMAIL = os.getenv(
    "IMAP_EMAIL",
    "",
)

IMAP_APP_PASSWORD = os.getenv(
    "IMAP_APP_PASSWORD",
    "",
)

FAMAPP_SENDER_EMAIL = "no-reply@famapp.in"

IMAP_LOOKBACK_DAYS = 1


# ==========================================================
# DEPOSIT — AMOUNT VERIFICATION
# ==========================================================

DEPOSIT_AMOUNT_TOLERANCE = "0.000001"

DEPOSIT_ALLOW_OVERPAY = True

# A pending deposit is checked at most this many times, including the first
# check after the customer submits its reference. It is then cancelled.
DEPOSIT_MAX_CHECK_ATTEMPTS = max(1, int(os.getenv("DEPOSIT_MAX_CHECK_ATTEMPTS", "10")))

DEPOSIT_DELETE_FAILED = False


# ==========================================================
# DEPOSIT TIME WINDOW
# ==========================================================

DEPOSIT_BEFORE_TX_GRACE_DAYS = 5

DEPOSIT_MAX_AGE_DAYS = 7


# ==========================================================
# MINIMUM DEPOSIT
# ==========================================================

MIN_DEPOSIT_USD = 0.01


# ==========================================================
# BLOCKCHAIN VERIFICATION
# ==========================================================

ENABLE_CONTRACT_CHECK = True
ENABLE_BLOCKLIST_CHECK = True
ENABLE_SENDER_BLOCKLIST = True
ENABLE_CONFIRMATION_CHECK = True
ENABLE_FINALITY_CHECK = False
ENABLE_TIMESTAMP_CHECK = True
ENABLE_VALUE_CHECK = True
ENABLE_DUST_CHECK = True


# ==========================================================
# TRANSACTION VERIFICATION
# ==========================================================

VERIFY_TRANSACTION_RECEIPT = True
VERIFY_TRANSACTION_SUCCESS = True
VERIFY_RECIPIENT_ADDRESS = True
VERIFY_TOKEN_TRANSFER_EVENT = True
VERIFY_TOKEN_CONTRACT = True
VERIFY_TRANSACTION_BLOCK = True


# ==========================================================
# BLOCK CONFIRMATION METHOD
# ==========================================================

CONFIRMATIONS_FROM_RPC = True

CONFIRMATION_INCLUDE_TX_BLOCK = True


# ==========================================================
# RPC CONFIRMATION POLLING
# ==========================================================

RPC_CONFIRMATION_POLL_INTERVAL = int(
    os.getenv(
        "RPC_CONFIRMATION_POLL_INTERVAL",
        "5",
    )
)

RPC_CONFIRMATION_MAX_ATTEMPTS = int(
    os.getenv(
        "RPC_CONFIRMATION_MAX_ATTEMPTS",
        "60",
    )
)


# ==========================================================
# BLOCKCHAIN SAFETY
# ==========================================================

REJECT_REVERTED_TRANSACTIONS = True
REJECT_ZERO_VALUE_TRANSFERS = True
REJECT_WRONG_RECIPIENT = True
REJECT_WRONG_CONTRACT = True
REJECT_WRONG_NETWORK = True
REJECT_OLD_TRANSACTIONS = True


# ==========================================================
# BLOCKLISTS
# ==========================================================

KNOWN_FAKE_CONTRACTS = []

KNOWN_SCAM_SENDERS = []


# ==========================================================
# EXPLORER CHECK
# ==========================================================

ENABLE_EXPLORER_CHECK = True

BSCSCAN_API_KEY = os.getenv(
    "BSCSCAN_API_KEY",
    "",
)

POLYGONSCAN_API_KEY = os.getenv(
    "POLYGONSCAN_API_KEY",
    "",
)

ETHERSCAN_V2_API_KEY = os.getenv(
    "ETHERSCAN_V2_API_KEY",
    "",
)


# ==========================================================
# EXPLORER SETTINGS
# ==========================================================

BSCSCAN_BASE_URL = (
    "https://api.bscscan.com/api"
)

POLYGONSCAN_BASE_URL = (
    "https://api.polygonscan.com/api"
)

ETHERSCAN_V2_BASE_URL = (
    "https://api.etherscan.io/v2/api"
)


# ==========================================================
# EXPLORER API USAGE
# ==========================================================

EXPLORER_FALLBACK_ONLY = True

EXPLORER_REQUEST_TIMEOUT = int(
    os.getenv(
        "EXPLORER_REQUEST_TIMEOUT",
        "10",
    )
)


# ==========================================================
# CURRENCY CONVERSION — USD / USDT → INR
# ==========================================================

CURRENCY_API_URL = (
    "https://api.currencyapi.com/v3/latest"
)


# ==========================================================
# CURRENCY API KEYS
# ==========================================================

CURRENCY_API_KEY_1 = os.getenv(
    "CURRENCY_API_KEY_1",
    "",
)

CURRENCY_API_KEY_2 = os.getenv(
    "CURRENCY_API_KEY_2",
    "",
)

CURRENCY_API_KEY_3 = os.getenv(
    "CURRENCY_API_KEY_3",
    "",
)


# ==========================================================
# AVAILABLE CURRENCY API KEYS
# ==========================================================

CURRENCY_API_KEYS = [
    key
    for key in (
        CURRENCY_API_KEY_1,
        CURRENCY_API_KEY_2,
        CURRENCY_API_KEY_3,
    )
    if key
]


# ==========================================================
# CURRENCY RATE CACHE
# ==========================================================

CURRENCY_RATE_CACHE_MINUTES = 20


# ==========================================================
# CURRENCY SETTINGS
# ==========================================================

CURRENCY_BASE_CURRENCY = "USD"

CURRENCY_TARGET_CURRENCY = "INR"

CURRENCY_REQUEST_TIMEOUT = 10


# ==========================================================
# FALLBACK CURRENCY RATE
# ==========================================================

UPI_USDT_INR_RATE = 95.0


# ==========================================================
# CURRENCY API BEHAVIOR
# ==========================================================

CURRENCY_ROTATE_KEYS_ON_ERROR = True

CURRENCY_USE_CACHED_RATE_ON_ERROR = True

CURRENCY_USE_FALLBACK_RATE_ON_ERROR = True


# ==========================================================
# RESELLER API
# ==========================================================

RESELLER_API_URL = os.getenv(
    "RESELLER_API_URL",
    "https://arrsnetworkzone.in",
).rstrip("/")

RESELLER_BASE_URL = os.getenv(
    "RESELLER_BASE_URL",
    "https://arrsnetworkzone.in",
).rstrip("/")


RESELLER_API_KEY = os.getenv(
    "RESELLER_API_KEY",
    "",
)

RESELLER_API_SECRET = os.getenv(
    "RESELLER_API_SECRET",
    "",
)

UPSTREAM_CALLBACK_URL = os.getenv("UPSTREAM_CALLBACK_URL", "").strip()


RESELLER_API_TIMEOUT = int(
    os.getenv(
        "RESELLER_API_TIMEOUT",
        "15",
    )
)


# ==========================================================
# EXTRA SYSTEM PADDING & UTILITY CONFIGURATION BLOCK
# ==========================================================

ENABLE_EXTENDED_LOGGING = True
SYSTEM_MAINTENANCE_MODE = False
DEFAULT_LOCALE = "en_IN"
SUPPORTED_LOCALES = ["en_IN", "en_US", "hi_IN"]
MAX_CONCURRENT_TASKS = 100
SESSION_TIMEOUT_SECONDS = 3600
DATABASE_POOL_SIZE = int(os.getenv("DATABASE_POOL_SIZE", "10"))
DATABASE_MAX_OVERFLOW = int(os.getenv("DATABASE_MAX_OVERFLOW", "20"))
DATABASE_POOL_RECYCLE = int(os.getenv("DATABASE_POOL_RECYCLE", "1800"))
DATABASE_POOL_TIMEOUT = int(os.getenv("DATABASE_POOL_TIMEOUT", "10"))
# Short-lived in-process caches prevent a remote API/database round trip for
# every Telegram update. Set either value to 0 to turn its cache off.
MEMBERSHIP_CACHE_TTL = int(os.getenv("MEMBERSHIP_CACHE_TTL", "300"))
BANNED_USER_CACHE_TTL = int(os.getenv("BANNED_USER_CACHE_TTL", "30"))
# Telegram can take a moment to report a newly joined member. These retries
# are used only when a fresh membership check is required.
MEMBERSHIP_VERIFY_ATTEMPTS = max(1, int(os.getenv("MEMBERSHIP_VERIFY_ATTEMPTS", "3")))
MEMBERSHIP_RETRY_DELAY_SECONDS = max(0.0, min(5.0, float(os.getenv("MEMBERSHIP_RETRY_DELAY_SECONDS", "1"))))
ENABLE_PERFORMANCE_METRICS = True
METRICS_COLLECTION_INTERVAL = 60
CACHE_BACKEND = "memory"
CACHE_DEFAULT_TTL = 300
RATE_LIMIT_ENABLED = True
RATE_LIMIT_DEFAULT_REQUESTS = 60
RATE_LIMIT_DEFAULT_WINDOW = 60
FEATURE_FLAG_NEW_UI = True
FEATURE_FLAG_ADVANCED_ANALYTICS = False
FEATURE_FLAG_EXPERIMENTAL_ROUTING = False
SECURITY_ENFORCE_HTTPS = True
SECURITY_SESSION_COOKIE_SECURE = True
SECURITY_SESSION_COOKIE_HTTPONLY = True
SECURITY_SESSION_COOKIE_SAMESITE = "Lax"
LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
LOG_LEVEL = "INFO"
TELEGRAM_WEBHOOK_DROP_PENDING_UPDATES = True
TELEGRAM_POLLING_TIMEOUT = 30
TELEGRAM_ALLOWED_UPDATES = ["message", "callback_query", "inline_query"]
AUTO_BACKUP_ENABLED = False
AUTO_BACKUP_SCHEDULE_HOURS = 24
NOTIFICATION_CHANNEL_BACKUP = ""
MAINTENANCE_MESSAGE = "System is currently undergoing scheduled maintenance. Please try again later."
ERROR_RESPONSE_TEMPLATE = "An unexpected error occurred. Please contact support or try again shortly."
SUCCESS_RESPONSE_TEMPLATE = "Operation completed successfully."
CUSTOM_METADATA_HEADER = "X-Service-Region"
CUSTOM_METADATA_VALUE = "ap-south-1"
API_VERSION = "v2.5.0"
COMPATIBILITY_MODE = True
ENVIRONMENT = os.getenv("ENVIRONMENT", "production")
DEBUG_MODE = os.getenv("DEBUG_MODE", "false").lower() == "true"
APP_TITLE = "Telegram Shop & Deposit Verification Bot"
APP_DESCRIPTION = "Automated crypto and UPI deposit verification backend with live currency conversion."
CONTACT_EMAIL = "support@arrsnetworkzone.in"
LICENSE_TYPE = "Proprietary"
ORGANIZATION_NAME = "Rain Enterprise"
TIMEZONE = "Asia/Kolkata"
INTERNAL_SERVICE_TOKEN = os.getenv("INTERNAL_SERVICE_TOKEN", "")
EXTERNAL_WEBHOOK_TIMEOUT = 10
MAX_RETRY_BACKOFF_FACTOR = 2.0
CIRCUIT_BREAKER_FAILURE_THRESHOLD = 5
CIRCUIT_BREAKER_RECOVERY_TIMEOUT = 30
METRICS_ENDPOINT_ENABLED = True
HEALTH_CHECK_ENDPOINT_ENABLED = True
VERSION_HEADER_NAME = "X-App-Version"
TRACE_ID_HEADER_NAME = "X-Trace-ID"
REQUEST_ID_HEADER_NAME = "X-Request-ID"
RATE_LIMIT_STORAGE_URL = os.getenv("REDIS_URL", "memory://")
SESSION_SECRET_KEY = os.getenv("SESSION_SECRET_KEY", "fallback-secret-key-change-me")
JWT_ALGORITHM = "HS256"
JWT_ACCESS_TOKEN_EXPIRE_MINUTES = 30
JWT_REFRESH_TOKEN_EXPIRE_DAYS = 7
PASSWORD_HASHING_ROUNDS = 12
ENCRYPTION_KEY_VERSION = 1
FEATURE_FLAG_MULTI_CURRENCY = True
FEATURE_FLAG_AUTO_REFUND = False
FEATURE_FLAG_WEBHOOK_SIGNATURE_VERIFICATION = True
ALLOWED_HOSTS = ["*"]
CORS_ORIGINS = ["*"]
CORS_CREDENTIALS = True
CORS_METHODS = ["GET", "POST", "PUT", "DELETE", "OPTIONS"]
CORS_HEADERS = ["*"]
WEBSOCKET_PING_INTERVAL = 20
WEBSOCKET_PING_TIMEOUT = 10
BACKGROUND_WORKER_CONCURRENCY = 4
BACKGROUND_WORKER_QUEUES = ["default", "deposits", "notifications"]
TASK_RETRY_MAX_ATTEMPTS = 3
TASK_RETRY_DELAY_SECONDS = 5
EMAIL_BACKEND = "smtp"
EMAIL_HOST = os.getenv("EMAIL_HOST", "smtp.gmail.com")
EMAIL_PORT = int(os.getenv("EMAIL_PORT", "587"))
EMAIL_USE_TLS = True
EMAIL_HOST_USER = os.getenv("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = os.getenv("EMAIL_HOST_PASSWORD", "")
DEFAULT_FROM_EMAIL = os.getenv("DEFAULT_FROM_EMAIL", "noreply@raindeals.com")
SMS_GATEWAY_PROVIDER = "none"
SMS_GATEWAY_API_KEY = os.getenv("SMS_GATEWAY_API_KEY", "")
PUSH_NOTIFICATION_PROVIDER = "none"
PUSH_NOTIFICATION_API_KEY = os.getenv("PUSH_NOTIFICATION_API_KEY", "")
ANALYTICS_TRACKING_ID = os.getenv("ANALYTICS_TRACKING_ID", "")
ERROR_TRACKING_DSN = os.getenv("ERROR_TRACKING_DSN", "")
PERFORMANCE_MONITORING_SAMPLE_RATE = 0.1
AUDIT_LOG_ENABLED = True
AUDIT_LOG_RETENTION_DAYS = 90
COMPLIANCE_MODE_GDPR = False
COMPLIANCE_MODE_CCPA = False
DATA_MASKING_ENABLED = True
IP_WHITELIST_ENABLED = False
IP_WHITELIST_CIDRS = []
IP_BLACKLIST_CIDRS = []
GEO_IP_BLOCKING_ENABLED = False
BLOCKED_COUNTRIES = []
SSL_VERIFICATION_ENABLED = True
CERTIFICATE_PINNING_ENABLED = False
PINNED_CERTIFICATE_HASHES = []
HSTS_MAX_AGE = 31536000
HSTS_INCLUDE_SUBDOMAINS = True
HSTS_PRELOAD = True
X_FRAME_OPTIONS = "DENY"
X_CONTENT_TYPE_OPTIONS = "nosniff"
X_XSS_PROTECTION = "1; mode=block"
CONTENT_SECURITY_POLICY = "default-src 'self'"
REFERRER_POLICY = "strict-origin-when-cross-origin"
PERMISSIONS_POLICY = "geolocation=(), microphone=()"
EVENT_BUS_BACKEND = "local"
EVENT_BUS_TOPICS = ["deposit.created", "deposit.completed", "deposit.failed"]
FEATURE_FLAG_ANALYTICS_V2 = True
FEATURE_FLAG_DYNAMIC_PRICING = False
FEATURE_FLAG_WEBHOOK_RETRY = True
SYSTEM_BOOT_TIME = os.getenv("SYSTEM_BOOT_TIME", "2026-01-01T00:00:00Z")
DEPLOYMENT_REGION = os.getenv("DEPLOYMENT_REGION", "ap-south-1")
INFRASTRUCTURE_PROVIDER = os.getenv("INFRASTRUCTURE_PROVIDER", "vercel")
STORAGE_BACKEND = os.getenv("STORAGE_BACKEND", "local")
STORAGE_BUCKET_NAME = os.getenv("STORAGE_BUCKET_NAME", "Rain-storage")
STORAGE_REGION = os.getenv("STORAGE_REGION", "ap-south-1")
CDN_BASE_URL = os.getenv("CDN_BASE_URL", "")
CDN_ENABLED = False
IMAGE_OPTIMIZATION_ENABLED = True
MAX_UPLOAD_FILE_SIZE_MB = 10
ALLOWED_UPLOAD_EXTENSIONS = [".jpg", ".jpeg", ".png", ".pdf", ".pem"]
PDF_GENERATION_ENGINE = "reportlab"
EXCEL_GENERATION_ENGINE = "openpyxl"
TEMPLATE_ENGINE = "jinja2"
I18N_DEFAULT_LANGUAGE = "en"
I18N_FALLBACK_LANGUAGE = "en"
TRANSLATION_LOAD_PATH = "locales"
CURRENCY_SUPPORTED_LIST = ["USD", "USDT", "INR", "BUSD", "USDC"]
CURRENCY_DEFAULT_DISPLAY = "USD"
CURRENCY_DECIMAL_PLACES = 2
CRYPTO_DECIMAL_PLACES = 8
EXCHANGE_RATE_PROVIDER_PRIMARY = "currencyapi"
EXCHANGE_RATE_PROVIDER_SECONDARY = "binance"
EXCHANGE_RATE_PROVIDER_FALLBACK = "static"
BLOCKCHAIN_EXPLORER_BSC = "https://bscscan.com"
BLOCKCHAIN_EXPLORER_POLYGON = "https://polygonscan.com"
GAS_PRICE_ORACLE_ENABLED = False
GAS_PRICE_DEFAULT_GWEI = 5
MAX_GAS_LIMIT = 500000
TRANSACTION_TIMEOUT_SECONDS = 300
NOTIFY_ADMIN_ON_HIGH_VALUE_DEPOSIT = True
HIGH_VALUE_DEPOSIT_THRESHOLD_USD = 1000.0
AUTO_REFUND_THRESHOLD_USD = 0.0
MANUAL_REVIEW_REQUIRED_ABOVE_USD = 5000.0
KYC_VERIFICATION_REQUIRED_ABOVE_USD = 10000.0
AFFILIATE_PROGRAM_ENABLED = False
AFFILIATE_COMMISSION_PERCENTAGE = 5.0
REFERRAL_BONUS_USD = 1.0
PROMO_CODE_MAX_DISCOUNT_PERCENTAGE = 50.0
LOYALTY_POINTS_PER_USD = 10
LOYALTY_REDEMPTION_RATE = 0.01
MARKETING_NEWSLETTER_ENABLED = False
SUPPORT_TICKET_AUTO_CLOSE_DAYS = 7
FAQ_BASE_URL = "https://t.me/RainOrdersGroup"
API_DOCS_URL = "https://t.me/RainOrdersGroup"
FEEDBACK_EMAIL = "feedback@arrsnetworkzone.in"
SECURITY_EMAIL = "security@arrsnetworkzone.in"
ABUSE_EMAIL = "abuse@arrsnetworkzone.in"
PRESS_EMAIL = "press@arrsnetworkzone.in"
PARTNERSHIPS_EMAIL = "partners@arrsnetworkzone.in"
LEGAL_EMAIL = "legal@arrsnetworkzone.in"
COMPLIANCE_EMAIL = "compliance@arrsnetworkzone.in"
HR_EMAIL = "hr@arrsnetworkzone.in"
INVESTOR_EMAIL = "investors@arrsnetworkzone.in"
SYSTEM_MAINTENANCE_WINDOW_UTC = "02:00-04:00"
BACKUP_RETENTION_COUNT = 30
HEALTH_CHECK_TIMEOUT = 5
METRICS_COLLECTION_TIMEOUT = 5
EXTERNAL_API_RETRY_COUNT = 3
EXTERNAL_API_BACKOFF_FACTOR = 1.5
HTTP_CLIENT_FOLLOW_REDIRECTS = True
HTTP_CLIENT_MAX_REDIRECTS = 5
DB_CONNECTION_TIMEOUT = 10
DB_STATEMENT_TIMEOUT = 30
REDIS_SOCKET_TIMEOUT = 5
REDIS_CONNECTION_POOL_SIZE = 50
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = True
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
CELERY_TASK_ACKS_LATE = True
CELERY_TIMEZONE = "Asia/Kolkata"
LOG_TO_FILE = True
LOG_FILE_PATH = "logs/RainDeals.log"
LOG_MAX_FILE_SIZE_BYTES = 10485760
LOG_BACKUP_COUNT = 5
LOG_JSON_FORMAT = True
SENTRY_ENABLED = False
DATADOG_ENABLED = False
NEW_RELIC_ENABLED = False
PROMETHEUS_ENABLED = True
OTEL_ENABLED = False
TRACE_SAMPLE_RATE = 0.05
FEATURE_FLAG_DARK_MODE = True
FEATURE_FLAG_PUSH_NOTIFICATIONS = False
FEATURE_FLAG_BIOMETRIC_AUTH = False
FEATURE_FLAG_TWO_FACTOR_AUTH = True
FEATURE_FLAG_EMAIL_VERIFICATION = True
FEATURE_FLAG_PHONE_VERIFICATION = False
FEATURE_FLAG_MAINTENANCE_BANNER = False
FEATURE_FLAG_ANNOUNCEMENT_MODAL = True
FEATURE_FLAG_RATE_LIMITING = True
FEATURE_FLAG_IP_BLOCKING = True
FEATURE_FLAG_CAPTCHA_VERIFICATION = False
CAPTCHA_PROVIDER = "none"
CAPTCHA_SITE_KEY = ""
CAPTCHA_SECRET_KEY = ""
TERMS_OF_SERVICE_VERSION = "1.0.0"
PRIVACY_POLICY_VERSION = "1.0.0"
COOKIE_POLICY_VERSION = "1.0.0"
SYSTEM_INITIALIZED = True
