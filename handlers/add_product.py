
from utils.ui import answer_card
from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message
from aiogram.fsm.context import FSMContext
from decimal import Decimal, InvalidOperation
import logging
from sqlalchemy.exc import SQLAlchemyError

from config import ADMIN_IDS
from database import SessionLocal
from models.product import Product
from services.fulfillment_policy import account_lines
from states.product_states import AddProduct

router = Router()
logger = logging.getLogger(__name__)


def is_admin(user_id):
    return user_id in ADMIN_IDS


@router.message(Command("addproduct"))
async def add_product(
        message: Message,
        state: FSMContext
):

    if not is_admin(message.from_user.id):
        return

    await state.set_state(
        AddProduct.name
    )

    await answer_card(message,
        "📌 Enter Product Name:"
    )


# NAME
@router.message(AddProduct.name)
async def product_name(
        message: Message,
        state: FSMContext
):

    value = (message.text or "").strip()
    if not value or len(value) > 255:
        await answer_card(message, "❌ Enter a product name between 1 and 255 characters.")
        return

    await state.update_data(
        name=value
    )

    await state.set_state(
        AddProduct.icon
    )

    await answer_card(message,
        """
😀 Enter Product Icon

Examples:

🎬
🎮
🎵
📺
💎
"""
    )


# ICON
@router.message(AddProduct.icon)
async def product_icon(
        message: Message,
        state: FSMContext
):

    value = (message.text or "").strip()
    if not value or len(value) > 50:
        await answer_card(message, "❌ Enter an icon or short label up to 50 characters.")
        return

    await state.update_data(
        icon=value
    )

    await state.set_state(
        AddProduct.description
    )

    await answer_card(message,
        "📝 Enter Description:"
    )


# DESCRIPTION
@router.message(AddProduct.description)
async def product_description(
        message: Message,
        state: FSMContext
):

    value = message.text or message.caption or ""
    if len(value) > 16000:
        await answer_card(message, "❌ Description is too long. Keep it below 16,000 characters.")
        return

    await state.update_data(
        description=value
    )

    await state.set_state(
        AddProduct.price
    )

    await answer_card(message,
        "💲 Enter Price:"
    )


# PRICE
@router.message(AddProduct.price)
async def product_price(
        message: Message,
        state: FSMContext
):

    try:
        price = Decimal((message.text or "").strip())
        if not price.is_finite() or price < 0 or price > Decimal("999999999999.99999999"):
            raise InvalidOperation
    except (InvalidOperation, ValueError, TypeError):
        await answer_card(message,
            "❌ Enter a finite price from 0 up to 999,999,999,999.99999999."
        )
        return

    await state.update_data(
        # FSM stores are often JSON-backed; keep Decimal lossless as text.
        price=str(price)
    )

    await state.set_state(
        AddProduct.category
    )

    await answer_card(message,
        "🏷 Enter Category:"
    )


# CATEGORY
@router.message(AddProduct.category)
async def product_category(
        message: Message,
        state: FSMContext
):

    value = (message.text or "").strip()
    if not value or len(value) > 255:
        await answer_card(message, "❌ Enter a category between 1 and 255 characters.")
        return

    await state.update_data(
        category=value
    )

    await state.set_state(
        AddProduct.accounts
    )

    await answer_card(message,
        """
📦 Send accounts.

Example:

mail1@gmail.com:123
mail2@gmail.com:456
mail3@gmail.com:789
"""
    )


# ACCOUNTS
@router.message(AddProduct.accounts)
async def product_accounts(
        message: Message,
        state: FSMContext
):

    if not message.text:
        await answer_card(message, "❌ Send account lines as text. No product was created.")
        return

    # Keep the first copy of each credential. Email identifiers are matched
    # case-insensitively while password/key contents remain case-sensitive.
    accounts = account_lines(message.text)

    stock = len(accounts)

    data = await state.get_data()

    db = SessionLocal()

    try:

        product = Product(
            name=data["name"],
            icon=data["icon"],
            description=data["description"],
            price=Decimal(str(data["price"])),
            category=data["category"],
            stock=stock,
            file_content="\n".join(
                accounts
            )
        )

        db.add(product)

        db.commit()
    except SQLAlchemyError:
        db.rollback()
        logger.exception("Legacy add-product save failed for admin=%s", message.from_user.id)
        await answer_card(message, "❌ Product could not be saved. Your setup is still open; please retry or cancel.")
        return

    finally:

        db.close()

    await state.clear()

    await answer_card(message,
        f"""
✅ Product Added

{data["icon"]} {data["name"]}

💲 Price:
${data["price"]}

🏷 Category:
{data["category"]}

📦 Stock:
{stock}
"""
    )
