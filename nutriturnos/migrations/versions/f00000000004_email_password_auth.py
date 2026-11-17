"""support professional email and password accounts

Revision ID: f00000000004
Revises: e00000000003
"""
from alembic import op
import sqlalchemy as sa


revision = "f00000000004"
down_revision = "e00000000003"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    bind.execute(sa.text("UPDATE professional_account SET email = lower(email)"))
    duplicates = bind.execute(sa.text(
        "SELECT email FROM professional_account GROUP BY email HAVING COUNT(*) > 1 LIMIT 10"
    )).fetchall()
    if duplicates:
        raise RuntimeError(
            "Hay cuentas profesionales con emails repetidos; resolvelas antes de cambiar el inicio de sesión: "
            + ", ".join(row[0] for row in duplicates)
        )

    op.add_column(
        "professional_account",
        sa.Column("password_hash", sa.String(length=255), nullable=True),
    )
    with op.batch_alter_table("professional_account") as batch:
        batch.alter_column(
            "google_sub",
            existing_type=sa.String(length=255),
            nullable=True,
        )
        batch.create_unique_constraint(
            "uq_professional_account_email",
            ["email"],
        )


def downgrade():
    bind = op.get_bind()
    has_password_accounts = bind.execute(sa.text(
        "SELECT COUNT(*) FROM professional_account WHERE password_hash IS NOT NULL"
    )).scalar_one()
    if has_password_accounts:
        raise RuntimeError("No se puede volver al inicio con Google mientras existan cuentas con contraseña.")

    with op.batch_alter_table("professional_account") as batch:
        batch.drop_constraint("uq_professional_account_email", type_="unique")
        batch.drop_column("password_hash")
        batch.alter_column(
            "google_sub",
            existing_type=sa.String(length=255),
            nullable=False,
        )
