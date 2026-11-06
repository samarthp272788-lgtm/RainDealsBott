# Claim dossiers in the bot and manager

## Missing subscription / delivery date

The original manager product form omitted aftercare fields. Earlier manager
versions could also fulfill orders without setting the completion clock. The
updated manager saves Warranty / Refund / Skip and its duration, and new manual
fulfillments save the coverage start. The Python bot still captures the policy
on purchase; product edits never rewrite existing order policies.

When a completed order has no saved delivery date, the claim screen looks for a
recorded completion event, manager fulfillment audit, bot delivery audit or sent
delivery job. Native automatic local orders can use their purchase time when
delivery occurred in the same transaction. Recovered dates are persisted; the
system never substitutes today and extends an older order's coverage.

If the duration or date cannot be established, the customer can still submit an
issue and proof. Refund approval stays blocked until staff confirms the real
subscription length and delivery time, with a recorded reason. The calculation
then uses the original claim submission timestamp. No product price or current
product policy is silently substituted for the purchased contract.

## Customer flow

My Orders → Claim warranty / Ask for refund:

1. Check the original product, order reference, quantity and paid amount.
2. Confirm this is the product with the issue.
3. Select Deactivated / Cannot log in / Subscription missing / Other.
4. Describe the issue and when it stopped working.
5. Supply screenshot proof (required for refunds; written evidence also works
   for warranties), hiding passwords/unrelated personal information.
6. Review the saved refund estimate, or its pending-metadata notice, and submit.

New photo proofs are downloaded by the bot and saved privately in the database,
up to 2 MB, as JPG/PNG/WebP. The claim captures its original order invoice and
delivered information, so later product/delivery edits do not erase what was
being reviewed. A deposit PAYOUT invoice is separate from this product-order
invoice; wallet-funded purchases are not assigned a fabricated deposit link.

## Review in the bot

Admin → Warranty / refund requests opens the request with invoice, issue,
customer confirmation, reason, reported deactivation time and proof. Buttons
open the saved invoice, delivered information, full order/submitted buyer
details, and evidence. Missing metadata can be confirmed using:

`days | YYYY-MM-DD HH:MM:SS (UTC) | reason`

Replacements can be approved, declined, then delivered. Refunds require proof
review and a separate explicit approval. Money returns to the bot wallet once.

## Review on the manager website

Open **Warranty / refund claims**, or **View order details → Open claim, proof &
invoice**. Active/all-history views are searchable and paginated. Each dossier
shows the original invoice/product, customer-confirmed issue, screenshot, buyer
submissions, saved calculation, delivered information and recorded history.
The invoice can be printed / saved as PDF; printed invoices exclude passwords.

Existing Orders permission grants claim read access. Orders-write permission is
required for metadata correction or review actions. Delivery credentials require
the existing delivery-secrets capability as well; completed replacement
credentials are masked for other staff. Proof routes require a signed-in manager
and never return a Telegram token or public file URL.

Older proofs may only contain a Telegram file ID. The manager can cache them
server-side using its existing main-bot token when configured. Otherwise, open
**View evidence** on the claim in the shop bot once; that caches the image for
web review without adding a new manager secret.

Bot and web review lock the same order/claim records, use the same stored refund
amount, and queue the same customer notification event. Repeated approval cannot
credit twice. Failure to save the notification rolls back the credit. This flow
does not return used account credentials to inventory or send an external refund.

## Deploy

Deploy the Python bot and Cloudflare manager update together. Normal startup
checks add the fields. Without schema-write permission, apply the missing ALTERs
in `migrations/20261106_claim_dossier.sql` once. The manager also checks its required
aftercare columns. Existing orders with no trustworthy plan/date need staff
confirmation; deployment does not invent those values or credit real payments.

## Validation

Local fixture checks on 6 November 2026 passed: 122 Python tests, the manager
aftercare checks, existing order checks and manager security checks. These cover
product confirmation, immutable invoice/delivery snapshots, evidence validation,
date recovery/correction, role-based credential masking, proof caching, atomic
refunds, duplicate approval and transaction rollback. Syntax/whitespace checks
also passed. No live orders, refunds or production schema were changed.
