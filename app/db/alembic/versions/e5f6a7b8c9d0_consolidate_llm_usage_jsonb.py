"""consolidate token fields + agent_model into single llm_usage JSONB column"""

revision = 'e5f6a7b8c9d0'
down_revision = 'd4e5f6a7b8c9'
branch_labels = None
depends_on = None

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


def upgrade() -> None:
    # Migrate existing data into JSONB before dropping columns
    op.add_column('messages', sa.Column('llm_usage', JSONB(), nullable=True))
    op.execute("""
        UPDATE messages
        SET llm_usage = jsonb_build_object(
            'model', COALESCE(agent_model, ''),
            'prompt_tokens', COALESCE(prompt_tokens, 0),
            'completion_tokens', COALESCE(completion_tokens, 0),
            'total_tokens', COALESCE(total_tokens, 0),
            'cost', COALESCE(llm_cost, 0)
        )
        WHERE prompt_tokens IS NOT NULL OR llm_cost IS NOT NULL OR agent_model IS NOT NULL
    """)
    op.drop_column('messages', 'prompt_tokens')
    op.drop_column('messages', 'completion_tokens')
    op.drop_column('messages', 'total_tokens')
    op.drop_column('messages', 'llm_cost')
    op.drop_column('messages', 'agent_model')


def downgrade() -> None:
    op.add_column('messages', sa.Column('agent_model', sa.String(128), nullable=True))
    op.add_column('messages', sa.Column('prompt_tokens', sa.Integer(), nullable=True))
    op.add_column('messages', sa.Column('completion_tokens', sa.Integer(), nullable=True))
    op.add_column('messages', sa.Column('total_tokens', sa.Integer(), nullable=True))
    op.add_column('messages', sa.Column('llm_cost', sa.Float(), nullable=True))
    op.execute("""
        UPDATE messages
        SET agent_model = llm_usage->>'model',
            prompt_tokens = (llm_usage->>'prompt_tokens')::int,
            completion_tokens = (llm_usage->>'completion_tokens')::int,
            total_tokens = (llm_usage->>'total_tokens')::int,
            llm_cost = (llm_usage->>'cost')::float
        WHERE llm_usage IS NOT NULL
    """)
    op.drop_column('messages', 'llm_usage')
