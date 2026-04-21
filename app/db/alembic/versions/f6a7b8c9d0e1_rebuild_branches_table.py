"""split branches into 3 tables: filials, sales_offices, sales_points

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-04-21 15:00:00.000000

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "f6a7b8c9d0e1"
down_revision: str | None = "e5f6a7b8c9d0"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # Drop old flat branches table (test data only, safe to lose)
    op.drop_table("branches")

    # filials — главные офисы (landmark, location_url, нет parent)
    op.create_table(
        "filials",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name_ru", sa.String(255), nullable=False),
        sa.Column("name_uz", sa.String(255), nullable=True),
        sa.Column("address_ru", sa.Text(), nullable=False),
        sa.Column("address_uz", sa.Text(), nullable=True),
        sa.Column("landmark_ru", sa.Text(), nullable=True),
        sa.Column("landmark_uz", sa.Text(), nullable=True),
        sa.Column("location_url", sa.String(512), nullable=True),
        sa.Column("latitude", sa.Float(), nullable=True),
        sa.Column("longitude", sa.Float(), nullable=True),
        sa.Column("phone", sa.String(64), nullable=True),
        sa.Column("hours", sa.String(255), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_filials_name_ru", "filials", ["name_ru"])

    # sales_offices — мини-офисы (region + FK к filial)
    op.create_table(
        "sales_offices",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name_ru", sa.String(255), nullable=False),
        sa.Column("name_uz", sa.String(255), nullable=True),
        sa.Column("region_ru", sa.String(255), nullable=True),
        sa.Column("region_uz", sa.String(255), nullable=True),
        sa.Column("address_ru", sa.Text(), nullable=False),
        sa.Column("address_uz", sa.Text(), nullable=True),
        sa.Column(
            "parent_filial_id",
            sa.Integer(),
            sa.ForeignKey("filials.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("latitude", sa.Float(), nullable=True),
        sa.Column("longitude", sa.Float(), nullable=True),
        sa.Column("phone", sa.String(64), nullable=True),
        sa.Column("hours", sa.String(255), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_sales_offices_name_ru", "sales_offices", ["name_ru"])
    op.create_index("ix_sales_offices_region_ru", "sales_offices", ["region_ru"])
    op.create_index(
        "ix_sales_offices_parent_filial_id", "sales_offices", ["parent_filial_id"]
    )

    # sales_points — точки в автосалонах (без региона, только FK)
    op.create_table(
        "sales_points",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name_ru", sa.String(255), nullable=False),
        sa.Column("name_uz", sa.String(255), nullable=True),
        sa.Column("address_ru", sa.Text(), nullable=False),
        sa.Column("address_uz", sa.Text(), nullable=True),
        sa.Column(
            "parent_filial_id",
            sa.Integer(),
            sa.ForeignKey("filials.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("latitude", sa.Float(), nullable=True),
        sa.Column("longitude", sa.Float(), nullable=True),
        sa.Column("phone", sa.String(64), nullable=True),
        sa.Column("hours", sa.String(255), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_sales_points_name_ru", "sales_points", ["name_ru"])
    op.create_index(
        "ix_sales_points_parent_filial_id", "sales_points", ["parent_filial_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_sales_points_parent_filial_id", table_name="sales_points")
    op.drop_index("ix_sales_points_name_ru", table_name="sales_points")
    op.drop_table("sales_points")

    op.drop_index("ix_sales_offices_parent_filial_id", table_name="sales_offices")
    op.drop_index("ix_sales_offices_region_ru", table_name="sales_offices")
    op.drop_index("ix_sales_offices_name_ru", table_name="sales_offices")
    op.drop_table("sales_offices")

    op.drop_index("ix_filials_name_ru", table_name="filials")
    op.drop_table("filials")

    # Restore the old flat branches (minimal, for safety)
    op.create_table(
        "branches",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("region", sa.String(255), nullable=False),
        sa.Column("district", sa.String(255), nullable=False),
        sa.Column("address", sa.Text(), nullable=True),
        sa.Column("landmarks", sa.Text(), nullable=True),
        sa.Column("metro", sa.String(255), nullable=True),
        sa.Column("phone", sa.String(64), nullable=True),
        sa.Column("hours", sa.String(255), nullable=True),
        sa.Column("weekend", sa.String(255), nullable=True),
        sa.Column("inn", sa.String(64), nullable=True),
        sa.Column("mfo", sa.String(64), nullable=True),
        sa.Column("postal_index", sa.String(32), nullable=True),
        sa.Column("uzcard_accounts", sa.Text(), nullable=True),
        sa.Column("humo_accounts", sa.Text(), nullable=True),
        sa.Column("latitude", sa.Float(), nullable=True),
        sa.Column("longitude", sa.Float(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
