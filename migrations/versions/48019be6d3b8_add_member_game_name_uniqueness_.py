"""add member game name uniqueness constraint

Revision ID: 48019be6d3b8
Revises: 08fe8c314fee
Create Date: 2026-09-04 00:18:18.192212

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "48019be6d3b8"
down_revision: Union[str, Sequence[str], None] = "08fe8c314fee"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("members") as batch_op:
        batch_op.create_unique_constraint(
            "uq_members_alliance_id_game_name",
            ["alliance_id", "game_name"],
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("members") as batch_op:
        batch_op.drop_constraint(
            "uq_members_alliance_id_game_name",
            type_="unique",
        )
