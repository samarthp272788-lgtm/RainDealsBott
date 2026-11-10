"""Saved aftercare policies, evidence review and exactly-once approved wallet refunds."""
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_DOWN
from html import escape

REFUND_RULES = (
    '‼️ <b>REFUND COMPUTATION</b> ‼️\n\n'
    '<b>1–2 days used</b>\nFULL REFUND\n\n'
    '<b>3–7 days used</b>\n(price paid ÷ subscription days) × remaining days × 0.80\n\n'
    '<b>8 days and up used</b>\n(price paid ÷ subscription days) × remaining days × 0.75\n\n'
    'Usage starts at delivery. Each started 24-hour period counts as one day. '
    'Remaining days = subscription days − days used. '
    'The estimate is saved when you submit; approval returns it to your bot wallet.'
)


def policy(value):
    mode = getattr(value, 'aftercare_policy', None)
    if mode is None:
        return 'warranty' if getattr(value, 'warranty_days', 0) else 'skip'
    return mode if mode in ('warranty', 'refund', 'skip') else 'skip'


def policy_snapshot(product):
    mode = policy(product)
    return dict(aftercare_policy=mode, term_days=max(0,int(getattr(product,"term_days",0) or 0)),
                warranty_days=max(0, int(getattr(product, 'warranty_days', 0) or 0)) if mode == 'warranty' else 0,
                subscription_days=max(0, int(getattr(product, 'subscription_days', 0) or 0)) if mode == 'refund' else 0)


def policy_label(product):
    mode = policy(product)
    if mode == 'warranty':
        return f'🛡 Warranty: {product.warranty_days} days'
    if mode == 'refund':
        return f'💸 Refund policy: {product.subscription_days}-day subscription'
    return 'Aftercare: skipped'


def refund_quote(order, now=None):
    """Use saved total paid (including quantity/custom rates), never today's price."""
    days = int(getattr(order, 'subscription_days', 0) or 0)
    started = getattr(order, 'coverage_started_at', None)
    now = now or datetime.utcnow()
    if policy(order) != 'refund' or days < 1 or not started or now < started:
        return None
    used = max(1, (now - started).days + 1)
    remaining = max(0, days - used)
    amount = Decimal(str(order.amount))
    if not amount.is_finite() or amount < 0:
        return None
    factor = Decimal('1') if used <= 2 else Decimal('.80') if used <= 7 else Decimal('.75')
    # Multiply before division so an exact result (e.g. 100 / 30 * 27 * .8)
    # does not round just below 72 before truncating to wallet precision.
    refund = amount if used <= 2 else amount * remaining * factor / days
    refund = min(amount, max(Decimal('0'), refund)).quantize(Decimal('.00000001'), rounding=ROUND_DOWN)
    return dict(paid=amount, subscription_days=days, used_days=used, remaining_days=remaining,
                factor=factor, refund_amount=refund)


def quote_text(quote):
    if not quote:
        return 'Refund calculation is pending staff confirmation of the subscription and delivery date.'
    return (f'Paid: <b>${quote["paid"]:.8f}</b> · Subscription: {quote["subscription_days"]} days\n'
            f'Used: {quote["used_days"]} · Remaining: {quote["remaining_days"]}\n'
            f'Estimated refund: <b>${quote["refund_amount"]:.8f} USDT</b>\n'
            'Subject to admin review of your deactivation proof.')


def summary_text(ctx, admin=False):
    """Keep replacement details complete within one Telegram message."""
    expires = ctx['expires'].strftime('%d %b %Y %H:%M UTC') if ctx['expires'] else 'Not active'
    mode = ctx.get('policy', 'warranty')
    title = 'Refund' if mode == 'refund' else 'Warranty'
    text = f'🛡 <b>Order #{ctx["order_id"]} · {title}</b>\nCoverage: {ctx["days"]} day(s)\nEnds: {expires}\n'
    if ctx.get('invoice'):
        inv = ctx['invoice']
        text += f'Product: <b>{escape((inv["product"] or "Product")[:70])}</b> · Qty {inv["quantity"]}\nPaid: ${escape(inv["paid"])} USDT\n'
    request = ctx['request']
    if not request:
        if mode == 'refund':
            text += '\n' + REFUND_RULES + '\n\n' + quote_text(ctx.get('quote')) + '\n'
        return text + ('\n' + escape(ctx['error']) if ctx['error'] else '')
    reason = request['reason']
    text += f'\nStatus: <b>{escape(request["status"])}</b>\nReason: {escape(reason[:120])}'
    if request.get('issue_type'):
        from services.claim_details import ISSUES
        text += '\nIssue: '+ISSUES.get(request['issue_type'],'Other issue')
    if mode == 'refund' and request.get('refund_amount') is None:
        text += '\nCalculation pending staff confirmation of subscription/delivery details.'
    if request.get('refund_amount') is not None:
        text += (f'\nRefund: <b>${request["refund_amount"]:.8f} USDT</b>'
                 f'\nDays used: {request["used_days"]} · Remaining: {request["remaining_days"]}')
    if request.get('deactivated_at'):
        text += '\nDeactivated (reported): ' + escape(request['deactivated_at'])
    if len(reason) > 120:
        text += '…'
    evidence = request['evidence']
    if evidence.get('kind') == 'text':
        value = evidence.get('value', '')
        text += '\nEvidence: ' + escape(value[:120]) + ('…' if len(value) > 120 else '')
    if request['resolution']:
        if admin:
            text += '\n\nResponse / replacement:\n' + escape(request['resolution'])
        elif request['status'] in ('rejected', 'declined'):
            text += '\n\nThe request was declined. Contact support if you need clarification.'
        else:
            text += '\n\nThe team updated this request. Open Support if you need clarification.'
    return text


def request_text(ctx):
    request = ctx['request']
    text = f'🧾 <b>Order #{ctx["order_id"]} · Request details</b>\n\n' + escape(request['reason'])
    from services.claim_details import ISSUES
    text += '\n\nIssue: '+ISSUES.get(request.get('issue_type'),'Other issue')
    text += '\nProduct confirmed: '+('Yes' if request.get('product_confirmed') else 'Not recorded for this older claim')
    if request.get('deactivated_at'):
        text += '\n\n<b>Deactivated / stopped working (reported)</b>\n' + escape(request['deactivated_at'])
    if request['evidence'].get('kind') == 'text':
        text += '\n\n<b>Evidence</b>\n' + escape(request['evidence'].get('value', ''))
    else:
        text += '\n\nPhoto evidence is available from the warranty screen.'
    return text


def start_coverage(order):
    term_days = int(getattr(order, 'term_days', 0) or 0)
    if term_days and not getattr(order, 'subscription_expires_at', None):
        order.subscription_expires_at = (getattr(order, 'coverage_started_at', None) or datetime.utcnow()) + timedelta(days=term_days)
    mode = policy(order)
    if mode == 'skip':
        return
    days = int(getattr(order, 'warranty_days', 0) or 0)
    if not getattr(order, 'coverage_started_at', None):
        previous_end = getattr(order, 'warranty_expires_at', None)
        order.coverage_started_at = previous_end - timedelta(days=days) if previous_end and days else datetime.utcnow()
    if mode == 'warranty' and days and not getattr(order, 'warranty_expires_at', None):
        order.warranty_expires_at = order.coverage_started_at + timedelta(days=days)


def eligibility(order, user_id, now=None):
    if not order or order.telegram_id != user_id:
        return 'Order not found.'
    if order.status != 'completed' or order.refunded:
        return 'Claims are available after delivery for eligible orders.'
    mode = policy(order)
    if mode == 'skip':
        return 'This order has no warranty or refund policy.'
    if mode == 'refund':
        quote = refund_quote(order, now)
        if not quote:
            return 'The saved subscription or delivery date is unavailable. Contact support.'
        end = order.coverage_started_at + timedelta(days=order.subscription_days)
        if end <= (now or datetime.utcnow()) or quote['refund_amount'] <= 0:
            return 'No refundable subscription days remain.'
        return None
    if not getattr(order, 'warranty_days', 0) or not getattr(order, 'warranty_expires_at', None):
        return 'This order has no replacement warranty.'
    if order.warranty_expires_at <= (now or datetime.utcnow()):
        return 'The warranty period has ended.'
    return None


def configure(product_id, days, admin_id):
    return configure_policy(product_id, 'warranty' if days else 'skip', days, admin_id)


def configure_policy(product_id, mode, days, admin_id):
    from config import ADMIN_IDS
    from database import transaction
    from models.product import Product
    from models.admin_audit_log import AdminAuditLog
    if admin_id not in ADMIN_IDS:
        return 'Access denied.'
    if mode not in ('warranty', 'refund', 'skip') or not isinstance(days, int) or not (days == 0 if mode == 'skip' else 1 <= days <= 3650):
        return 'Choose warranty, refund or skip; enter 1–3650 days for a policy.'
    with transaction() as db:
        product = db.query(Product).filter(Product.id == product_id).with_for_update().first()
        if not product:
            return 'Product not found.'
        product.aftercare_policy = mode
        product.warranty_days = days if mode == 'warranty' else 0
        product.subscription_days = days if mode == 'refund' else 0
        db.add(AdminAuditLog(admin_telegram_id=admin_id, action='warranty_policy', entity_type='product',
                             entity_id=str(product_id), summary=f'Aftercare for new purchases: {mode}, {days} days.'))
    return None


def context(order_id, user_id, admin=False):
    from config import ADMIN_IDS
    from database import SessionLocal
    from models.order import Order
    from models.bot_operations import WarrantyRequest, WarrantyRequestRevision
    from services.claim_details import recover_delivery_date, missing_metadata, invoice, metadata_invoice
    if admin and user_id not in ADMIN_IDS:
        return None
    with SessionLocal() as db:
        order = db.query(Order).filter(Order.id == order_id).with_for_update().first()
        if not order or (not admin and order.telegram_id != user_id):
            return None
        request = db.query(WarrantyRequest).filter_by(order_id=order_id).first()
        mode = policy(order)
        changed = recover_delivery_date(db,order) if mode != 'skip' else False
        if request and not request.bot_id:
            from services.notification_outbox import main_bot_id
            request.bot_id = main_bot_id()
            changed = True
        if request:
            updated_invoice = metadata_invoice(request.invoice_snapshot,order)
            if updated_invoice != request.invoice_snapshot:
                request.invoice_snapshot,changed = updated_invoice,True
            if request.status=='submitted' and request.request_type=='refund' and request.refund_amount is None and not missing_metadata(order):
                recovered_quote=refund_quote(order,request.created_at)
                if recovered_quote and recovered_quote['refund_amount']>0:
                    request.refund_amount,request.used_days,request.remaining_days = recovered_quote['refund_amount'],recovered_quote['used_days'],recovered_quote['remaining_days']
                    changed=True
        if changed:
            db.commit()
        expires = order.warranty_expires_at if mode == 'warranty' else (
            order.coverage_started_at + timedelta(days=order.subscription_days)
            if mode == 'refund' and order.coverage_started_at else None)
        error = eligibility(order, order.telegram_id)
        missing = missing_metadata(order)
        if missing:
            error = 'The subscription or delivery date needs staff confirmation. You can still submit your issue and proof here.'
        return dict(order_id=order_id, error=error, metadata_missing=missing,
                    can_submit=not error or missing, invoice=(request.invoice_snapshot if request else None) or invoice(order),
                    reviewable=order.status == 'completed' and not order.refunded,
                    policy=mode, quote=refund_quote(order), expires=expires,
                    days=order.subscription_days if mode == 'refund' else order.warranty_days,
                    request=None if not request else dict(id=request.id, status=request.status, reason=request.reason,
                         evidence=request.evidence, resolution=request.resolution, admin=request.admin_id,
                         request_type=request.request_type, refund_amount=request.refund_amount,
                         used_days=request.used_days, remaining_days=request.remaining_days, deactivated_at=request.deactivated_at,
                         issue_type=request.issue_type, product_confirmed=bool(request.product_confirmed),
                         proof_available=bool(request.proof_mime)))


def submit(order_id, user_id, reason, evidence, deactivated_at=None, expected_refund=None, *,
           issue_type='other', product_confirmed=True, proof_bytes=None):
    from config import ADMIN_IDS
    from database import transaction
    from models.order import Order
    from models.bot_operations import WarrantyRequest, WarrantyRequestRevision
    from services.input_forms import text_value, InputError
    from services.order_workflow import record_event
    from services.notification_outbox import enqueue
    from services.claim_details import ISSUES, proof_mime, recover_delivery_date, missing_metadata, invoice
    from services.notification_outbox import main_bot_id
    try:
        reason = text_value(reason, 1500, 10)
        if deactivated_at is not None:
            deactivated_at = text_value(deactivated_at, 200)
        if not isinstance(evidence, dict) or evidence.get('kind') not in ('text', 'photo'):
            return 'Add evidence as a photo or a description.'
        if evidence['kind'] == 'text':
            evidence = {'kind': 'text', 'value': text_value(evidence.get('value'), 2000, 10)}
        else:
            evidence = {'kind': 'photo', 'file_id': text_value(evidence.get('file_id'), 500)}
    except InputError as error:
        return str(error)
    if issue_type not in ISSUES or product_confirmed is not True:
        return 'Confirm the purchased product and select the issue first.'
    try:
        mime = proof_mime(proof_bytes) if proof_bytes is not None else None
    except ValueError as error:
        return str(error)
    with transaction() as db:
        order = db.query(Order).filter(Order.id == order_id).with_for_update().first()
        if order and order.telegram_id == user_id and policy(order) != 'skip':
            recover_delivery_date(db,order)
        error = eligibility(order, user_id)
        if error and not (order and order.telegram_id == user_id and missing_metadata(order)):
            return error
        request = db.query(WarrantyRequest).filter_by(order_id=order_id).with_for_update().first()
        if request and request.status not in ('rejected', 'declined'):
            return 'A claim is already open or completed. Open its status from this order.'
        mode = policy(order)
        quote = refund_quote(order) if mode == 'refund' else None
        if mode == 'refund':
            if evidence['kind'] != 'photo' or not deactivated_at:
                return 'Add a screenshot of deactivation and when it stopped working.'
            if expected_refund is not None and (not quote or str(quote['refund_amount']) != str(expected_refund)):
                return 'The refund estimate changed. Review the updated estimate and submit again.'
        saved_invoice = invoice(order)
        # Refunds must reverse the commission actually recorded at purchase,
        # not whatever rate happens to be configured when the claim is filed.
        saved_invoice.update(
            referral_commission_amount=(
                str(order.referral_commission_amount)
                if getattr(order, 'referral_commission_amount', None) is not None else None
            ),
            referral_credit_to_balance=(
                bool(order.referral_commission_to_balance)
                if getattr(order, 'referral_commission_to_balance', None) is not None else None
            ),
        )
        now = datetime.utcnow()
        if request:
            from sqlalchemy import func
            revision = (db.query(func.count(WarrantyRequestRevision.id))
                        .filter(WarrantyRequestRevision.request_id == request.id).scalar() or 0) + 1
            db.add(WarrantyRequestRevision(
                request_id=request.id, order_id=order_id, revision=revision,
                telegram_id=request.telegram_id, status=request.status, reason=request.reason,
                evidence=request.evidence, request_type=request.request_type,
                refund_amount=request.refund_amount, used_days=request.used_days,
                remaining_days=request.remaining_days, deactivated_at=request.deactivated_at,
                issue_type=request.issue_type, product_confirmed=request.product_confirmed,
                invoice_snapshot=request.invoice_snapshot, delivery_snapshot=request.delivery_snapshot,
                proof_image=request.proof_image, proof_mime=request.proof_mime,
                admin_id=request.admin_id, resolution=request.resolution,
                created_at=request.created_at, updated_at=request.updated_at,
            ))
            request.telegram_id, request.status, request.reason = user_id, 'submitted', reason
            request.evidence, request.request_type = evidence, mode
            request.refund_amount = quote['refund_amount'] if quote else None
            request.used_days = quote['used_days'] if quote else None
            request.remaining_days = quote['remaining_days'] if quote else None
            request.deactivated_at, request.issue_type = deactivated_at, issue_type
            request.product_confirmed, request.invoice_snapshot = 1, saved_invoice
            request.delivery_snapshot, request.proof_image, request.proof_mime = order.delivered_account, proof_bytes, mime
            request.bot_id, request.admin_id, request.resolution = main_bot_id(), None, None
            request.created_at = request.updated_at = now
            revision_number = revision + 1
        else:
            request = WarrantyRequest(order_id=order_id, telegram_id=user_id, reason=reason, evidence=evidence,
                                      status='submitted', request_type=mode, deactivated_at=deactivated_at,
                                      issue_type=issue_type, product_confirmed=1, invoice_snapshot=saved_invoice,
                                      delivery_snapshot=order.delivered_account, proof_image=proof_bytes,
                                      proof_mime=mime, bot_id=main_bot_id(),
                                      refund_amount=quote['refund_amount'] if quote else None,
                                      used_days=quote['used_days'] if quote else None,
                                      remaining_days=quote['remaining_days'] if quote else None)
            db.add(request)
            revision_number = 1
        record_event(db, order_id, mode+'_requested', f'{mode.title()} request attempt {revision_number} submitted.', actor_id=user_id)
        from services.notification_outbox import enqueue_admin
        enqueue_admin(db, f'warranty:{order_id}:submitted:{revision_number}',
                      f'🛡 <b>{mode.title()} review needed</b>\nOrder #{order_id}\nAdmin → Warranty / refund requests.',
                      reply_markup={'inline_keyboard': [[{'text': 'Review claim', 'callback_data': f'warranty_admin_{order_id}'}]]})
    return None


def review(order_id, admin_id, operation, resolution=None):
    from config import ADMIN_IDS
    from database import transaction
    from models.order import Order
    from models.bot_operations import WarrantyRequest
    from services.input_forms import text_value, InputError
    from services.order_workflow import record_event
    from services.notification_outbox import enqueue
    if admin_id not in ADMIN_IDS:
        return 'Access denied.'
    if operation not in ('approve', 'reject', 'complete'):
        return 'Unknown action.'
    if operation in ('reject', 'complete'):
        try:
            resolution = text_value(resolution, 3000, 1)
        except InputError as error:
            return str(error)
    with transaction() as db:
        order = db.query(Order).filter(Order.id == order_id).with_for_update().first()
        request = db.query(WarrantyRequest).filter_by(order_id=order_id).with_for_update().first()
        if not order or not request:
            return 'Request not found.'
        if order.refunded or order.status != 'completed':
            return 'The order is closed or refunded; this claim cannot change its balance.'
        expected = 'approved' if operation == 'complete' else 'submitted'
        if request.status != expected:
            return 'This request was already handled. Refresh its status.'
        if request.request_type == 'refund':
            if operation == 'complete':
                return 'Use Approve refund to credit the saved calculation.'
            if operation == 'approve':
                return _approve_refund(db, order, request, admin_id)
        request.status = {'approve': 'approved', 'reject': 'rejected', 'complete': 'completed'}[operation]
        request.admin_id, request.resolution = admin_id, resolution
        summary = {'approve': 'Replacement approved; awaiting team delivery.',
                   'reject': request.request_type.title()+' request declined.', 'complete': 'Replacement delivered.'}[operation]
        record_event(db, order_id, request.request_type+'_'+request.status, summary, actor_id=admin_id)
        body = f'🛡 <b>Order #{order_id} · {request.request_type.title()}</b>\n{summary}'
        enqueue(db, f'warranty:{order_id}:{request.status}', order.telegram_id, body,
                reply_markup={'inline_keyboard': [[{'text': 'Open request', 'callback_data': f'warranty_order_{order_id}'}]]})
    return None


def _approve_refund(db, order, request, admin_id):
    """Called inside the locked review transaction; all accounting rolls back together."""
    from sqlalchemy import func
    from models.order import Order
    from models.user import User
    from models.bot_operations import WarrantyRequest
    from models.admin_audit_log import AdminAuditLog
    from services.order_workflow import record_event
    from services.notification_outbox import enqueue
    amount = request.refund_amount
    if amount is None:
        return 'Confirm the saved subscription and delivery date before approving this refund.'
    if not amount.is_finite() or not 0 < amount <= Decimal(str(order.amount)):
        return 'The saved refund calculation is invalid. No credit was made.'
    # Conditional writes also protect engines that do not honor FOR UPDATE.
    claimed = db.query(WarrantyRequest).filter(WarrantyRequest.id == request.id,
                WarrantyRequest.status == 'submitted', WarrantyRequest.request_type == 'refund').update(
                    {WarrantyRequest.status: 'completed'}, synchronize_session=False)
    if claimed != 1:
        return 'This claim was already handled. Refresh its status.'
    closed = db.query(Order).filter(Order.id == order.id, Order.refunded == False,
                Order.status == 'completed').update({Order.refunded: True, Order.status: 'refunded',
                    Order.refund_amount: amount}, synchronize_session=False)
    if closed != 1:
        raise RuntimeError('Order changed during refund; transaction rolled back.')
    buyer = db.query(User).filter(User.telegram_id == order.telegram_id).with_for_update().first()
    if not buyer:
        raise RuntimeError('Refund wallet unavailable; transaction rolled back.')
    before_balance = Decimal(str(buyer.balance or 0))
    db.query(User).filter(User.telegram_id == order.telegram_id).update(
        {User.balance: func.coalesce(User.balance, 0) + amount}, synchronize_session=False)
    db.refresh(buyer)
    from services.wallet_ledger import record_wallet
    record_wallet(db,buyer,f"Refund · order #{order.id}",before_balance)
    commission_reversed = None
    if buyer.referred_by:
        from services.referral_commission import reverse_saved_commission
        commission_reversed = reverse_saved_commission(
            db, order, buyer.referred_by, amount, admin_id,
            invoice_snapshot=request.invoice_snapshot,
        )
    request.status, request.admin_id = 'completed', admin_id
    request.resolution = f'Approved refund: ${amount:.8f} USDT credited to your bot wallet.'
    order.refunded, order.status, order.refund_amount = True, 'refunded', amount
    # Used delivery credentials must never return to sellable inventory.
    record_event(db, order.id, 'refunded', request.resolution, actor_id=admin_id)
    commission_note = ''
    if commission_reversed and commission_reversed.get('status') == 'legacy_unrecorded':
        commission_note = ' Legacy referral commission was not reversed because its historical amount was not recorded; see audit log.'
    db.add(AdminAuditLog(admin_telegram_id=admin_id, action='policy_refund', entity_type='order',
                        entity_id=str(order.id), summary=f'Approved ${amount:.8f}; {request.used_days} days used; inventory unchanged.{commission_note}'))
    enqueue(db, f'warranty:{order.id}:refund', order.telegram_id,
            f'💸 <b>Order #{order.id} · Refund approved</b>\n{request.resolution}')
    return None


def correct_metadata(order_id, admin_id, days, delivered_at, note):
    from config import ADMIN_IDS
    from database import transaction
    from models.order import Order
    from models.bot_operations import WarrantyRequest
    from models.admin_audit_log import AdminAuditLog
    from services.order_workflow import record_event
    from services.claim_details import metadata_invoice
    from types import SimpleNamespace
    if admin_id not in ADMIN_IDS:
        return 'Access denied.'
    if not isinstance(days,int) or not 1 <= days <= 3650 or not isinstance(note,str) or not 5 <= len(note.strip()) <= 500:
        return 'Enter 1–3650 days and a short explanation for the correction.'
    try:
        from datetime import timezone
        started = datetime.fromisoformat(delivered_at.replace('Z','+00:00'))
        if started.tzinfo:
            started = started.astimezone(timezone.utc).replace(tzinfo=None)
    except (AttributeError,TypeError,ValueError):
        return 'Use a valid delivery date/time in UTC.'
    with transaction() as db:
        order = db.query(Order).filter(Order.id == order_id).with_for_update().first()
        if not order or order.refunded or order.status != 'completed' or policy(order) == 'skip':
            return 'Only completed, non-refunded orders with a saved policy can be corrected.'
        request = db.query(WarrantyRequest).filter_by(order_id=order_id).with_for_update().first()
        if request and request.status != 'submitted':
            return 'This claim has already been reviewed.'
        if not order.created_at <= started <= (request.created_at if request else datetime.utcnow()):
            return 'Delivery must be between purchase and claim submission (or now).'
        mode = policy(order)
        quote = refund_quote(SimpleNamespace(aftercare_policy=mode,subscription_days=days,
                    coverage_started_at=started,amount=order.amount),request.created_at) if request and mode == 'refund' else None
        if request and mode == 'refund' and (not quote or quote['refund_amount'] <= 0):
            return 'No refundable amount remains at the saved submission date. Decline the claim with a reason.'
        order.coverage_started_at = started
        if mode == 'refund':
            order.subscription_days = days
            order.warranty_days,order.warranty_expires_at = 0,None
        else:
            order.subscription_days = 0
            order.warranty_days, order.warranty_expires_at = days, started+timedelta(days=days)
        if request:
            request.invoice_snapshot = metadata_invoice(request.invoice_snapshot,order)
            if quote:
                request.refund_amount,request.used_days,request.remaining_days = quote['refund_amount'],quote['used_days'],quote['remaining_days']
        record_event(db,order_id,'aftercare_metadata_confirmed','Team confirmed the subscription and delivery date.',actor_id=admin_id)
        db.add(AdminAuditLog(admin_telegram_id=admin_id,action='aftercare_metadata',entity_type='order',
                            entity_id=str(order_id),summary=f'{days} days; delivered {started.isoformat()} UTC; {note.strip()}'[:500]))
    return None


def delivery_details(order_id, user_id, admin=False):
    from config import ADMIN_IDS
    from database import SessionLocal
    from models.order import Order
    from models.bot_operations import WarrantyRequest
    if admin and user_id not in ADMIN_IDS:
        return None
    with SessionLocal() as db:
        order = db.get(Order,order_id)
        if not order or (not admin and order.telegram_id != user_id):
            return None
        claim = db.query(WarrantyRequest).filter_by(order_id=order_id).first()
        return (claim.delivery_snapshot if claim else None) or order.delivered_account or 'No delivery content recorded.'


def cache_proof(order_id,user_id,data,admin=False):
    from config import ADMIN_IDS
    from database import transaction
    from models.bot_operations import WarrantyRequest
    from services.claim_details import proof_mime
    if admin and user_id not in ADMIN_IDS:
        return False
    mime = proof_mime(data)
    with transaction() as db:
        claim = db.query(WarrantyRequest).filter_by(order_id=order_id).with_for_update().first()
        if not claim or (not admin and claim.telegram_id != user_id):
            return False
        if not claim.proof_mime:
            claim.proof_image,claim.proof_mime = data,mime
    return True


def pending(page=0):
    from database import SessionLocal
    from models.bot_operations import WarrantyRequest as Request
    from models.order import Order
    with SessionLocal() as db:
        query = db.query(Request).join(Order, Order.id == Request.order_id).filter(
            Request.status.in_(('submitted', 'needs_info', 'approved')), Order.status == 'completed', Order.refunded == False)
        count = query.count()
        rows = query.order_by(Request.created_at.asc()).offset(max(0,page)*8).limit(8).all()
        return count, [dict(order_id=r.order_id, status=r.status, request_type=r.request_type) for r in rows]
