"""add Google professional accounts

Revision ID: d00000000002
Revises: ca0d2b4140ae
Create Date: 2026-11-16

"""
from alembic import op
import sqlalchemy as sa


revision = "d00000000002"
down_revision = "ca0d2b4140ae"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "professional_account",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("google_sub", sa.String(length=255), nullable=False),
        sa.Column("email", sa.String(length=254), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("google_sub"),
    )
    op.create_index(
        op.f("ix_professional_account_email"),
        "professional_account",
        ["email"],
        unique=False,
    )


def downgrade():
    op.drop_index(op.f("ix_professional_account_email"), table_name="professional_account")
    op.drop_table("professional_account")
