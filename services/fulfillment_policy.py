"""Pure delivery planning shared by browsing, checkout, and admin stock tools."""
from dataclasses import dataclass
import re


_EMAIL = re.compile(r"^[^\s@|:]+@[^\s@|:]+$")


def account_identity(value):
    """Return a conservative duplicate key without changing stored secrets.

    Email identifiers are case-insensitive; the credential/token portion stays
    case-sensitive. Other account formats are only trimmed and whitespace
    around a recognized delimiter is normalized, so distinct passwords/keys
    are not accidentally merged.
    """
    line = str(value or '').strip()
    if not line:
        return ''
    split_at = min((pos for pos in (line.find(':'), line.find('|'), line.find('\t')) if pos >= 0), default=-1)
    if split_at >= 0:
        identifier = line[:split_at].strip()
        # Preserve the secret portion exactly. Leading spaces can be part of a
        # password/token, so normalizing them could merge distinct accounts.
        secret = line[split_at + 1:]
        if _EMAIL.fullmatch(identifier):
            return f"email:{identifier.casefold()}\x1f{secret}"
        # Normalize only the identifier boundary; preserve case and secret.
        return f"field:{identifier}\x1f{secret}"
    return 'raw:' + line


def account_lines(value):
    """Return nonblank, first-occurrence inventory rows only.

    Legacy duplicate/blank rows must not inflate immediately deliverable
    stock. The first original row is retained byte-for-byte (apart from outer
    whitespace) because this function is also used to build delivery payloads.
    """
    rows = []
    seen = set()
    for raw in str(value or '').splitlines():
        line = raw.strip()
        if not line:
            continue
        identity = account_identity(line)
        if identity and identity not in seen:
            seen.add(identity)
            rows.append(line)
    return rows


def manual_allowed(product):
    return (product.delivery_type or 'automatic').lower() == 'manual' or bool(getattr(product, 'manual_fallback', False))


def automatic_inventory(product):
    """Return the units that can actually be delivered without staff work."""
    catalog_count = getattr(product, '_catalog_account_count', None)
    accounts = max(0, int(catalog_count)) if catalog_count is not None else len(
        account_lines(getattr(product, 'file_content', None))
    )
    catalog_reusable = getattr(product, '_catalog_reusable_content_present', None)
    reusable_content = (
        bool(catalog_reusable) if catalog_reusable is not None
        else bool(str(getattr(product, 'reusable_delivery', '') or '').strip())
    )
    reusable = max(0, int(getattr(product, 'reusable_stock', 0) or 0)) if reusable_content else 0
    return accounts + reusable


def effective_delivery_type(product):
    """Report manual delivery when fallback is enabled and auto inventory is empty."""
    mode = (getattr(product, 'delivery_type', None) or 'automatic').lower()
    source = (getattr(product, 'source', 'own') or 'own').lower()
    if source == 'own' and mode != 'manual' and manual_allowed(product) and automatic_inventory(product) == 0:
        return 'manual'
    return mode


def manual_fallback_only(product):
    """Whether orders currently route to staff because the automatic pool is empty."""
    mode = (getattr(product, 'delivery_type', None) or 'automatic').lower()
    return mode != 'manual' and effective_delivery_type(product) == 'manual'


def order_capacity(product):
    mode = (product.delivery_type or 'automatic').lower()
    stock = max(0, int(product.stock or 0))
    inventory = automatic_inventory(product)
    if mode == 'manual':
        return stock
    return max(stock, inventory) if manual_allowed(product) else inventory


def api_order_capacity(product):
    """Return stock that the reseller API can actually fulfill immediately.

    Telegram's manual fallback can make an automatic/hybrid product visible
    and queue a team-delivered order when its account pool is empty. That
    fallback capacity is not API inventory; API clients can only buy the
    account/reusable units currently available. Explicit manual products keep
    their configured stock capacity.
    """
    mode = (getattr(product, 'delivery_type', None) or 'automatic').lower()
    if mode in ('automatic', 'hybrid'):
        return automatic_inventory(product)
    return order_capacity(product)


@dataclass(frozen=True)
class DeliveryPlan:
    automatic: tuple[str, ...]
    remaining: tuple[str, ...]
    manual_quantity: int
    stock_after: int
    order_mode: str
    preorder: bool = False
    automatic_quantity: int = 0
    reusable_quantity: int = 0
    reusable_content: str | None = None


def plan_delivery(product, quantity):
    if quantity < 1:
        raise ValueError('Quantity must be at least 1.')
    mode = effective_delivery_type(product)
    accounts = account_lines(product.file_content)
    reusable_content = str(getattr(product, 'reusable_delivery', '') or '').strip()
    reusable_stock = max(0, int(getattr(product, 'reusable_stock', 0) or 0)) if reusable_content else 0
    capacity = order_capacity(product)
    if quantity > capacity:
        if bool(product.preorder) and manual_allowed(product):
            return DeliveryPlan((), tuple(accounts), quantity, capacity, 'manual', True, 0, 0, None)
        raise ValueError(f'Only {capacity} available. Choose a smaller quantity.')
    automatic_accounts = min(len(accounts), quantity) if mode in ('automatic', 'hybrid') else 0
    reusable_count = min(reusable_stock, quantity - automatic_accounts) if mode in ('automatic', 'hybrid') else 0
    auto_count = automatic_accounts + reusable_count
    pending = quantity - auto_count
    if pending and not manual_allowed(product):
        raise ValueError(f'Only {len(accounts) + reusable_stock} units are ready for automatic delivery.')
    automatic_payload = list(accounts[:automatic_accounts])
    if reusable_count:
        automatic_payload.append(reusable_content)
    return DeliveryPlan(tuple(automatic_payload), tuple(accounts[automatic_accounts:]), pending,
                        capacity - quantity,
                        'hybrid' if pending and auto_count else 'manual' if pending else mode,
                        False, auto_count, reusable_count, reusable_content if reusable_count else None)
