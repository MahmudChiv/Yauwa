"""allow pending trader name

Revision ID: 748f62d467af
Revises: 312a4154ed5c
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "748f62d467af"
down_revision: Union[str, Sequence[str], None] = "312a4154ed5c"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Allow a trader to exist while the bot is waiting for their name."""
    op.alter_column("trader", "name", existing_type=sa.String(), nullable=True)


def downgrade() -> None:
    """Restore the constraint only when every trader has completed onboarding."""
    connection = op.get_bind()
    pending_count = connection.execute(
        sa.text("SELECT count(*) FROM trader WHERE name IS NULL")
    ).scalar_one()
    if pending_count:
        raise RuntimeError(
            "Cannot downgrade while trader rows with NULL names still exist."
        )
    op.alter_column("trader", "name", existing_type=sa.String(), nullable=False)
