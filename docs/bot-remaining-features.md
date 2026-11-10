# Remaining five bot features

Implemented in `E:\BotCodex\RainDealsBott`, alongside the previous five-feature
update. No production deployment or real payment/order test was performed.

## Warranty and refund requests

Admin product setup now chooses Warranty, Refund, or Skip. New purchases save
that policy and the configured duration. Claim forms collect the problem,
reported deactivation time, evidence and confirmation. Refunds require a
screenshot and use the requested 1–2 / 3–7 / 8+ day computation. Approval credits
the saved amount once to the bot wallet; replacements remain admin-provided.
See [the aftercare guide](aftercare-policy.md) for the calculation, review and
migration details. Existing orders are not rewritten when product settings change.

## Provider health

Provider observations record HTTP request/failure counts, consecutive failures,
last successful requests/catalog syncs, and safe error classifications internally.
The Provider Health button and dashboard callback were removed from the bot. Catalog stock is marked stale after 120 seconds without a
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
an order or submitting a claim. Warranty/refund forms and admin drafts can now
resume through `/restart`; see [sessions and invoices](sessions-and-invoices.md).

## Notification management

Admin → Notifications shows durable queued, sending, sent, failed, uncertain and
resolved jobs for the current bot identity. Admin/public order alerts,
stock/restock broadcasts, low-stock/referral alerts, warranty updates, supplier
completion/cancellation, refund notices, deposit/promo group notices, required
information alerts and delivery-start links use the outbox. Restock subscription
consumption and queue insertion share a transaction. Keyboards are preserved.
Bulk broadcast delivery stays on its existing sender; each successful copy is
also recorded in the user's inbox. Interactive card edits are not inbox events.
The separate delivery bridge retains its own send/retry path; its order event
appears in the inbox when the main bot queues the corresponding customer update.
This dashboard does not track arbitrary Telegram messages or another bot's
independent queue.

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

Internal diagnostics record process-local totals, failures, average latency and
P95 for database execution, supplier operations, Telegram methods and handler
groups. The Diagnostics button and dashboard callback were removed from the bot. Database samples carry safe operation names such as product/category;
SQL text, parameters and user input are not retained. Telegram `getUpdates`
long-poll waiting time is excluded. Pool checkout now records acquisition and
connection-creation time separately. P95 uses the latest 200 samples per
operation; the entire registry is capped at 200 operations and resets when the
process restarts.

## Phase 5–7 hardening

Deposit verification now claims work with an expiring database lease, so two
bot processes or the scheduled checker and a user's check button cannot
concurrently verify and credit the same pending invoice. A crash releases the
claim automatically after the lease expires. Startup schema setup adds the
lease columns and index; if the database user cannot run DDL, apply
`migrations/20261110_phase5_deposit_leases.sql` once.

For Binance crypto deposits and Binance Pay, a successful matched provider
record credits automatically without an admin approval step. If a verified
transfer is below the requested amount, the bot credits only the actual amount
confirmed by Binance. A late crypto TXID may be submitted for up to
`DEPOSIT_LATE_REFERENCE_HOURS` (default 72), but Binance's recorded transfer
time must still fall inside that invoice's original validity window, with a
five-minute clock tolerance. Binance outages, denied requests, missing history,
or location restrictions remain pending because the bot has no authoritative
confirmation; it does not bypass restrictions or treat an unverified TXID as
payment.

The verifier bounds concurrent checks with `DEPOSIT_VERIFICATION_CONCURRENCY`
(default 4, maximum 8), serializes access to the shared Binance SDK client, and
uses finite IMAP timeouts. UPI email scans default to seven days and 200
messages, configurable with `IMAP_LOOKBACK_DAYS`, `IMAP_TIMEOUT_SECONDS`, and
`UPI_MAX_EMAILS_TO_SCAN`.

New saved session snapshots use `SESSION_RESUME_ENCRYPTION_KEY` or
`API_KEY_ENCRYPTION_KEY`, not the rotating Telegram bot token. Keep one of
those keys stable across deployments to preserve saved drafts; the bot token
is retained only to decrypt legacy snapshots. Resume persistence is attempted
immediately, and a failed cached write is discarded so the next update can
retry it. Form replies no longer invalidate a newer screen as if they were
navigation; commands and callback navigation still supersede stale renders.

Notification events now coalesce only while queued, preserving the newest
queued message while leaving sent, failed, and uncertain records unchanged.
Order-progress notifications have a dedicated preference category, and admin
reroutes use per-record deduplication keys. Telegram cannot guarantee exactly
once sends after an ambiguous timeout; those remain uncertain and are not
automatically replayed.

## Phase 7–9 hardening

Notification cleanup now removes only old terminal rows after the configured
retention period (`NOTIFICATION_RETENTION_DAYS`, default 365, minimum 90 days).
Unread inbox items, queued/sending jobs and uncertain sends are retained.
Admins may explicitly retry an uncertain notification after a warning that it
could duplicate; the retry is audited. Admin order sent timestamps are changed
only by a confirmed send of an `admin_order` notification.

Rejected warranty/refund claims can be resubmitted. The prior rejected claim,
including its saved invoice, reason and proof, is archived as a revision before
the current request is reopened. Refund processing reverses only commission
amounts recorded on the source order; legacy orders with no saved amount are
flagged for manual audit rather than recalculated using today's commission
rate. If a commission was already spent, reversal is capped at the remaining
wallet/earnings instead of creating a negative balance.

Admin order callbacks validate identifiers and recheck the current order state
inside locked transactions. Delivery Bot `queued` and `waiting_for_start`
results remain `delivery_pending`; warranty coverage and preorder commission
start only after Telegram confirms the actual send. Direct bot delivery messages
also complete their order only after the notification outbox records a
successful Telegram send. Delivery-pending orders cannot be refunded until the
remote state is reconciled, preventing refund/delivery races. Proof images are
limited to 2 MB and validated as JPEG, PNG or WebP before storage.

Maintenance reads fail closed on database errors and use a one-second cache so
the database is not queried for every update. Critical schema setup failures
now stop startup; duplicate DDL from concurrent startup is accepted only after
the expected column, index or table is confirmed. Health endpoints report
database/schema readiness rather than returning `ok` when the database is down.
Long-running service loops restart under supervision. Telegram polling uses a
database lease, so only one Render replica polls a bot token at a time; standby
replicas wait and take over after the lease expires. Shutdown waits for pending
encrypted session-draft writes before closing the database/bot resources.

## Deployment and validation

No new secrets are required. `main.py` and `render_api.py` supervise the
background services and coordinate the polling lease. Normal schema setup
checks the feature tables, columns and indexes, including unique payment
references and API request IDs. If the database account cannot create tables,
apply `migrations/20261110_phase8_9_runtime_safety.sql` for warranty revisions
and the polling lease (polling deployments only); apply the Phase 5 deposit
lease migration if it has not already been applied.
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

The earlier feature implementation was validated on 6 November 2026 with 93
Python checks and manager order checks. The Phase 5–9 changes in this checkout
have not been tested against a live payment provider, database, Telegram bot,
or deployment.
