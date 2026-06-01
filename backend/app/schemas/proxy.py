import uuid

from pydantic import BaseModel, ConfigDict, field_validator

from app.models.proxy import ProxyProtocol, ProxyType


# Whitelist of protocols verified end-to-end through the local
# auth-forwarder (pproxy → Chrome). SOCKS upstreams stay in the DB enum
# for back-compat with old rows but are NOT acceptable on new
# create/update calls until they are verified.
_ALLOWED_PROTOCOLS: frozenset[str] = frozenset(
    {ProxyProtocol.HTTP.value, ProxyProtocol.HTTPS.value}
)


def _reject_unverified_protocol(value: ProxyProtocol | None) -> ProxyProtocol | None:
    if value is None:
        return value
    if value.value not in _ALLOWED_PROTOCOLS:
        raise ValueError(
            f"protocol={value.value!r} is not currently supported. Allowed: "
            f"{sorted(_ALLOWED_PROTOCOLS)}. SOCKS upstreams are wired in the "
            "schema but not verified end-to-end through the local auth "
            "forwarder yet."
        )
    return value


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

    @field_validator("protocol")
    @classmethod
    def _check_protocol(cls, v: ProxyProtocol) -> ProxyProtocol:
        return _reject_unverified_protocol(v)  # type: ignore[return-value]


class ProxyCreate(ProxyBase):
    pass


class ProxyRead(ProxyBase):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID

    # Tolerate legacy rows whose protocol is socks4/socks5 (they exist in
    # the DB but the UI no longer exposes them). Bypass the strict
    # validator on read so the dashboard doesn't 500 on an old row.
    @field_validator("protocol")
    @classmethod
    def _check_protocol(cls, v: ProxyProtocol) -> ProxyProtocol:  # type: ignore[override]
        return v


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

    @field_validator("protocol")
    @classmethod
    def _check_protocol(cls, v: ProxyProtocol | None) -> ProxyProtocol | None:
        return _reject_unverified_protocol(v)
