"""add alliance name uniqueness constraint

Revision ID: 08fe8c314fee
Revises: af181031a5cc
Create Date: 2026-09-03 23:55:35.765448

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "08fe8c314fee"
down_revision: Union[str, Sequence[str], None] = "af181031a5cc"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("alliances") as batch_op:
        batch_op.create_unique_constraint(
            "uq_alliances_guild_id_name",
            ["guild_id", "name"],
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("alliances") as batch_op:
        batch_op.drop_constraint(
            "uq_alliances_guild_id_name",
            type_="unique",
        )
