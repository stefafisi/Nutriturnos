"""Initial schema used by the original SQLite application.

Revision ID: b00000000001
"""
from alembic import op
import sqlalchemy as sa


revision = "b00000000001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "patient",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("first_name", sa.String(length=100), nullable=False),
        sa.Column("last_name", sa.String(length=100), nullable=False),
        sa.Column("dni", sa.String(length=20), nullable=True),
        sa.Column("phone", sa.String(length=50), nullable=False),
        sa.Column("email", sa.String(length=100), nullable=True),
        sa.Column("birth_date", sa.Date(), nullable=True),
        sa.Column("gender", sa.String(length=20), nullable=True),
        sa.Column("medical_history", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("dni"),
    )
    op.create_table(
        "service",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("duration_minutes", sa.Integer(), nullable=False),
        sa.Column("price", sa.Numeric(precision=10, scale=2), nullable=False),
        sa.Column("is_online", sa.Boolean(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "doctor_schedule",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("day_of_week", sa.Integer(), nullable=False),
        sa.Column("start_time", sa.Time(), nullable=False),
        sa.Column("end_time", sa.Time(), nullable=False),
        sa.Column("slot_duration", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "blocked_day",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("reason", sa.String(length=100), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("day"),
    )
    op.create_table(
        "appointment",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("patient_id", sa.Integer(), nullable=False),
        sa.Column("service_id", sa.Integer(), nullable=True),
        sa.Column("appointment_date", sa.DateTime(), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=True),
        sa.Column("payment_status", sa.String(length=30), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["patient_id"], ["patient.id"]),
        sa.ForeignKeyConstraint(["service_id"], ["service.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "clinical_record",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("appointment_id", sa.Integer(), nullable=True),
        sa.Column("patient_id", sa.Integer(), nullable=False),
        sa.Column("record_date", sa.Date(), nullable=True),
        sa.Column("weight_kg", sa.Float(), nullable=True),
        sa.Column("height_cm", sa.Float(), nullable=True),
        sa.Column("bmi", sa.Float(), nullable=True),
        sa.Column("fat_percentage", sa.Float(), nullable=True),
        sa.Column("muscle_percentage", sa.Float(), nullable=True),
        sa.Column("visceral_fat", sa.Integer(), nullable=True),
        sa.Column("waist_cm", sa.Float(), nullable=True),
        sa.Column("hip_cm", sa.Float(), nullable=True),
        sa.Column("arm_cm", sa.Float(), nullable=True),
        sa.Column("thigh_cm", sa.Float(), nullable=True),
        sa.Column("tricipital_mm", sa.Float(), nullable=True),
        sa.Column("subscapular_mm", sa.Float(), nullable=True),
        sa.Column("suprailiac_mm", sa.Float(), nullable=True),
        sa.Column("abdominal_mm", sa.Float(), nullable=True),
        sa.Column("diet_plan_notes", sa.Text(), nullable=True),
        sa.Column("next_goals", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["appointment_id"], ["appointment.id"]),
        sa.ForeignKeyConstraint(["patient_id"], ["patient.id"]),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade():
    op.drop_table("clinical_record")
    op.drop_table("appointment")
    op.drop_table("blocked_day")
    op.drop_table("doctor_schedule")
    op.drop_table("service")
    op.drop_table("patient")
