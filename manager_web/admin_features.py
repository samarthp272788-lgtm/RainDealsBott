"""Additional authenticated web controls mirroring the Telegram admin menu."""

from __future__ import annotations

import asyncio
import hashlib
import secrets
import string
from datetime import datetime
from decimal import Decimal, InvalidOperation
from html import escape

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, or_

import manager_web.app as core
from database import SessionLocal
from models.api_key import ApiKey
from models.custom_rate import CustomRate
from models.deposit import Deposit
from models.manager_auth import ManagerAuthSession
from models.order import Order
from models.product import Product
from models.promocode import PromoCode
from models.rate_control import ProductRateControl, RateControlAssignment
from models.ticket import Ticket
from models.user import User


router = APIRouter()
_broadcast_jobs: dict[str, dict] = {}


def install_nav() -> None:
    """Extend the shared nav without changing existing manager pages."""
    original = core._management_nav

    def nav(active: str) -> str:
        links = (
            ("orders", "/", "Orders"),
            ("order_admin", "/admin/orders", "Order actions"),
            ("products", "/products", "Products"),
            ("inventory", "/admin/inventory", "Stock"),
            ("users", "/admin/users", "Users"),
            ("deposits", "/deposits", "Deposits"),
            ("statistics", "/admin/statistics", "Statistics"),
            ("tickets", "/admin/tickets", "Tickets"),
            ("promocodes", "/admin/promocodes", "Promocodes"),
            ("broadcast", "/admin/broadcast", "Broadcast"),
            ("rates", "/admin/rates", "Custom rates"),
            ("api_keys", "/admin/api-keys", "API keys"),
            ("providers", "/providers", "Providers"),
            ("system", "/system", "Maintenance"),
        )
        return '<nav class="tabs">' + "".join(
            f'<a class="{"active" if active == key else ""}" href="{path}">{label}</a>'
            for key, path, label in links
        ) + "</nav>"

    core._management_nav = nav


def _auth(request: Request):
    return core._require_manager(request)


def _page(request: Request, title: str, body: str, section: str):
    return HTMLResponse(core._manager_page(
        title, body, core._csrf_token(request), section,
        getattr(request.state, "manager_expires_unix", None),
    ))


def _form(request: Request) -> dict[str, str]:
    return core._parse_form(request.state.form_body)


async def _read_form(request: Request) -> dict[str, str]:
    body = await request.body()
    request.state.form_body = body
    return core._parse_form(body)


def _csrf(request: Request, form: dict[str, str]) -> None:
    core._verify_csrf(request, form.get("csrf", ""))


def _flash(text: str) -> str:
    return f'<div class="notice">{escape(text)}</div>' if text else ""


def _session_admin_id(request: Request) -> int:
    token = request.session.get("auth_token", "")
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    db = SessionLocal()
    try:
        row = db.query(ManagerAuthSession.telegram_id).filter(
            ManagerAuthSession.token_hash == token_hash,
            ManagerAuthSession.revoked_at.is_(None),
        ).first()
        if not row:
            raise HTTPException(status_code=401, detail="Admin verification expired.")
        return int(row[0])
    finally:
        db.close()


@router.get("/admin/statistics", response_class=HTMLResponse)
async def statistics_page(request: Request):
    if response := _auth(request):
        return response
    db = SessionLocal()
    try:
        users = db.query(func.count(User.id)).scalar() or 0
        products = db.query(func.count(Product.id)).scalar() or 0
        active_products = db.query(func.count(Product.id)).filter(Product.is_active.is_(True)).scalar() or 0
        orders = db.query(func.count(Order.id)).scalar() or 0
        pending = db.query(func.count(Order.id)).filter(Order.status.in_(core.GENERAL_PENDING_STATUSES)).scalar() or 0
        completed = db.query(func.count(Order.id)).filter(Order.status == "completed").scalar() or 0
        refunded = db.query(func.count(Order.id)).filter(Order.refunded.is_(True)).scalar() or 0
        revenue = db.query(func.coalesce(func.sum(Order.amount), 0)).filter(
            Order.status == "completed", Order.refunded.is_(False)
        ).scalar() or 0
        deposits = db.query(func.count(Deposit.id)).scalar() or 0
        tickets = db.query(func.count(Ticket.id)).scalar() or 0
        open_tickets = db.query(func.count(Ticket.id)).filter(Ticket.status == "Open").scalar() or 0
    finally:
        db.close()
    entries = (
        ("Customers", users), ("Products", f"{active_products} active / {products} total"),
        ("Orders", orders), ("Pending orders", pending), ("Completed orders", completed),
        ("Refunded orders", refunded), ("Completed order value", f"${core._money(revenue):.2f}"),
        ("Deposits", deposits), ("Support tickets", f"{open_tickets} open / {tickets} total"),
    )
    cards = "".join(f'<article class="card"><small>{label}</small><h2>{value}</h2></article>' for label, value in entries)
    body = f'<section class="grid">{cards}</section><p class="sub">Totals are read from the shared store database. Completed order value excludes refunded orders.</p>'
    return _page(request, "Statistics", body, "statistics")


@router.get("/admin/promocodes", response_class=HTMLResponse)
async def promocodes_page(request: Request):
    if response := _auth(request):
        return response
    csrf = core._csrf_token(request)
    db = SessionLocal()
    try:
        promos = db.query(PromoCode).order_by(PromoCode.created_at.desc()).limit(200).all()
    finally:
        db.close()
    rows = "".join(
        f'''<tr><td><code>{escape(p.code)}</code></td><td>${core._money(p.amount):.2f}</td>
        <td>{p.used_count}/{"∞" if not p.max_uses else p.max_uses}</td>
        <td>{"Active" if p.is_active and p.can_use() else "Inactive / exhausted"}</td><td>
        <form method="post" action="/admin/promocodes/{p.id}/toggle"><input type="hidden" name="csrf" value="{escape(csrf)}">
        <button>{"Deactivate" if p.is_active else "Reactivate"}</button></form></td></tr>'''
        for p in promos
    )
    notice = _flash(request.query_params.get("notice", ""))
    body = f'''{notice}<section class="panel"><h2>Create a promocode</h2><form method="post" action="/admin/promocodes/create">
    <input type="hidden" name="csrf" value="{escape(csrf)}"><div class="grid">
    <label>Code (leave blank to generate)<input name="code" maxlength="32" pattern="[A-Za-z0-9_-]*"></label>
    <label>Wallet credit amount (USDT)<input name="amount" type="number" min="0.00000001" step="0.00000001" required></label>
    <label>Maximum uses (0 = unlimited)<input name="max_uses" type="number" min="0" step="1" value="1" required></label>
    </div><button class="primary">Create code</button></form></section>
    <section class="panel"><h2>Promocodes ({len(promos)} latest)</h2><div style="overflow:auto"><table class="table"><thead><tr><th>Code</th><th>Credit</th><th>Uses</th><th>Status</th><th>Action</th></tr></thead><tbody>{rows or '<tr><td colspan="5">No promocodes yet.</td></tr>'}</tbody></table></div></section>'''
    return _page(request, "Promocodes", body, "promocodes")


@router.post("/admin/promocodes/create")
async def promocode_create(request: Request):
    if response := _auth(request):
        return response
    data = await _read_form(request)
    _csrf(request, data)
    try:
        amount = Decimal(data.get("amount", ""))
        max_uses = int(data.get("max_uses", "1"))
        code = data.get("code", "").strip().upper() or "".join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(8))
        if not amount.is_finite() or amount <= 0 or max_uses < 0 or not code or len(code) > 32:
            raise ValueError
        if not all(ch.isalnum() or ch in "_-" for ch in code):
            raise ValueError
    except (InvalidOperation, ValueError):
        return RedirectResponse("/admin/promocodes?notice=Invalid+promocode+fields", status_code=303)
    db = SessionLocal()
    try:
        if db.query(PromoCode.id).filter(PromoCode.code == code).first():
            return RedirectResponse("/admin/promocodes?notice=That+code+already+exists", status_code=303)
        db.add(PromoCode(code=code, amount=float(amount), max_uses=max_uses, created_by=_session_admin_id(request)))
        db.commit()
    except Exception:
        db.rollback()
        core.logger.exception("Manager promocode creation failed")
        raise HTTPException(status_code=500, detail="Could not create promocode.")
    finally:
        db.close()
    return RedirectResponse("/admin/promocodes?notice=Promocode+created", status_code=303)


@router.post("/admin/promocodes/{promo_id}/toggle")
async def promocode_toggle(request: Request, promo_id: int):
    if response := _auth(request):
        return response
    data = await _read_form(request)
    _csrf(request, data)
    db = SessionLocal()
    try:
        promo = db.query(PromoCode).filter(PromoCode.id == promo_id).with_for_update().first()
        if not promo:
            raise HTTPException(status_code=404, detail="Promocode not found.")
        promo.is_active = not promo.is_active
        db.commit()
    finally:
        db.close()
    return RedirectResponse("/admin/promocodes", status_code=303)


@router.get("/admin/rates", response_class=HTMLResponse)
async def rates_page(request: Request):
    if response := _auth(request):
        return response
    csrf = core._csrf_token(request)
    db = SessionLocal()
    try:
        users = db.query(User.telegram_id, User.username, User.full_name).order_by(User.id.desc()).limit(0).all()
        products = db.query(Product.id, Product.name).order_by(Product.name.asc()).limit(500).all()
        rates = db.query(CustomRate).filter(CustomRate.is_active.is_(True)).order_by(CustomRate.created_at.desc()).limit(200).all()
        controls = db.query(ProductRateControl, Product.name).join(Product, Product.id == ProductRateControl.product_id).order_by(Product.name.asc()).all()
        assignments = db.query(RateControlAssignment).filter(RateControlAssignment.is_active.is_(True)).order_by(RateControlAssignment.created_at.desc()).limit(200).all()
    finally:
        db.close()
    product_options = '<option value="">All products</option>' + "".join(f'<option value="{pid}">{escape(name)}</option>' for pid, name in products)
    rate_rows = "".join(
        f'''<tr><td>{r.telegram_id}</td><td>{"All products" if r.product_id is None else "Product #" + str(r.product_id)}</td>
        <td>${core._money(r.price):.8f}</td><td>{r.created_at or "—"}</td><td><form method="post" action="/admin/rates/{r.id}/stop"><input type="hidden" name="csrf" value="{escape(csrf)}"><button class="danger">Stop</button></form></td></tr>'''
        for r in rates
    )
    control_rows = "".join(
        f'''<tr><td>#{pid} · {escape(name)}</td><td>${core._money(control.price):.8f}</td><td>
        <form method="post" action="/admin/rates/control/{control.id}/toggle"><input type="hidden" name="csrf" value="{escape(csrf)}"><button>{"Disable" if control.is_active else "Enable"}</button></form></td></tr>'''
        for control, name in controls for pid in (control.product_id,)
    )
    assignment_rows = "".join(
        f'''<tr><td>{a.telegram_id}</td><td>Active</td><td><form method="post" action="/admin/rates/assignment/{a.id}/stop"><input type="hidden" name="csrf" value="{escape(csrf)}"><button class="danger">Stop</button></form></td></tr>'''
        for a in assignments
    )
    body = f'''<section class="panel"><h2>Set a user custom rate</h2><form method="post" action="/admin/rates/create"><input type="hidden" name="csrf" value="{escape(csrf)}"><div class="grid">
    <label>Telegram user ID<input name="telegram_id" type="number" min="1" required></label><label>Product scope<select name="product_id">{product_options}</select></label>
    <label>Fixed price per unit (USDT)<input name="price" type="number" min="0" step="0.00000001" required></label></div><button class="primary">Apply custom rate</button></form>
    <p class="sub">Choose “All products” for one fixed price across the catalog. Choose a product for a product-specific rate.</p></section>
    <section class="panel"><h2>Rate Control prices</h2><p class="sub">Saved per-product prices can be assigned to a user using the form below.</p>
    <form method="post" action="/admin/rates/control/save"><input type="hidden" name="csrf" value="{escape(csrf)}"><div class="grid"><label>Product<select name="product_id" required>{product_options.replace('<option value="">All products</option>', '')}</select></label><label>Rate (USDT)<input name="price" type="number" min="0" step="0.00000001" required></label></div><button>Save product rate</button></form>
    <form method="post" action="/admin/rates/assignment/create"><input type="hidden" name="csrf" value="{escape(csrf)}"><label>Assign all active Rate Control prices to Telegram user ID<input name="telegram_id" type="number" min="1" required></label><button class="primary">Assign Rate Control</button></form>
    <div style="overflow:auto"><table class="table"><thead><tr><th>Product</th><th>Price</th><th>Action</th></tr></thead><tbody>{control_rows or '<tr><td colspan="3">No saved product rates.</td></tr>'}</tbody></table></div></section>
    <section class="panel"><h2>Active custom rates</h2><div style="overflow:auto"><table class="table"><thead><tr><th>User</th><th>Scope</th><th>Price</th><th>Created</th><th></th></tr></thead><tbody>{rate_rows or '<tr><td colspan="5">No active custom rates.</td></tr>'}</tbody></table></div></section>
    <section class="panel"><h2>Rate Control assignments</h2><div style="overflow:auto"><table class="table"><thead><tr><th>User</th><th>Status</th><th></th></tr></thead><tbody>{assignment_rows or '<tr><td colspan="3">No active assignments.</td></tr>'}</tbody></table></div></section>'''
    return _page(request, "Custom rates", body, "rates")


@router.post("/admin/rates/create")
async def rate_create(request: Request):
    if response := _auth(request):
        return response
    data = await _read_form(request)
    _csrf(request, data)
    try:
        telegram_id = int(data.get("telegram_id", ""))
        product_id = int(data["product_id"]) if data.get("product_id") else None
        price = Decimal(data.get("price", ""))
        if telegram_id <= 0 or not price.is_finite() or price < 0:
            raise ValueError
    except (ValueError, InvalidOperation):
        return RedirectResponse("/admin/rates", status_code=303)
    db = SessionLocal()
    try:
        if not db.query(User.id).filter(User.telegram_id == telegram_id).first():
            raise HTTPException(status_code=404, detail="User not found.")
        if product_id is not None and not db.query(Product.id).filter(Product.id == product_id).first():
            raise HTTPException(status_code=404, detail="Product not found.")
        if product_id is None:
            db.query(CustomRate).filter(CustomRate.telegram_id == telegram_id, CustomRate.product_id.is_(None), CustomRate.is_active.is_(True)).update({CustomRate.is_active: False, CustomRate.stopped_at: datetime.utcnow()}, synchronize_session=False)
            db.query(RateControlAssignment).filter(RateControlAssignment.telegram_id == telegram_id, RateControlAssignment.is_active.is_(True)).update({RateControlAssignment.is_active: False, RateControlAssignment.stopped_at: datetime.utcnow()}, synchronize_session=False)
        else:
            db.query(CustomRate).filter(CustomRate.telegram_id == telegram_id, CustomRate.product_id == product_id, CustomRate.is_active.is_(True)).update({CustomRate.is_active: False, CustomRate.stopped_at: datetime.utcnow()}, synchronize_session=False)
        db.add(CustomRate(telegram_id=telegram_id, product_id=product_id, price=price, is_active=True))
        db.commit()
    finally:
        db.close()
    return RedirectResponse("/admin/rates", status_code=303)


@router.post("/admin/rates/{rate_id}/stop")
async def rate_stop(request: Request, rate_id: int):
    if response := _auth(request):
        return response
    data = await _read_form(request)
    _csrf(request, data)
    db = SessionLocal()
    try:
        rate = db.query(CustomRate).filter(CustomRate.id == rate_id).with_for_update().first()
        if rate:
            rate.is_active = False
            rate.stopped_at = datetime.utcnow()
            db.commit()
    finally:
        db.close()
    return RedirectResponse("/admin/rates", status_code=303)


@router.post("/admin/rates/control/save")
async def rate_control_save(request: Request):
    if response := _auth(request):
        return response
    data = await _read_form(request)
    _csrf(request, data)
    try:
        product_id, price = int(data.get("product_id", "")), Decimal(data.get("price", ""))
        if product_id <= 0 or not price.is_finite() or price < 0:
            raise ValueError
    except (ValueError, InvalidOperation):
        return RedirectResponse("/admin/rates", status_code=303)
    db = SessionLocal()
    try:
        control = db.query(ProductRateControl).filter(ProductRateControl.product_id == product_id).with_for_update().first()
        if control:
            control.price, control.is_active = price, True
        else:
            db.add(ProductRateControl(product_id=product_id, price=price, is_active=True))
        db.commit()
    finally:
        db.close()
    return RedirectResponse("/admin/rates", status_code=303)


@router.post("/admin/rates/control/{control_id}/toggle")
async def rate_control_toggle(request: Request, control_id: int):
    if response := _auth(request):
        return response
    data = await _read_form(request)
    _csrf(request, data)
    db = SessionLocal()
    try:
        control = db.query(ProductRateControl).filter(ProductRateControl.id == control_id).with_for_update().first()
        if control:
            control.is_active = not control.is_active
            db.commit()
    finally:
        db.close()
    return RedirectResponse("/admin/rates", status_code=303)


@router.post("/admin/rates/assignment/create")
async def rate_assignment_create(request: Request):
    if response := _auth(request):
        return response
    data = await _read_form(request)
    _csrf(request, data)
    try:
        telegram_id = int(data.get("telegram_id", ""))
        if telegram_id <= 0:
            raise ValueError
    except ValueError:
        return RedirectResponse("/admin/rates", status_code=303)
    db = SessionLocal()
    try:
        if not db.query(User.id).filter(User.telegram_id == telegram_id).first():
            raise HTTPException(status_code=404, detail="User not found.")
        if not db.query(ProductRateControl.id).filter(ProductRateControl.is_active.is_(True)).first():
            return RedirectResponse("/admin/rates", status_code=303)
        db.query(RateControlAssignment).filter(RateControlAssignment.telegram_id == telegram_id, RateControlAssignment.is_active.is_(True)).update({RateControlAssignment.is_active: False, RateControlAssignment.stopped_at: datetime.utcnow()}, synchronize_session=False)
        db.query(CustomRate).filter(CustomRate.telegram_id == telegram_id, CustomRate.product_id.is_(None), CustomRate.is_active.is_(True)).update({CustomRate.is_active: False, CustomRate.stopped_at: datetime.utcnow()}, synchronize_session=False)
        db.add(RateControlAssignment(telegram_id=telegram_id, is_active=True))
        db.commit()
    finally:
        db.close()
    return RedirectResponse("/admin/rates", status_code=303)


@router.post("/admin/rates/assignment/{assignment_id}/stop")
async def rate_assignment_stop(request: Request, assignment_id: int):
    if response := _auth(request):
        return response
    data = await _read_form(request)
    _csrf(request, data)
    db = SessionLocal()
    try:
        assignment = db.query(RateControlAssignment).filter(RateControlAssignment.id == assignment_id).with_for_update().first()
        if assignment:
            assignment.is_active = False
            assignment.stopped_at = datetime.utcnow()
            db.commit()
    finally:
        db.close()
    return RedirectResponse("/admin/rates", status_code=303)


@router.get("/admin/api-keys", response_class=HTMLResponse)
async def api_keys_page(request: Request, q: str = ""):
    if response := _auth(request):
        return response
    csrf, search = core._csrf_token(request), q.strip()[:100]
    db = SessionLocal()
    try:
        query = db.query(ApiKey)
        if search:
            if search.isdigit():
                query = query.filter(ApiKey.telegram_id == int(search))
            else:
                query = query.filter(ApiKey.key_prefix.ilike(f"%{search}%"))
        keys = query.order_by(ApiKey.id.desc()).limit(200).all()
    finally:
        db.close()
    rows = "".join(
        f'''<tr><td>{key.telegram_id}</td><td><code>{escape(key.key_prefix)}…</code></td><td>{"Active" if key.is_active else "Revoked"}</td>
        <td>{key.created_at or "—"}</td><td>{key.last_used_at or "Never"}</td><td>{"" if not key.is_active else f'<form method="post" action="/admin/api-keys/{key.id}/revoke"><input type="hidden" name="csrf" value="{escape(csrf)}"><button class="danger">Revoke</button></form>'}</td></tr>'''
        for key in keys
    )
    body = f'''<section class="panel"><h2>Reseller API keys</h2><p class="sub">Only prefixes are shown. Secret keys are never displayed or regenerated here; users generate their key through Telegram. Revocation takes effect immediately.</p>
    <form class="search" method="get"><input name="q" value="{escape(search)}" placeholder="Telegram ID or key prefix"><button>Search</button></form>
    <div style="overflow:auto"><table class="table"><thead><tr><th>Owner</th><th>Key</th><th>Status</th><th>Created</th><th>Last used</th><th></th></tr></thead><tbody>{rows or '<tr><td colspan="6">No API keys found.</td></tr>'}</tbody></table></div></section>'''
    return _page(request, "API key control", body, "api_keys")


@router.post("/admin/api-keys/{key_id}/revoke")
async def api_key_revoke(request: Request, key_id: int):
    if response := _auth(request):
        return response
    data = await _read_form(request)
    _csrf(request, data)
    db = SessionLocal()
    try:
        key = db.query(ApiKey).filter(ApiKey.id == key_id).with_for_update().first()
        if key and key.is_active:
            key.is_active = False
            key.revoked_at = datetime.utcnow()
            db.commit()
    finally:
        db.close()
    return RedirectResponse("/admin/api-keys", status_code=303)


@router.get("/admin/inventory", response_class=HTMLResponse)
async def inventory_page(request: Request):
    if response := _auth(request):
        return response
    csrf = core._csrf_token(request)
    db = SessionLocal()
    try:
        products = db.query(Product).filter(Product.source == "own").order_by(Product.name.asc()).limit(500).all()
    finally:
        db.close()
    options = "".join(f'<option value="{p.id}">#{p.id} · {escape(p.name)} · stock {p.stock}</option>' for p in products)
    manual_options = "".join(f'<option value="{p.id}">#{p.id} · {escape(p.name)} · stock {p.stock}</option>' for p in products if p.delivery_type == "manual")
    rows = "".join(f'<tr><td>#{p.id} · {escape(p.name)}</td><td>{p.stock}</td><td>{p.low_stock_threshold}</td><td><a href="/products?edit={p.id}">Edit product</a></td></tr>' for p in products)
    body = f'''<section class="panel"><h2>Add inventory without replacing existing stock</h2><form method="post" action="/admin/inventory/add"><input type="hidden" name="csrf" value="{escape(csrf)}">
    <label>Own-inventory product<select name="product_id" required>{options}</select></label><label>New account/code lines<textarea name="inventory" required maxlength="50000" placeholder="One account or key per line"></textarea></label><button class="primary">Append inventory</button></form>
    <p class="sub">Reseller inventory stays provider-managed. This appends non-empty lines, recalculates stock, and never overwrites existing items.</p></section>
    <section class="panel"><h2>Increase manual-fulfillment quantity</h2><form method="post" action="/admin/inventory/increase"><input type="hidden" name="csrf" value="{escape(csrf)}">
    <label>Manual-fulfillment product<select name="product_id" required>{manual_options}</select></label><label>Units to add<input name="quantity" type="number" min="1" max="100000" step="1" required></label><button>Increase quantity</button></form>
    <p class="sub">This count-only action is accepted only for products configured for manual fulfillment. Use the inventory-line form for account or key delivery.</p></section>
    <section class="panel"><h2>Own inventory ({len(products)} products)</h2><div style="overflow:auto"><table class="table"><thead><tr><th>Product</th><th>Quantity</th><th>Low-stock threshold</th><th></th></tr></thead><tbody>{rows or '<tr><td colspan="4">No own-inventory products.</td></tr>'}</tbody></table></div></section>'''
    return _page(request, "Inventory control", body, "inventory")


@router.post("/admin/inventory/add")
async def inventory_add(request: Request):
    if response := _auth(request):
        return response
    data = await _read_form(request)
    _csrf(request, data)
    try:
        product_id = int(data.get("product_id", ""))
        additions = [line.strip() for line in data.get("inventory", "").splitlines() if line.strip()]
        if product_id <= 0 or not additions or len(additions) > 5000:
            raise ValueError
    except ValueError:
        return RedirectResponse("/admin/inventory", status_code=303)
    db = SessionLocal()
    try:
        product = db.query(Product).filter(Product.id == product_id).with_for_update().first()
        if not product or product.source != "own":
            raise HTTPException(status_code=400, detail="Choose an own-inventory product.")
        existing = [line.strip() for line in (product.file_content or "").splitlines() if line.strip()]
        product.file_content = "\n".join(existing + additions)
        product.stock = len(existing) + len(additions)
        db.commit()
    finally:
        db.close()
    return RedirectResponse("/admin/inventory", status_code=303)


@router.post("/admin/inventory/increase")
async def inventory_increase(request: Request):
    if response := _auth(request):
        return response
    data = await _read_form(request)
    _csrf(request, data)
    try:
        product_id, quantity = int(data.get("product_id", "")), int(data.get("quantity", ""))
        if product_id <= 0 or not 1 <= quantity <= 100000:
            raise ValueError
    except ValueError:
        return RedirectResponse("/admin/inventory", status_code=303)
    db = SessionLocal()
    try:
        product = db.query(Product).filter(Product.id == product_id).with_for_update().first()
        if not product or product.source != "own" or product.delivery_type != "manual":
            raise HTTPException(status_code=400, detail="Quantity-only increases are limited to own manual-fulfillment products.")
        product.stock = (product.stock or 0) + quantity
        db.commit()
    finally:
        db.close()
    return RedirectResponse("/admin/inventory", status_code=303)


@router.post("/users/{telegram_id}/balance")
async def balance_update(request: Request, telegram_id: int):
    if response := _auth(request):
        return response
    data = await _read_form(request)
    _csrf(request, data)
    try:
        amount = Decimal(data.get("amount", ""))
        mode = data.get("mode")
        if not amount.is_finite() or abs(amount.as_tuple().exponent) > 8 or mode not in {"set", "adjust"}:
            raise ValueError
    except (InvalidOperation, ValueError):
        raise HTTPException(status_code=400, detail="Enter a valid amount with at most 8 decimals.")
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.telegram_id == telegram_id).with_for_update().first()
        if not user:
            raise HTTPException(status_code=404, detail="User not found.")
        new_balance = amount if mode == "set" else Decimal(str(user.balance)) + amount
        if new_balance < 0:
            raise HTTPException(status_code=400, detail="Balance cannot be negative.")
        user.balance = new_balance
        db.commit()
    finally:
        db.close()
    return RedirectResponse(f"/users/{telegram_id}", status_code=303)


@router.get("/admin/users", response_class=HTMLResponse)
async def admin_users_page(request: Request, q: str = ""):
    if response := _auth(request):
        return response
    csrf, search = core._csrf_token(request), q.strip()[:100]
    db = SessionLocal()
    try:
        query = db.query(User)
        if search:
            if search.isdigit():
                query = query.filter(User.telegram_id == int(search))
            else:
                query = query.filter(or_(User.username.ilike(f"%{search}%"), User.full_name.ilike(f"%{search}%")))
        users = query.order_by(User.id.desc()).limit(100).all()
    finally:
        db.close()
    rows = "".join(
        f'''<tr><td><a href="/users/{u.telegram_id}">{u.telegram_id}</a><br>{escape(u.full_name)} (@{escape(u.username or "—")})</td>
        <td>${core._money(u.balance):.8f}</td><td>{"Banned" if u.is_banned else "Active"}</td><td>
        <form method="post" action="/users/{u.telegram_id}/balance"><input type="hidden" name="csrf" value="{escape(csrf)}"><input type="hidden" name="mode" value="adjust">
        <input name="amount" type="number" step="0.00000001" placeholder="± amount" required><button>Adjust</button></form>
        <form method="post" action="/users/{u.telegram_id}/balance"><input type="hidden" name="csrf" value="{escape(csrf)}"><input type="hidden" name="mode" value="set">
        <input name="amount" type="number" min="0" step="0.00000001" placeholder="New balance" required><button>Set</button></form>
        <form method="post" action="/users/{u.telegram_id}/ban"><input type="hidden" name="csrf" value="{escape(csrf)}"><button class="{'danger' if not u.is_banned else ''}">{'Unban' if u.is_banned else 'Ban'}</button></form></td></tr>'''
        for u in users
    )
    body = f'''<section class="panel"><h2>Customer account controls</h2><p class="sub">Balance edits change wallet funds directly. Record any financial adjustment in your business ledger.</p>
    <form class="search" method="get"><input name="q" value="{escape(search)}" placeholder="Telegram ID, username, or name"><button>Search</button></form>
    <div style="overflow:auto"><table class="table"><thead><tr><th>User</th><th>Balance</th><th>Status</th><th>Balance and access actions</th></tr></thead><tbody>{rows or '<tr><td colspan="4">No users found.</td></tr>'}</tbody></table></div></section>'''
    return _page(request, "Users", body, "users")


@router.get("/admin/orders", response_class=HTMLResponse)
async def order_admin_page(request: Request, q: str = ""):
    if response := _auth(request):
        return response
    csrf, search = core._csrf_token(request), q.strip()[:100]
    db = SessionLocal()
    try:
        query = db.query(Order)
        if search:
            if search.isdigit():
                query = query.filter(or_(Order.id == int(search), Order.telegram_id == int(search)))
            else:
                query = query.filter(Order.product_name.ilike(f"%{search}%"))
        orders = query.order_by(Order.created_at.desc(), Order.id.desc()).limit(100).all()
    finally:
        db.close()
    cards = "".join(
        f'''<article class="card"><div class="row"><div><strong>Order #{o.id} · {escape(o.product_name)}</strong><br><small>User {o.telegram_id} · ${core._money(o.amount):.2f} · qty {o.quantity} · {escape(o.status)}</small></div></div>
        <div class="row"><form method="post" action="/admin/orders/{o.id}/refund" onsubmit="return confirm('Refund this order and restore eligible stock?')"><input type="hidden" name="csrf" value="{escape(csrf)}"><button class="danger" {'disabled' if o.refunded or o.status == 'deleted' else ''}>Refund</button></form>
        <form method="post" action="/admin/orders/{o.id}/soft-delete" onsubmit="return confirm('Hide this order from the regular order list? Its record is retained.')"><input type="hidden" name="csrf" value="{escape(csrf)}"><button {'disabled' if o.status == 'deleted' else ''}>Soft-delete</button></form></div></article>'''
        for o in orders
    )
    body = f'''<section class="panel"><h2>Order actions</h2><p class="sub">Refund restores the buyer’s balance, reverses referral credit, and restores eligible stock in one locked transaction. Delete is soft-only and retains the audit row.</p>
    <form class="search" method="get"><input name="q" value="{escape(search)}" placeholder="Order ID, Telegram ID, or product"><button>Search</button></form></section>{cards or '<p class="sub">No orders found.</p>'}'''
    return _page(request, "Order actions", body, "order_admin")


@router.post("/admin/orders/{order_id}/refund")
async def order_refund(request: Request, order_id: int):
    if response := _auth(request):
        return response
    data = await _read_form(request)
    _csrf(request, data)
    from handlers.admin_orders import _do_refund

    result = await asyncio.to_thread(_do_refund, order_id, _session_admin_id(request))
    if result.get("error"):
        return RedirectResponse(f"/admin/orders?notice={core.quote(result['error'])}", status_code=303)
    buyer_id, amount = result["buyer_id"], result["amount"]
    try:
        from services.notification_outbox import queue_message
        await queue_message(request.app.state.main_bot, f"manager-refund:{order_id}", buyer_id,
                            f"Your order was refunded. ${amount:.2f} returned to your balance.")
    except Exception:
        core.logger.exception("Refund notification failed for order %s", order_id)
    return RedirectResponse("/admin/orders?notice=Refund+completed", status_code=303)


@router.post("/admin/orders/{order_id}/soft-delete")
async def order_soft_delete(request: Request, order_id: int):
    if response := _auth(request):
        return response
    data = await _read_form(request)
    _csrf(request, data)
    db = SessionLocal()
    try:
        order = db.query(Order).filter(Order.id == order_id).with_for_update().first()
        if not order:
            raise HTTPException(status_code=404, detail="Order not found.")
        order.status = "deleted"
        db.commit()
    finally:
        db.close()
    return RedirectResponse("/admin/orders", status_code=303)


@router.get("/admin/broadcast", response_class=HTMLResponse)
async def broadcast_page(request: Request):
    if response := _auth(request):
        return response
    csrf = core._csrf_token(request)
    db = SessionLocal()
    try:
        recipients = db.query(func.count(User.id)).scalar() or 0
    finally:
        db.close()
    jobs = "".join(
        f'<article class="card"><strong>Broadcast {escape(job_id[:8])}</strong><br><small>{job["status"]} · {job["sent"]} sent · {job["failed"]} failed · {job["total"]} recipients</small><p>{escape(job["text"][:200])}</p></article>'
        for job_id, job in list(_broadcast_jobs.items())[-10:][::-1]
    )
    body = f'''<section class="panel"><h2>Text broadcast</h2><p class="sub">Targets {recipients} registered accounts. Sends plain text at a conservative rate. Telegram delivery is best-effort; blocked users are counted as failures.</p>
    <form method="post" action="/admin/broadcast/start"><input type="hidden" name="csrf" value="{escape(csrf)}"><label>Message (up to 4,096 characters)<textarea name="message" required maxlength="4096"></textarea></label>
    <label class="row" style="display:flex;align-items:center;gap:8px"><input type="checkbox" name="confirm" value="yes" required style="width:auto">I confirm sending this message to all registered users.</label><button class="danger">Start broadcast</button></form></section>
    <section><h2>Recent broadcasts</h2>{jobs or '<p class="sub">No broadcasts in this web process yet.</p>'}<p class="sub">Progress is kept in this web process; a restart interrupts an active broadcast. For very large lists use the Telegram bot’s broadcast or add a durable queue before production use.</p></section>'''
    return _page(request, "Broadcast", body, "broadcast")


async def _send_broadcast(job_id: str, bot, text: str, user_ids: list[int]):
    job = _broadcast_jobs[job_id]
    job.update(status="sending", total=len(user_ids))
    for user_id in user_ids:
        try:
            await bot.send_message(user_id, text, parse_mode=None, disable_web_page_preview=True)
            job["sent"] += 1
        except Exception as exc:
            job["failed"] += 1
            if "retry_after" in str(exc).lower():
                await asyncio.sleep(2)
        await asyncio.sleep(0.05)
    job["status"] = "complete"


@router.post("/admin/broadcast/start")
async def broadcast_start(request: Request):
    if response := _auth(request):
        return response
    data = await _read_form(request)
    _csrf(request, data)
    message = data.get("message", "").strip()
    if data.get("confirm") != "yes" or not message or len(message) > 4096:
        raise HTTPException(status_code=400, detail="Confirm the broadcast and enter a valid message.")
    db = SessionLocal()
    try:
        user_ids = [int(value[0]) for value in db.query(User.telegram_id).order_by(User.id.asc()).all()]
    finally:
        db.close()
    if not user_ids:
        raise HTTPException(status_code=409, detail="No users to broadcast to.")
    job_id = secrets.token_urlsafe(12)
    _broadcast_jobs[job_id] = {"status": "queued", "sent": 0, "failed": 0, "total": len(user_ids), "text": message}
    task = asyncio.create_task(_send_broadcast(job_id, request.app.state.main_bot, message, user_ids))
    request.app.state.admin_broadcast_tasks = getattr(request.app.state, "admin_broadcast_tasks", set())
    request.app.state.admin_broadcast_tasks.add(task)
    task.add_done_callback(request.app.state.admin_broadcast_tasks.discard)
    return RedirectResponse("/admin/broadcast", status_code=303)


@router.get("/tickets/{ticket_id}", response_class=HTMLResponse)
async def ticket_detail(request: Request, ticket_id: int):
    if response := _auth(request):
        return response
    csrf = core._csrf_token(request)
    db = SessionLocal()
    try:
        ticket = db.query(Ticket).filter(Ticket.id == ticket_id).first()
        if not ticket:
            body = '<section class="panel"><h2>Ticket not found</h2><a href="/tickets">Back to tickets</a></section>'
        else:
            user = db.get(User, ticket.user_id)
            tid = user.telegram_id if user else 0
            body = f'''<section class="panel"><h2>Ticket #{ticket.id}</h2><p>User <a href="/users/{tid}">{tid}</a> · {escape(ticket.category or "General")} · {escape(ticket.priority or "Normal")} · {escape(ticket.status)}</p>
            <p style="white-space:pre-wrap">{escape(ticket.message)}</p><p><strong>Previous response:</strong><br>{escape(ticket.admin_response or "None")}</p>
            <form method="post" action="/tickets/{ticket.id}/respond"><input type="hidden" name="csrf" value="{escape(csrf)}"><label>Reply<textarea name="response" required maxlength="4000"></textarea></label><button class="primary">Send reply and close ticket</button></form></section>'''
    finally:
        db.close()
    return _page(request, "Ticket details", body, "tickets")


@router.get("/admin/tickets", response_class=HTMLResponse)
async def admin_tickets_page(request: Request, status: str = "Open"):
    if response := _auth(request):
        return response
    selected = status if status in {"Open", "Closed"} else "Open"
    db = SessionLocal()
    try:
        rows = db.query(Ticket, User.telegram_id, User.full_name).join(
            User, User.id == Ticket.user_id
        ).filter(Ticket.status == selected).order_by(Ticket.id.desc()).limit(200).all()
    finally:
        db.close()
    cards = "".join(
        f'''<article class="card"><strong>Ticket #{ticket.id} · {escape(ticket.category or "General")}</strong>
        <p>User <a href="/users/{telegram_id}">{telegram_id}</a> · {escape(full_name)} · {escape(ticket.priority or "Normal")}</p>
        <p style="white-space:pre-wrap">{escape((ticket.message or "")[:1500])}</p>
        <a class="button" href="/tickets/{ticket.id}">View and reply</a></article>'''
        for ticket, telegram_id, full_name in rows
    )
    body = f'''<section class="panel"><h2>Support tickets</h2><div class="row"><a href="/admin/tickets?status=Open">Open</a> · <a href="/admin/tickets?status=Closed">Closed</a></div></section>
    {cards or '<p class="sub">No matching tickets.</p>'}'''
    return _page(request, "Tickets", body, "tickets")


@router.post("/tickets/{ticket_id}/respond")
async def ticket_respond(request: Request, ticket_id: int):
    if response := _auth(request):
        return response
    data = await _read_form(request)
    _csrf(request, data)
    response_text = data.get("response", "").strip()
    if not response_text or len(response_text) > 4000:
        raise HTTPException(status_code=400, detail="Reply must contain 1–4,000 characters.")
    db = SessionLocal()
    try:
        ticket = db.query(Ticket).filter(Ticket.id == ticket_id).with_for_update().first()
        if not ticket:
            raise HTTPException(status_code=404, detail="Ticket not found.")
        user = db.get(User, ticket.user_id)
        if not user:
            raise HTTPException(status_code=404, detail="Ticket owner not found.")
        telegram_id = user.telegram_id
        ticket.admin_response = response_text
        ticket.status = "Closed"
        db.commit()
    finally:
        db.close()
    try:
        from services.notification_outbox import queue_message
        response_key = hashlib.sha256(response_text.encode("utf-8")).hexdigest()[:20]
        await queue_message(request.app.state.main_bot, f"manager-ticket:{ticket_id}:{response_key}",
                            telegram_id, "Support replied to your ticket:\n\n" + response_text,
                            parse_mode=None)
    except Exception:
        core.logger.exception("Ticket reply notification failed for ticket %s", ticket_id)
    return RedirectResponse(f"/tickets?notice=Reply+saved", status_code=303)
