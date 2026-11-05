# Remaining five bot features

Implemented in `E:\BotCodex\RainDealsBott`, alongside the previous five-feature
update. No production deployment or real payment/order test was performed.

## Warranty and replacement requests

- Admin → Products → Manage product → Warranty. Enter 0–365 days. Zero is
  disabled by default; existing orders are not retroactively granted coverage.
- New Python-bot purchases capture that product's warranty duration. The expiry
  starts once the order is completed/fulfilled, including after required buyer
  information, a supplier callback or final hybrid/manual fulfillment. It is
  not reset by refreshing, editing the product policy or retrying delivery.
- My Orders → order → Warranty / replacement: submit a reason and photo or
  written evidence while coverage is active. One request per order prevents
  repeated claims. The server checks ownership, status, refund state and expiry
  again when the evidence is submitted.
- Admin → Warranty requests: view evidence, approve or decline with a reason,
  then enter replacement details for an approved claim. Repeated approval or
  delivery taps cannot create another replacement notification. A request made
  within coverage can still be reviewed after its expiry.
- Approval does not debit a wallet or consume uploaded stock automatically.
  Replacement details are provided by the admin and remain accessible in the
  request. The customer receives a queued status/delivery notification.
- Warranty content and photos are owner/admin only. Do not put passwords in
  evidence or diagnostic notes.
- Full reasons/evidence have a separate details card so long evidence does not
  push replacement credentials out of the warranty summary. Completed requests
  remain available from the customer's order and the admin's order details.

## Provider health

Admin → Provider health displays observed HTTP request/failure counts, consecutive
failures, last successful request, last successful catalog sync, and a safe error
classification. Catalog stock is marked stale after 120 seconds without a
successful sync. Observations come from existing traffic; the dashboard does
not place orders or repeatedly probe suppliers.

Observations are buffered outside the request path and saved approximately
every ten service ticks. The buffer is capped at 2,000 observations, so long
database outages/process crashes can lose diagnostic samples. Counts describe
observed HTTP requests; an HTTP 200 response rejected by a catalog parser can
instead appear as a sync error. URLs, request headers, API keys and raw provider
response bodies are not stored by the health feature.

## Reusable input forms

Quantity entry and required order fields share validation helpers with the new
warranty forms. They validate integer quantity/range, email, phone, text length,
photo and Telegram contact inputs. Cancellation clears the draft without placing
an order or submitting a claim. Warranty drafts are intentionally not included
in the persisted `/restart` whitelist; reopen their order if the draft expires.

## Notification management

Admin → Notifications shows durable queued, sending, sent, failed, uncertain and
resolved jobs for the current bot identity. Admin/public order alerts,
stock/restock broadcasts, low-stock/referral alerts, warranty updates, supplier
completion/cancellation, refund notices, deposit/promo group notices, required
information alerts and delivery-start links use the outbox. Restock subscription
consumption and queue insertion share a transaction. Keyboards are preserved.
Interactive card edits, bulk admin broadcasts and the separate delivery bridge
retain their own paths; this dashboard does not claim to track every Telegram
message or another bot's queue.

- Stable event keys prevent duplicate enqueueing. Conditional claims and
  database locks prevent simultaneous workers from sending the same queued job.
- Explicit Telegram rejections become failed jobs. An admin can retry them
  after fixing the cause; the retry is audited and cannot be queued twice.
- Telegram rate limits defer the job until the indicated retry time.
- A rejected destination pauses further jobs for that destination for five
  minutes while other recipients continue. Dedicated indexes keep the queued
  job and destination lookups bounded as notification history grows.
- Network timeouts and interrupted sends are uncertain: Telegram may have
  accepted the message. They are never automatically resent. Check the
  recipient's chat, then resolve without resending through the dashboard.
  Resolving does not fabricate a Telegram delivery timestamp.
- Telegram has no general send-message idempotency key. This outbox reduces
  duplicate risk, but cannot prove exactly-once delivery after an ambiguous
  network failure. Interrupted sending claims are held after five minutes.

Admin order alert producers save the queue handoff on the order; the existing
sent timestamp is set only after Telegram confirms success. This prevents a bad
destination at the head of the queue from hiding newer orders. Notification
records are bot-scoped, so a staging bot cannot consume production bot jobs.

## Performance diagnostics

Admin → Diagnostics shows process-local totals, failures, average latency and
P95 for database execution, supplier operations, Telegram methods and handler
groups. Database samples carry safe operation names such as product/category;
SQL text, parameters and user input are not retained. Telegram `getUpdates`
long-poll waiting time is excluded. Pool acquisition delay is not separately
measured. P95 uses the latest 200 samples per operation; the entire registry is
capped at 200 operations and resets when the process restarts.

## Deployment and validation

No new secrets are required. `main.py` and `render_api.py` start and stop the new
notification/health service. Normal schema setup checks the feature tables,
columns and indexes, including unique payment references and API request IDs.
If DDL permission is unavailable, an administrator may apply the relevant
migration files once, skipping already-applied ALTERs and equivalent indexes.
`migrations/20261106_feature_integrations.sql` covers upgrades from the first
operations implementation. Existing financial history is never deleted or
rewritten to make an index succeed.

The Cloudflare manager has a compatible optional-column update that starts
coverage when fulfilling a covered order. It also respects the bot's assignment
and records fulfillment timeline events. Deploy that manager change too if it
is used for those orders. Its existing role permissions remain in force. The
Python bot features above do not move the main bot away from Render.

Checks run locally:

```powershell
Set-Location E:\BotCodex\RainDealsBott
python -m unittest discover -s checks -p 'test_*.py' -v
node cloudflare/manager-worker/order-checks.mjs
```

SQLite fixture tests exercise ownership, expiry, transactional rollback,
duplicate claims, known failures, rate limits, uncertain sends, concurrent
notification consumers and diagnostic redaction. Telegram sends are mocked.
They do not replace a staging check with the actual database and bots.

Validation completed on 6 November 2026: 93 Python tests passed, along with the
manager order checks, JavaScript/Python syntax checks and Git whitespace checks.
The startup smoke test imports the real bot/API entry points and configures the
ORM using dummy settings with database/network connections blocked. It also
verifies the new menus and masked database exception parameters.
