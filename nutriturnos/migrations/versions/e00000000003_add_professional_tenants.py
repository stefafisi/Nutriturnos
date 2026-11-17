"""isolate clinic data by professional account

Revision ID: e00000000003
Revises: d00000000002
"""
from datetime import datetime

from alembic import op
import sqlalchemy as sa


revision = "e00000000003"
down_revision = "d00000000002"
branch_labels = None
depends_on = None


TENANT_TABLES = (
    "patient",
    "service",
    "doctor_schedule",
    "blocked_day",
    "appointment",
    "clinic_settings",
    "reminder_log",
    "google_token",
    "booking_email_code",
    "clinical_record",
)

NAMING_CONVENTION = {
    "uq": "uq_%(table_name)s_%(column_0_name)s",
}


def upgrade():
    bind = op.get_bind()
    op.add_column("professional_account", sa.Column("display_name", sa.String(120), nullable=True))
    op.add_column("professional_account", sa.Column("slug", sa.String(160), nullable=True))

    accounts = bind.execute(sa.text(
        "SELECT id, email FROM professional_account ORDER BY id"
    )).fetchall()
    for account_id, email in accounts:
        display_name = (email or "").split("@", 1)[0][:120] or "Profesional"
        bind.execute(sa.text(
            "UPDATE professional_account SET display_name = :name, slug = :slug WHERE id = :id"
        ), {"name": display_name, "slug": f"profesional-{account_id}", "id": account_id})

    if not accounts:
        bind.execute(sa.text(
            "INSERT INTO professional_account "
            "(google_sub, email, created_at, display_name, slug) "
            "VALUES (:sub, :email, :created, :name, :slug)"
        ), {
            "sub": "__legacy__",
            "email": "legacy@nutriturnos.invalid",
            "created": datetime.utcnow(),
            "name": "Consultorio actual",
            "slug": "legacy",
        })
        owner_id = bind.execute(sa.text(
            "SELECT id FROM professional_account WHERE google_sub = :sub"
        ), {"sub": "__legacy__"}).scalar_one()
    else:
        owner_id = accounts[0][0]

    with op.batch_alter_table(
        "professional_account",
        naming_convention=NAMING_CONVENTION,
    ) as batch:
        batch.alter_column("display_name", existing_type=sa.String(120), nullable=False)
        batch.alter_column("slug", existing_type=sa.String(160), nullable=False)
        batch.create_unique_constraint("uq_professional_account_slug", ["slug"])

    for table in TENANT_TABLES:
        op.add_column(table, sa.Column("account_id", sa.Integer(), nullable=True))
        bind.execute(sa.text(f"UPDATE {table} SET account_id = :account_id"), {"account_id": owner_id})
        old_unique = None
        old_unique_columns = {"patient": ["dni"], "blocked_day": ["day"]}.get(table)
        if old_unique_columns:
            old_unique = next(
                (constraint["name"] for constraint in sa.inspect(bind).get_unique_constraints(table)
                 if constraint["column_names"] == old_unique_columns),
                None,
            )
            if old_unique is None:
                old_unique = f"uq_{table}_{old_unique_columns[0]}"
        with op.batch_alter_table(table, naming_convention=NAMING_CONVENTION) as batch:
            batch.alter_column("account_id", existing_type=sa.Integer(), nullable=False)
            batch.create_foreign_key(
                f"fk_{table}_account_id_professional_account",
                "professional_account",
                ["account_id"],
                ["id"],
            )
            if table not in ("clinic_settings", "google_token"):
                batch.create_index(f"ix_{table}_account_id", ["account_id"])
            if table == "patient":
                batch.drop_constraint(old_unique, type_="unique")
                batch.create_unique_constraint(
                    "uq_patient_account_dni", ["account_id", "dni"]
                )
            elif table == "blocked_day":
                batch.drop_constraint(old_unique, type_="unique")
                batch.create_unique_constraint(
                    "uq_blocked_day_account_day", ["account_id", "day"]
                )
            elif table in ("clinic_settings", "google_token"):
                batch.create_unique_constraint(
                    f"uq_{table}_account_id", ["account_id"]
                )

    op.drop_index("uq_appointment_active_slot", table_name="appointment")
    op.create_index(
        "uq_appointment_active_slot",
        "appointment",
        ["account_id", "appointment_date"],
        unique=True,
        sqlite_where=sa.text("status != 'cancelado'"),
        postgresql_where=sa.text("status != 'cancelado'"),
    )


def downgrade():
    op.drop_index("uq_appointment_active_slot", table_name="appointment")
    op.create_index(
        "uq_appointment_active_slot",
        "appointment",
        ["appointment_date"],
        unique=True,
        sqlite_where=sa.text("status != 'cancelado'"),
        postgresql_where=sa.text("status != 'cancelado'"),
    )

    for table in reversed(TENANT_TABLES):
        with op.batch_alter_table(table, naming_convention=NAMING_CONVENTION) as batch:
            if table == "patient":
                batch.drop_constraint("uq_patient_account_dni", type_="unique")
                batch.create_unique_constraint("uq_patient_dni", ["dni"])
            elif table == "blocked_day":
                batch.drop_constraint("uq_blocked_day_account_day", type_="unique")
                batch.create_unique_constraint("uq_blocked_day_day", ["day"])
            elif table in ("clinic_settings", "google_token"):
                batch.drop_constraint(f"uq_{table}_account_id", type_="unique")
            if table not in ("clinic_settings", "google_token"):
                batch.drop_index(f"ix_{table}_account_id")
            batch.drop_constraint(
                f"fk_{table}_account_id_professional_account", type_="foreignkey"
            )
            batch.drop_column("account_id")

    with op.batch_alter_table(
        "professional_account",
        naming_convention=NAMING_CONVENTION,
    ) as batch:
        batch.drop_constraint("uq_professional_account_slug", type_="unique")
        batch.drop_column("slug")
        batch.drop_column("display_name")
