"""update item schema for bulk and unit fields

Revision ID: 6a4136f8e33f
Revises: 748f62d467af
Create Date: 2026-09-24 11:07:11.355757

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '6a4136f8e33f'
down_revision: Union[str, Sequence[str], None] = '748f62d467af'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Alter item table to support bulk packages and unit quantities."""
    # 1. Rename column quantity -> unit_quantity
    op.alter_column('item', 'quantity', new_column_name='unit_quantity')

    # 2. Change unit_quantity type to Integer, nullable
    op.alter_column(
        'item',
        'unit_quantity',
        existing_type=sa.Float(),
        type_=sa.Integer(),
        nullable=True,
        postgresql_using='unit_quantity::integer',
    )

    # 3. Add column bulk_type (String, nullable)
    op.add_column('item', sa.Column('bulk_type', sa.String(), nullable=True))

    # 4. Add column bulk_quantity (Integer, nullable)
    op.add_column('item', sa.Column('bulk_quantity', sa.Integer(), nullable=True))

    # 5. Make unit_price nullable
    op.alter_column('item', 'unit_price', existing_type=sa.Float(), nullable=True)


def downgrade() -> None:
    """Reverse changes: drop bulk columns and restore quantity as non-nullable float."""
    # 5. Revert unit_price back to non-nullable
    op.alter_column('item', 'unit_price', existing_type=sa.Float(), nullable=False)

    # 4. Drop bulk_quantity
    op.drop_column('item', 'bulk_quantity')

    # 3. Drop bulk_type
    op.drop_column('item', 'bulk_type')

    # 2. Revert unit_quantity back to Float, non-nullable
    op.alter_column(
        'item',
        'unit_quantity',
        existing_type=sa.Integer(),
        type_=sa.Float(),
        nullable=False,
    )

    # 1. Rename unit_quantity back to quantity
    op.alter_column('item', 'unit_quantity', new_column_name='quantity')
