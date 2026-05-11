from __future__ import annotations

import enum
import uuid
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, Integer, String, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base

if TYPE_CHECKING:
    from app.models.account import InstagramAccount


class ProxyType(str, enum.Enum):
    """How the proxy rotates its IP."""

    ORDINARY = "ordinary"
    STICKY = "sticky"


class ProxyProtocol(str, enum.Enum):
    """Protocol used to talk to the upstream proxy.

    HTTP and HTTPS work fine with Chrome's authenticated proxy extension.
    SOCKS4 and SOCKS5 are also supported as a transport, but Chrome cannot
    do per request auth for them through webRequest.onAuthRequired. So with
    SOCKS the provider must whitelist our IP and we skip the user:pass part
    in the proxy URL.
    """

    HTTP = "http"
    HTTPS = "https"
    SOCKS4 = "socks4"
    SOCKS5 = "socks5"


class Proxy(Base):
    """Proxy server config row."""

    __tablename__ = "proxies"
    __table_args__ = (
        CheckConstraint(
            "type IN ('ordinary', 'sticky')",
            name="ck_proxies_type",
        ),
        CheckConstraint(
            "protocol IN ('http', 'https', 'socks4', 'socks5')",
            name="ck_proxies_protocol",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    host: Mapped[str] = mapped_column(String(255), nullable=False)
    port: Mapped[int] = mapped_column(Integer, nullable=False)
    username: Mapped[str] = mapped_column(String(255), nullable=False)
    password: Mapped[str] = mapped_column(String(255), nullable=False)
    rotation_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    type: Mapped[str] = mapped_column(String(20), nullable=False)
    protocol: Mapped[str] = mapped_column(
        String(10),
        nullable=False,
        default=ProxyProtocol.HTTP.value,
        server_default=text("'http'"),
    )

    # relations
    instagram_accounts: Mapped[list[InstagramAccount]] = relationship(
        back_populates="proxy",
        lazy="selectin",
    )

    def __repr__(self) -> str:
        return f"<Proxy {self.host}:{self.port}>"
