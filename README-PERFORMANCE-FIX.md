# NomanBot performance fix

## What was fixed

- The database pool now uses the configured production values instead of a
  hard-coded pool of two connections.
- The global ban check no longer freezes the async bot loop while it waits for
  TiDB; it runs in a worker and has a short cache.
- `/start` no longer performs a second remote database query, and required
  channel/group membership checks run concurrently and are cached briefly.
- Admin dashboard and statistics database queries run outside the bot event
  loop.
- Product-list, product-detail, account-editor, provider-selection, and admin
  back-navigation queries also run outside the bot event loop.
- Deposit verification releases its database connection before waiting on
  RPC, Binance Pay, or IMAP/network services. This prevents pending deposits
  from making normal commands wait for a database connection.

## Deploy exactly one mode

Use **one** of these modes for a given bot token, never both:

1. **Render/Railway/VPS polling:** run `main.py`. Do not configure a Telegram
   webhook for this token.
2. **Vercel webhook:** deploy `api/index.py` and run `set_webhook.py` once
   after deployment. Do not run `main.py` at the same time.

Both modes call Telegram webhook APIs. Starting both causes one deployment to
disable the other, so commands may appear to stop working.

## Recommended environment values

```env
DATABASE_POOL_SIZE=10
DATABASE_MAX_OVERFLOW=20
DATABASE_POOL_TIMEOUT=10
MEMBERSHIP_CACHE_TTL=300
BANNED_USER_CACHE_TTL=30
```

Keep your existing `.env` file private and copy it into the deployed project.
The delivery archive intentionally excludes it so bot, database, payment, and
email credentials are not copied accidentally.


## Manual fallback, partial delivery, and resume

The product creation wizard now asks whether unavailable uploaded inventory may be fulfilled manually. Strict automatic/hybrid products with no uploaded accounts offer a switch to manual; declining keeps them unavailable until accounts are uploaded. Manual or fallback-enabled products ask for order capacity (for example 1000), separately from uploaded account count. Existing products have a Manual fallback toggle in Admin → Products. Edit Stock or `/setstock ID CAPACITY` changes their capacity.

Example: hybrid, capacity 1000, two accounts uploaded, order quantity three. Checkout deducts three from capacity, sends the two ready accounts, and records one remaining manual unit. Disabling fallback limits checkout to the two uploaded accounts, regardless of a larger capacity counter. Final manual delivery appends to the original delivery. Required buyer information holds automatic accounts until submission; a partial order remains pending manual afterward. Refund tracking restores the reserved capacity and only allocated automatic inventory, excluding free-form manual instructions.

`/start` clears the customer draft and opens Home. `/restart` restores a resumable customer input/navigation step. A saved confirmation is rebuilt with current stock and price, never automatically purchased. Drafts are stored in `bot_resume_sessions` for 24 hours. Admin/password/provider credential flows are excluded. Seven-minute inactivity prompts provide Continue and Home choices. Invoice expiry remains five minutes; an expired/closed invoice requires an explicit payment-step button, without extending the old invoice, charging, or replaying a submitted reference.

The existing startup schema helper adds `products.manual_fallback`, `orders.automatic_quantity`, `orders.manual_pending_quantity`, `orders.stock_deducted_quantity`, and the resume table. Existing manual/hybrid products retain manual fulfillment on first migration; automatic products remain strict. Startup needs CREATE/ALTER permission in the existing application database. Deploy the Render bot first so this schema exists, then deploy the updated Cloudflare manager if using web fulfillment. The separate Cloudflare public API checkout implementation is not changed by the Python bot deployment; Python reseller API orders use the shared bot checkout policy.

Checks: `python -m unittest discover -s checks -p "test_*.py" -v`. They use isolated fixtures and do not create live orders or credits.
