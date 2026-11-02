from aiogram.fsm.state import State, StatesGroup


class AddProduct(StatesGroup):

    # First selection
    source = State()

    # Own product flow
    name = State()
    icon = State()
    category = State()
    price = State()
    description = State()
    delivery_type = State()
    delivery_instruction = State()
    required_info = State()
    preorder = State()
    bulk_pricing = State()
    accounts = State()

    # Reseller flow
    reseller_select = State()
    reseller_product = State()
    reseller_quantity = State()
    reseller_price = State()



class EditProduct(StatesGroup):
    select_product = State()
    edit_field = State()
    new_value = State()
    name = State()
    category = State()
    required_info = State()


class AddStock(StatesGroup):
    select_product = State()
    accounts = State()


class DeleteProduct(StatesGroup):
    confirm = State()


class AddAccounts(StatesGroup):
    select_product = State()
    accounts = State()
    confirm = State()


class EditAccounts(StatesGroup):
    select_product = State()
    accounts = State()
    replace_accounts = State()
    confirm_replace = State()


class EditBulkPricing(StatesGroup):
    select_product = State()
    pricing = State()
    waiting_input = State()


class OrderRequirementStates(StatesGroup):
    waiting_input = State()
