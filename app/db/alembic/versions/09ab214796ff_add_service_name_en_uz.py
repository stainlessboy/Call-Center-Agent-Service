"""add_service_name_en_uz"""

revision = '09ab214796ff'
down_revision = '913edc1a4151'
branch_labels = None
depends_on = None

from alembic import op
import sqlalchemy as sa


def upgrade() -> None:
    op.add_column('card_product_offers', sa.Column('service_name_en', sa.String(length=512), nullable=True))
    op.add_column('card_product_offers', sa.Column('service_name_uz', sa.String(length=512), nullable=True))
    op.add_column('credit_product_offers', sa.Column('service_name_en', sa.String(length=512), nullable=True))
    op.add_column('credit_product_offers', sa.Column('service_name_uz', sa.String(length=512), nullable=True))
    op.add_column('deposit_product_offers', sa.Column('service_name_en', sa.String(length=512), nullable=True))
    op.add_column('deposit_product_offers', sa.Column('service_name_uz', sa.String(length=512), nullable=True))


def downgrade() -> None:
    op.drop_column('deposit_product_offers', 'service_name_uz')
    op.drop_column('deposit_product_offers', 'service_name_en')
    op.drop_column('credit_product_offers', 'service_name_uz')
    op.drop_column('credit_product_offers', 'service_name_en')
    op.drop_column('card_product_offers', 'service_name_uz')
    op.drop_column('card_product_offers', 'service_name_en')
