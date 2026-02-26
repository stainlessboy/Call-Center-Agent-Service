"""add lead table"""

revision = '913edc1a4151'
down_revision = 'c2d3e4f5a6b7'
branch_labels = None
depends_on = None

from alembic import op
import sqlalchemy as sa


def upgrade() -> None:
    op.create_table('leads',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('session_id', sa.String(length=36), nullable=True),
    sa.Column('telegram_user_id', sa.BigInteger(), nullable=True),
    sa.Column('product_category', sa.String(length=64), nullable=True),
    sa.Column('product_name', sa.String(length=512), nullable=True),
    sa.Column('amount', sa.BigInteger(), nullable=True),
    sa.Column('term_months', sa.Integer(), nullable=True),
    sa.Column('rate_pct', sa.Float(), nullable=True),
    sa.Column('contact_name', sa.String(length=255), nullable=True),
    sa.Column('contact_phone', sa.String(length=64), nullable=True),
    sa.Column('status', sa.String(length=32), server_default='new', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_leads_session_id'), 'leads', ['session_id'], unique=False)
    op.create_index(op.f('ix_leads_telegram_user_id'), 'leads', ['telegram_user_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_leads_telegram_user_id'), table_name='leads')
    op.drop_index(op.f('ix_leads_session_id'), table_name='leads')
    op.drop_table('leads')
