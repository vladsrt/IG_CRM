import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict


class BillingBalanceBase(BaseModel):
    tokens_balance: Decimal = Decimal("0")


class BillingBalanceCreate(BillingBalanceBase):
    user_id: uuid.UUID


class BillingBalanceRead(BillingBalanceBase):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    user_id: uuid.UUID
    last_update: datetime
