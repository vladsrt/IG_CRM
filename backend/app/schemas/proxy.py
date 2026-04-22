import uuid

from pydantic import BaseModel, ConfigDict

from app.models.proxy import ProxyType


class ProxyBase(BaseModel):
    host: str
    port: int
    username: str
    password: str
    rotation_url: str
    type: ProxyType


class ProxyCreate(ProxyBase):
    pass


class ProxyRead(ProxyBase):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
