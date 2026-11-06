# Customer tools and notification routing

The main Telegram bot continues to run on Render. These changes do not deploy it to Cloudflare or change webhook secrets.

## One private screen

Opening a claim review replaces the tracked private menu. Returning Home or sending `/start` removes the tracked review card. Known older private admin alerts are also removed on `/start`, using their saved notification message IDs. Group cards are not deleted when a different admin opens a screen. Telegram may refuse deletion of a very old message; the bot cannot erase arbitrary untracked chat history.

Operational alerts (claim submissions/replies, cancellation requests, low stock, tickets and failed deposit verification) use the existing `ADMIN_ORDER_NOTIFICATION_CHANNEL_ID`, falling back to `NOTIFICATION_CHANNEL_ID` (the existing default is `@RainNotify`). Customer receipts and requested admin screens still appear in their own conversations. Queued old private claim/stock/ticket alerts are rerouted with duplicate protection.

## Cart

Choose quantity on a product, then **Add to cart**. Open Cart from the home menu. Each compact item has quantity and remove controls; the cart supports up to 20 different products, six per page.

Checkout reviews current custom/bulk prices, availability and wallet balance. The review expires after five minutes. Confirmation rechecks the reviewed cart, and every item gets a durable, unique checkout key. A changed price is rejected before purchase. Cart contents do not reserve stock.

Items are separate orders, processed in sequence. If one fails, successful orders stay purchased; failed and unprocessed items remain in the cart. Open each saved order for its receipt, delivered credentials and required buyer-information form. An uncertain supplier result holds the checkout rather than sending another supplier order. Admin Notify receives a reconciliation button. Staff must investigate the provider first, then link the exact saved order or explicitly document a confirmed no-order/no-charge result. This action is audited.

## Claims and more proof

Existing warranty/refund claims have a **Conversation / more proof** tile. Staff can request more information in the bot or the manager's claim dossier. The customer replies in the same claim with text or a screenshot below 2 MB. Replies never change the original invoice, submission date or saved refund calculation. Original and follow-up images remain private behind owner/admin or manager-role checks. Pending claims waiting for the buyer are marked `needs_info`.

## Wallet statement

Preferences includes Wallet statement. New main-bot money writes record a balance change and running balance in the same transaction. This covers native purchases/referrals, admin adjustments, verified deposits, promotions and approved claim refunds. Completed deliveries, money receipts and security notifications cannot be muted.

History starts with an explicit opening balance. Writes from a separate service that does not participate in this ledger are recorded as **external wallet adjustment / reconciliation** when the statement is opened or the next recorded main-bot wallet change occurs. Older transaction details are not invented. This statement does not modify the wallet balance.

## Subscription reminders

Product manager has **Subscription term** (0 disables; 1–3,650 days). The web product editor has the same field. This actual subscription length is separate from warranty/refund coverage. New orders snapshot it; their end time is set on final fulfillment, including fulfillment through the manager.

The existing background notification worker checks once a minute and queues one reminder per order/end-date/stage: up to three days remaining, up to one day remaining, and recently expired. Reminders are best-effort after service downtime. Renewals require a new purchase, never an automatic wallet charge. Existing orders are not assigned invented subscription dates.

## Delivery and cancellation

Completed orders have **Received** and **Not working** tiles. Receipt is an explicit, idempotent customer confirmation, not a Telegram read receipt. Not working leads to the existing eligible claim/support flow.

An undelivered local pending order can request cancellation with a reason. Partially delivered and supplier orders use support, because a completed external sale cannot safely be reversed automatically. Staff approves from Admin Notify using the existing transactional refund engine. The order is checked again under lock, so a request cannot refund an order after fulfillment wins the race. The manager shows the request in order details and can decline it; approval remains in the bot to keep inventory/referral reversal in one refund implementation.

## Preferences and schema

Preferences offers restock, optional order-progress and subscription reminders. Muted queued optional notifications are skipped before sending. Restock subscriptions are retained while muted.

Bot startup's existing `ensure_order_requirements_schema` creates the new tools tables and checks missing columns idempotently. It needs the existing database CREATE/ALTER privileges. The optional migration is `migrations/20261106_customer_tools.sql`; its commented column statements are applied only if missing. Deploy the main bot and the manager changes together after review. No new secret is needed.

For staging, check: duplicate Home/review cleanup; one admin notification destination; two-item checkout and repeated confirmation; custom-price change after review; claim request/reply/photo with another user's ID rejected; delivery racing cancellation; ledger rollback; muted reminders and receipts still enabled.
