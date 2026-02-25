"""drop legacy bank_services table"""

revision = "c2d3e4f5a6b7"
down_revision = "b1d2e3f4a5b6"
branch_labels = None
depends_on = None

from alembic import op
import sqlalchemy as sa


def upgrade() -> None:
    op.drop_index(op.f("ix_bank_services_sheet_key"), table_name="bank_services")
    op.drop_index(op.f("ix_bank_services_service_name"), table_name="bank_services")
    op.drop_index(op.f("ix_bank_services_section_name"), table_name="bank_services")
    op.drop_table("bank_services")


def downgrade() -> None:
    op.create_table(
        "bank_services",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("sheet_key", sa.String(length=64), nullable=False),
        sa.Column("section_name", sa.String(length=128), nullable=False),
        sa.Column("service_name", sa.String(length=512), nullable=False),
        sa.Column("row_order", sa.Integer(), nullable=False),
        sa.Column("attributes_json", sa.Text(), nullable=False),
        sa.Column("source_path", sa.String(length=255), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("sheet_key", "section_name", "row_order", name="uq_bank_services_sheet_section_row"),
    )
    op.create_index(op.f("ix_bank_services_section_name"), "bank_services", ["section_name"], unique=False)
    op.create_index(op.f("ix_bank_services_service_name"), "bank_services", ["service_name"], unique=False)
    op.create_index(op.f("ix_bank_services_sheet_key"), "bank_services", ["sheet_key"], unique=False)
