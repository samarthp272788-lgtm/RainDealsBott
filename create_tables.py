from database import engine, Base

from models.user import User
from models.product import Product
from models.order import Order
from models.deposit import Deposit
from models.ticket import Ticket
from models.referral import Referral
from models.provider import Provider
from models.custom_rate import CustomRate
from models.rate_control import ProductRateControl, RateControlAssignment
from models.order_requirement_submission import OrderRequirementSubmission


print("Creating tables...")

Base.metadata.create_all(bind=engine)

print("✅ Tables Created")
