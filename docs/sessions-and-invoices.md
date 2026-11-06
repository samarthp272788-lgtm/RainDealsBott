# Sessions and payment invoices

- Normal/customer/admin sections pause after **15 minutes of inactivity**.
- Payment input pauses after **20 minutes of inactivity**.
- A deposit invoice lasts **one hour from creation**, independently of session idle time.

Each accepted interaction refreshes the idle timer. It is not a countdown from
the age of the menu message. Time waiting for the user to send more input is idle
time. A paused input is not executed: choose Continue or send `/restart`, then
resend the input. `/start` starts over and clears the saved conversation draft.

Expired button clicks show the original-style Telegram popup without sending or
replacing a separate message. Typed input after expiry updates the current card
with Continue / Home; the original draft prompt stays saved for `/restart`.

## Resume coverage

Product creation/category/aftercare/import, accounts and stock uploads, product
edits, admin delivery, provider setup/editing, custom rates, promo creation,
broadcast input, assignment/notes and normal customer forms retain their input
position, draft fields and last prompt/keyboard. Customer warranty/refund proof
forms can resume too. Draft restoration displays the prompt; it never reruns a
purchase, credit, refund, broadcast or other submission automatically.

Current admin access is checked before an admin draft is restored. Password
verification steps resume at the password prompt; password values and completed
verification grants are not restored. Admin removal prevents draft restoration.

Shared UI helpers and a Telegram request recorder capture prompts, including old
handlers that edit messages directly. The existing wipe-out/wipe-in behavior is
retained for `/restart`, and deleted chat history can still be restored as a new
card. Current in-memory inputs take priority over the deferred database save.

Drafts are retained for up to **24 hours**, refreshed with activity, in the
existing `bot_resume_sessions` table. New writes are encrypted and bound to the
Telegram user/chat using existing server key material. No new secret is required.
Changing `API_KEY_ENCRYPTION_KEY` (or the bot token when no encryption key is set)
invalidates encrypted drafts; it does not erase orders or payments. A process
crash before a deferred save completes may lose the last interaction.

## Payments

Changing UPI/BEP20/Polygon/Binance methods reuses the active invoice and its
original expiry, rather than creating an invoice on every visit. `/restart`
reopens the saved invoice from the database and does not extend its deadline.
After the 20-minute idle pause, an unexpired invoice can still be resumed for the
remainder of its one-hour lifetime. Already-submitted payment references keep
their verification state; they are not resubmitted or credited by restoring UI.

The one-hour lifetime applies to newly created invoices. Existing invoice expiry
timestamps are left unchanged. The normal expiry/recovery job reads those stored
timestamps; no new schema or manual migration is needed for this session update.

Validation on 6 November 2026: all 117 Python checks passed, plus the manager
order checks and Python syntax/whitespace checks. New checks cover live admin
drafts, keyboard/Decimal restoration, role revocation, encrypted database saves,
actual inactivity timing, legacy popup behavior and unchanged invoice expiry
when switching payment methods. No live bot, database or deployment was changed.
