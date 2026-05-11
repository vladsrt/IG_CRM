import uuid

from pydantic import BaseModel, ConfigDict

from app.models.proxy import ProxyProtocol, ProxyType


class ProxyBase(BaseModel):
    host: str
    port: int
    username: str
    password: str
    rotation_url: str
    type: ProxyType
    protocol: ProxyProtocol = ProxyProtocol.HTTP


class ProxyCreate(ProxyBase):
    pass


class ProxyRead(ProxyBase):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID


class ProxyUpdate(BaseModel):
    """PATCH body. All fields optional."""

    model_config = ConfigDict(extra="forbid")

    host: str | None = None
    port: int | None = None
    username: str | None = None
    password: str | None = None
    rotation_url: str | None = None
    type: ProxyType | None = None
    protocol: ProxyProtocol | None = None
