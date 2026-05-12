"""
adds protocol to proxies, platform and user agent to accounts.

Revision ID: 9b21f0c4ae37
Revises: 54c53356ac82
Create Date: 2026-05-08 09:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "9b21f0c4ae37"
down_revision: Union[str, Sequence[str], None] = "54c53356ac82"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """upgrades schema."""
    # proxies.protocol
    op.add_column(
        "proxies",
        sa.Column(
            "protocol",
            sa.String(length=10),
            nullable=False,
            server_default="http",
        ),
    )
    op.create_check_constraint(
        "ck_proxies_protocol",
        "proxies",
        "protocol IN ('http', 'https', 'socks4', 'socks5')",
    )

    # instagram_accounts.platform
    op.add_column(
        "instagram_accounts",
        sa.Column(
            "platform",
            sa.String(length=16),
            nullable=False,
            server_default="windows",
        ),
    )
    op.create_check_constraint(
        "ck_instagram_accounts_platform",
        "instagram_accounts",
        "platform IN ('windows', 'macos', 'linux')",
    )

    # instagram_accounts.user_agent
    op.add_column(
        "instagram_accounts",
        sa.Column("user_agent", sa.String(length=512), nullable=True),
    )


def downgrade() -> None:
    """downgrades schema."""
    op.drop_column("instagram_accounts", "user_agent")
    op.drop_constraint(
        "ck_instagram_accounts_platform", "instagram_accounts", type_="check"
    )
    op.drop_column("instagram_accounts", "platform")
    op.drop_constraint("ck_proxies_protocol", "proxies", type_="check")
    op.drop_column("proxies", "protocol")
