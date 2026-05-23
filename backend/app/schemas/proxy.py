import uuid

from pydantic import BaseModel, ConfigDict

from app.models.proxy import ProxyProtocol, ProxyType


class ProxyBase(BaseModel):
    host: str
    port: int
    username: str = ""
    password: str = ""
    # optional with defaults so a per-account proxy can be added with just
    # host/port/(creds). DB columns are NOT NULL, hence empty-string defaults.
    rotation_url: str = ""
    type: ProxyType = ProxyType.ORDINARY
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
