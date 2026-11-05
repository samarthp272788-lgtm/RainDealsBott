# The agreed ten features

Scope: the ten improvements requested in chat. This is implementation and local
validation, not the separate larger platform proposal or a production deployment.

| Feature | Entry point / behavior | Main checks |
| --- | --- | --- |
| Shared screen controller | Existing menu helpers share card tracking, rendering locks and cleanup. | Single card, history clearing, `/start`, `/restart`, QR transitions. |
| Latest-click handling | Superseded requests cannot overwrite a newer card or its input state through the shared helpers. | Slow product → Home, state-write race, independent users. |
| Duplicate-action protection | Persistent checkout keys; immutable owner-bound payment references; locked/conditional credits; shared promo redemption; API response-loss recovery. | Parallel clicks, parallel credits, changed references, promo limits/replays, same API client ID. |
| Order timeline | Customer-safe purchase, information, partial delivery, team assignment, completion and warranty events. | Ownership, private-note redaction, event order for equal timestamps. |
| Admin fulfillment queue | Ready/waiting-info/mine/unassigned views; priorities, remaining units, assignment and private notes. | Closed orders, assignment/release, cross-admin delivery rejection in bot and manager. |
| Warranty/replacements | Product policy captured on purchase, owner evidence, admin review and replacement details. | Expiry, refunds, duplicate requests/actions, long evidence, transactional notification. |
| Provider health | Failed HTTP requests, last success/sync, stale stock and safe error categories. | Buffering, sync failures, stale timestamps, credential redaction. |
| Shared input forms | Common quantity/email/phone/text/photo/contact validation and cancellation handling. | Invalid/oversized input, cancellation, correct return card. |
| Notification management | Durable queue, keyboard preservation, destination isolation, known-failure retries and uncertain-send review. | Concurrent claims, rate limits, process interruption, transactional restock/refund/supplier notifications. |
| Performance diagnostics | Safe aggregate DB/supplier/Telegram/handler latency and failure counts. | No SQL parameters/user text retained, bounded memory, excludes long-poll waiting. |

## Duplicate-payment and API behavior

- Submitting the same reference again acknowledges the existing invoice. A
  different reference cannot replace it. Completed invoices cannot be expired
  or overwritten by a stale input message.
- Only an independently verified payment reaches the final credit function.
  The credit transaction claims the deposit and increments wallet/aggregate
  balances atomically. The Binance/UPI verification source and matching rules
  were not replaced with a trust-based shortcut.
- Final failure handling cannot demote or delete an already completed payment.
- Both promo entry points use the same redemption transaction. Per-user promo
  references support multi-user codes, preserve legacy redemption checks and
  enforce the total-use limit without counting the same user twice.
- API retries reuse `(api_key_id, client_order_id)`. A committed purchase can
  be recovered by its saved checkout key if the response mapping was not saved.
  Uncertain supplier calls are not automatically placed again.

## Rollout

1. Deploy the Python bot changes with the existing secrets/database settings.
2. Allow schema setup, or apply the documented migration/index changes with a
   database administrator. Review legacy duplicates if an index cannot be built.
3. Deploy the manager Worker changes if using web fulfillment.
4. Test with the staging bot: navigation, one low-value order, required details,
   manual/hybrid fulfillment, one test payment, warranty evidence and notifications.

## Local validation

On 6 November 2026, all 93 Python checks passed, including the isolated startup
smoke test. The manager order checks also passed. Python/JavaScript syntax and
Git whitespace checks completed successfully.

Automated checks use temporary fixture databases, synthetic settings and mocked
Telegram calls. They do not establish live provider correctness, zero network
latency, or exactly-once Telegram delivery after an ambiguous timeout. No live
orders, payments or production data were changed during this implementation.
