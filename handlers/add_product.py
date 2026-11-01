
from utils.ui import answer_card
from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message
from aiogram.fsm.context import FSMContext

from config import ADMIN_IDS
from database import SessionLocal
from models.product import Product
from states.product_states import AddProduct

router = Router()


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

    await state.update_data(
        name=message.text
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

    await state.update_data(
        icon=message.text
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

    await state.update_data(
        description=message.text
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

        price = float(
            message.text
        )

    except:

        await answer_card(message,
            "❌ Invalid price."
        )

        return

    await state.update_data(
        price=price
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

    await state.update_data(
        category=message.text
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

    accounts = []

    for line in message.text.splitlines():

        line = line.strip()

        if line:

            accounts.append(
                line
            )

    stock = len(accounts)

    data = await state.get_data()

    db = SessionLocal()

    try:

        product = Product(
            name=data["name"],
            icon=data["icon"],
            description=data["description"],
            price=data["price"],
            category=data["category"],
            stock=stock,
            file_content="\n".join(
                accounts
            )
        )

        db.add(product)

        db.commit()

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