"""add token usage fields to messages"""

revision = 'd4e5f6a7b8c9'
down_revision = '09ab214796ff'
branch_labels = None
depends_on = None

from alembic import op
import sqlalchemy as sa


def upgrade() -> None:
    op.add_column('messages', sa.Column('prompt_tokens', sa.Integer(), nullable=True))
    op.add_column('messages', sa.Column('completion_tokens', sa.Integer(), nullable=True))
    op.add_column('messages', sa.Column('total_tokens', sa.Integer(), nullable=True))
    op.add_column('messages', sa.Column('llm_cost', sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column('messages', 'llm_cost')
    op.drop_column('messages', 'total_tokens')
    op.drop_column('messages', 'completion_tokens')
    op.drop_column('messages', 'prompt_tokens')
