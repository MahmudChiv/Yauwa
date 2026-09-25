"""add low_stock_item table

Revision ID: 414e920ecda1
Revises: 6a4136f8e33f
Create Date: 2026-09-25 20:25:05.226868

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = '414e920ecda1'
down_revision: Union[str, Sequence[str], None] = '6a4136f8e33f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'lowstockitem',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('item_id', sa.Integer(), nullable=False),
        sa.Column('trader_id', sa.Integer(), nullable=False),
        sa.Column('created_at', sqlmodel.sql.sqltypes.UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(['item_id'], ['item.id']),
        sa.ForeignKeyConstraint(['trader_id'], ['trader.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_lowstockitem_item_id'), 'lowstockitem', ['item_id'], unique=True)
    op.create_index(op.f('ix_lowstockitem_trader_id'), 'lowstockitem', ['trader_id'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_lowstockitem_trader_id'), table_name='lowstockitem')
    op.drop_index(op.f('ix_lowstockitem_item_id'), table_name='lowstockitem')
    op.drop_table('lowstockitem')
