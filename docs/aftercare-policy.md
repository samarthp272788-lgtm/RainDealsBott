# Product warranty and refund policy

The shared bot/manager claim dossier now adds explicit product confirmation,
issue categories, private screenshot storage, original invoice/delivery snapshots,
and historical metadata recovery. See [claim dossiers](claim-dossier.md) for the
current review screens and migration instructions.

## Setup

After the description of an own product, choose **Warranty**, **Refund**, or
**Skip**. Provider imports ask the same question after the selling price and
before saving. For existing products use Admin → Products → Manage product →
the aftercare policy button. Enter 1–3650 warranty or subscription days as needed.
Skip removes the claim button from new orders.

Each new order saves its policy and duration. Editing a product later does not
change earlier purchases. Earlier warranty orders retain their original coverage;
refund eligibility is not retroactively granted to existing orders.

## Customer form

My Orders → order → **Claim warranty** or **Ask for refund** opens the saved
policy and, for refunds, the calculation. The form asks:

1. What stopped working or was deactivated?
2. When did it stop working? Include date/time/timezone, or say unknown.
3. Upload deactivation/error proof. Refunds require a screenshot sent as a photo.
   Warranty claims also accept written evidence. Hide passwords and unrelated data.
4. Review the claim and refund estimate, then explicitly submit to admin review.

Nothing is credited when the form is opened or evidence is uploaded. One request
per order prevents repeated submissions. Cancelling returns to the claim screen.
Order ownership, delivery, refund status and expiry are checked on the server.

## Calculation

Usage starts when the complete order is delivered. Each started 24-hour period
counts as one day: the first 24 hours are day 1, the next are day 2, and so on.
For hybrid orders the clock starts after the final manual portion is completed.

| Days used | Refund |
| --- | --- |
| 1–2 | Full amount actually paid |
| 3–7 | paid amount ÷ subscription days × remaining days × 0.80 |
| 8+ | paid amount ÷ subscription days × remaining days × 0.75 |

Remaining days = max(subscription days − days used, 0). Refunds are capped at
the original amount and rounded down to eight USDT decimals. No new claim is
accepted after expiry or if no refundable amount remains. The calculation uses
the total actually paid, including quantity and the customer's custom price.

Example for a $30 order lasting 30 days: day 2 → $30; day 3 → $21.60;
day 7 → $18.40; day 8 → $16.50.

The estimate is saved at submission time. If the rate changes while the form is
open, the customer must review the new amount and submit again. An admin's review
delay does not reduce that saved request amount. The customer's reported
deactivation time is evidence for review; it cannot backdate the calculation.

## Admin review

Admin → **Warranty / refund requests** shows the problem, reported deactivation
time, evidence and saved calculation. Review the screenshot, then approve or
decline with a reason. Warranty approval is followed by entering replacement
details. Refund approval has a separate confirmation that proof was checked.

An approved refund credits the customer's **bot wallet**, records the amount,
closes the order as refunded and queues a notification in one database transaction.
Repeated approvals cannot credit again. A proportional referral commission is
reversed using the existing commission configuration. Used accounts are never
returned to sellable stock by this refund flow. It does not issue an external
Binance/UPI payment. Existing admin full-order refund controls remain separate.

## Rollout

Python startup checks/adds the new columns. For installations without DDL
permission, apply `migrations/20261106_aftercare_policy.sql` once and skip ALTERs
already applied. No production schema was modified during local development.
Deploy the manager Worker update too if manual orders are fulfilled through the
web: it records the completion time used by this policy. The Render Python bot
and supplier completion paths also start the saved clock on completion.

Provider Health and Diagnostics have been removed from the bot menu. Their old
callbacks no longer return those dashboards; background observations remain.

Local validation on 6 November 2026: 107 Python checks passed, including formula
boundaries, saved-price/rounding rules, proof and owner checks, concurrent refund
approval, transaction rollback and real form transitions. The manager order checks
and syntax checks also passed. These fixture tests do not perform real refunds or
deploy the bot; verify the configured policy with a staging order after rollout.
