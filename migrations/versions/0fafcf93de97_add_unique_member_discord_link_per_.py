"""add unique member discord link per alliance

Revision ID: 0fafcf93de97
Revises: b802f8d8a50f
Create Date: 2026-09-04 11:19:17.411026

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "0fafcf93de97"
down_revision: Union[str, Sequence[str], None] = "b802f8d8a50f"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("members") as batch_op:
        batch_op.create_unique_constraint(
            "uq_members_alliance_id_discord_user_id",
            ["alliance_id", "discord_user_id"],
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("members") as batch_op:
        batch_op.drop_constraint(
            "uq_members_alliance_id_discord_user_id",
            type_="unique",
        )
