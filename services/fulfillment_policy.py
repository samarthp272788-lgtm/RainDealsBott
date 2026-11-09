"""Pure delivery planning shared by browsing, checkout, and admin stock tools."""
from dataclasses import dataclass


def account_lines(value):
    return [line.strip() for line in str(value or '').splitlines() if line.strip()]


def manual_allowed(product):
    return (product.delivery_type or 'automatic').lower() == 'manual' or bool(getattr(product, 'manual_fallback', False))


def order_capacity(product):
    mode = (product.delivery_type or 'automatic').lower()
    stock = max(0, int(product.stock or 0))
    accounts = len(account_lines(product.file_content))
    reusable_content = str(getattr(product, 'reusable_delivery', '') or '').strip()
    reusable = max(0, int(getattr(product, 'reusable_stock', 0) or 0)) if reusable_content else 0
    if mode == 'manual':
        return stock
    inventory = accounts + reusable
    return max(stock, inventory) if manual_allowed(product) else inventory


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
    mode = (product.delivery_type or 'automatic').lower()
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
