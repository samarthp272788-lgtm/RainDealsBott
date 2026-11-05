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
    if mode == 'manual':
        return stock
    return max(stock, accounts) if manual_allowed(product) else accounts


@dataclass(frozen=True)
class DeliveryPlan:
    automatic: tuple[str, ...]
    remaining: tuple[str, ...]
    manual_quantity: int
    stock_after: int
    order_mode: str
    preorder: bool = False


def plan_delivery(product, quantity):
    if quantity < 1:
        raise ValueError('Quantity must be at least 1.')
    mode = (product.delivery_type or 'automatic').lower()
    accounts = account_lines(product.file_content)
    capacity = order_capacity(product)
    if quantity > capacity:
        if bool(product.preorder) and manual_allowed(product):
            return DeliveryPlan((), tuple(accounts), quantity, capacity, 'manual', True)
        raise ValueError(f'Only {capacity} available. Choose a smaller quantity.')
    auto_count = min(len(accounts), quantity) if mode in ('automatic', 'hybrid') else 0
    pending = quantity - auto_count
    if pending and not manual_allowed(product):
        raise ValueError(f'Only {len(accounts)} accounts are ready for automatic delivery.')
    return DeliveryPlan(tuple(accounts[:auto_count]), tuple(accounts[auto_count:]), pending,
                        capacity - quantity,
                        'hybrid' if pending and auto_count else 'manual' if pending else mode)
