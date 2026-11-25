import os
import re
import smtplib
import ssl
import json
import hashlib
import base64
import secrets
import sqlite3
from pathlib import Path
from email.message import EmailMessage
from datetime import datetime, date, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from functools import wraps
from zoneinfo import ZoneInfo
from flask import (Flask, render_template, request, jsonify, redirect,
                   url_for, session, flash, abort, send_from_directory)
from flask_sqlalchemy import SQLAlchemy
from flask_wtf.csrf import CSRFProtect
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_migrate import Migrate
from flask import send_file
from flask import g, has_request_context
from authlib.integrations.flask_client import OAuth
from cryptography.fernet import Fernet
from cryptography.fernet import InvalidToken
import requests
from urllib.parse import quote
from sqlalchemy import Index, UniqueConstraint, event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as SQLAlchemySession, with_loader_criteria
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from werkzeug.security import check_password_hash, generate_password_hash
import unicodedata

ARGENTINA_TZ = ZoneInfo("America/Argentina/Buenos_Aires")
secret_key = os.environ.get("SECRET_KEY")
if not secret_key:
    raise RuntimeError("Definí SECRET_KEY con un valor aleatorio antes de iniciar la aplicación.")
if len(secret_key) < 32:
    raise RuntimeError("SECRET_KEY debe tener al menos 32 caracteres.")
app = Flask(__name__)
app_instance_dir = Path(__file__).resolve().parent / "instance"
app_instance_dir.mkdir(parents=True, exist_ok=True)
default_db_path = app_instance_dir / "nutri.db"
if not default_db_path.exists():
    default_db_path = Path(app.instance_path) / "nutri.db"
    default_db_path.parent.mkdir(parents=True, exist_ok=True)
app.config.update(
    SECRET_KEY=secret_key,
    SQLALCHEMY_DATABASE_URI=os.environ.get(
        "DATABASE_URL",
        f"sqlite:///{default_db_path.as_posix().replace('\\', '/')}",
    ),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "true").lower() in ("1", "true", "yes"),
    PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
)
db = SQLAlchemy(app)
csrf = CSRFProtect(app)
Migrate(app, db)
limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=[],
    storage_uri=os.environ.get("RATELIMIT_STORAGE_URI", "memory://"),
)

DIAS = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]
ESTADOS = ["pendiente", "confirmado", "completado", "cancelado", "no_asistio"]
ESTADOS_PAGO = ["pendiente", "seña_pagada", "completado"]
PROFESSIONAL_BRANDING = {
    "franco-paporello-3": {
        "logo": "images/franco-paporello-logo.png",
        "icon": "images/franco-paporello-icon.png",
        "address": "Zeballos 2920 (Gimnasio Gefitness)",
        "whatsapp_number": "5493424053538",
        "whatsapp_display": "+54 9 342 405 3538",
        "primary": "#006C90",
        "accent": "#C9D803",
        "background": "#FEFBE6",
        "text": "#0F5674",
        "border": "#C9D9D4",
    },
}


def utcnow_naive():
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ---------- Modelos ----------
class Patient(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    account_id = db.Column(db.Integer, db.ForeignKey("professional_account.id"), nullable=False, index=True)
    first_name = db.Column(db.String(100), nullable=False)
    last_name = db.Column(db.String(100), nullable=False)
    dni = db.Column(db.String(20))
    phone = db.Column(db.String(50), nullable=False)
    email = db.Column(db.String(100))
    whatsapp_opt_in = db.Column(db.Boolean, nullable=False, default=False, server_default=db.false())
    birth_date = db.Column(db.Date)
    gender = db.Column(db.String(20))
    medical_history = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=utcnow_naive)
    appointments = db.relationship("Appointment", backref="patient", cascade="all, delete-orphan")
    records = db.relationship("ClinicalRecord", backref="patient", cascade="all, delete-orphan",
                              order_by="ClinicalRecord.record_date")
    __table_args__ = (UniqueConstraint("account_id", "dni", name="uq_patient_account_dni"),)

    @property
    def full_name(self):
        return f"{self.first_name} {self.last_name}"


class Service(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    account_id = db.Column(db.Integer, db.ForeignKey("professional_account.id"), nullable=False, index=True)
    name = db.Column(db.String(100), nullable=False)
    duration_minutes = db.Column(db.Integer, nullable=False, default=45)
    price = db.Column(db.Numeric(10, 2), nullable=False)
    description = db.Column(db.Text, default="")
    is_online = db.Column(db.Boolean, default=False)
    is_active = db.Column(db.Boolean, default=True)


class DoctorSchedule(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    account_id = db.Column(db.Integer, db.ForeignKey("professional_account.id"), nullable=False, index=True)
    day_of_week = db.Column(db.Integer, nullable=False)  # 0=Lunes
    start_time = db.Column(db.Time, nullable=False)
    end_time = db.Column(db.Time, nullable=False)
    slot_duration = db.Column(db.Integer, default=45)


class BlockedDay(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    account_id = db.Column(db.Integer, db.ForeignKey("professional_account.id"), nullable=False, index=True)
    day = db.Column(db.Date, nullable=False)
    reason = db.Column(db.String(100))
    __table_args__ = (UniqueConstraint("account_id", "day", name="uq_blocked_day_account_day"),)


class Appointment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    account_id = db.Column(db.Integer, db.ForeignKey("professional_account.id"), nullable=False, index=True)
    patient_id = db.Column(db.Integer, db.ForeignKey("patient.id"), nullable=False)
    service_id = db.Column(db.Integer, db.ForeignKey("service.id"))
    appointment_date = db.Column(db.DateTime, nullable=False)
    status = db.Column(db.String(30), default="pendiente")
    payment_status = db.Column(db.String(30), default="pendiente")
    deposit_amount = db.Column(db.Numeric(10, 2), default=0, nullable=False, server_default="0")
    deposit_due_at = db.Column(db.DateTime)
    google_event_id = db.Column(db.String(200))
    notes = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=utcnow_naive)
    service = db.relationship("Service")
    __table_args__ = (
        Index(
            "uq_appointment_active_slot",
            "account_id",
            "appointment_date",
            unique=True,
            sqlite_where=db.text("status != 'cancelado'"),
            postgresql_where=db.text("status != 'cancelado'"),
        ),
    )


class ClinicSettings(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    account_id = db.Column(db.Integer, db.ForeignKey("professional_account.id"), nullable=False)
    site_title = db.Column(db.String(120), nullable=False, default="Consultorio de nutrición")
    site_description = db.Column(
        db.Text,
        nullable=False,
        default=(
            "Elegí el tipo de consulta, el día y el horario que mejor te quede. "
            "Todas las opciones incluyen acompañamiento continuo por WhatsApp, "
            "infografías y recetario."
        ),
    )
    deposit_amount = db.Column(db.Numeric(10, 2), nullable=False, default=0)
    payment_alias = db.Column(db.String(100), nullable=False, default="")
    payment_instructions = db.Column(db.Text, nullable=False, default="")
    practitioner_email = db.Column(db.String(120), nullable=False, default="")
    __table_args__ = (UniqueConstraint("account_id", name="uq_clinic_settings_account_id"),)


class ReminderLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    account_id = db.Column(db.Integer, db.ForeignKey("professional_account.id"), nullable=False, index=True)
    appointment_id = db.Column(db.Integer, db.ForeignKey("appointment.id"), nullable=False)
    reminder_type = db.Column(db.String(30), nullable=False)
    sent_at = db.Column(db.DateTime, nullable=False, default=utcnow_naive)
    __table_args__ = (db.UniqueConstraint("appointment_id", "reminder_type"),)


class GoogleToken(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    account_id = db.Column(db.Integer, db.ForeignKey("professional_account.id"), nullable=False)
    authorized_email = db.Column(db.String(120), nullable=False)
    encrypted_token = db.Column(db.Text, nullable=False)
    __table_args__ = (UniqueConstraint("account_id", name="uq_google_token_account_id"),)


class ProfessionalAccount(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    google_sub = db.Column(db.String(255), nullable=True, unique=True)
    email = db.Column(db.String(254), nullable=False, index=True)
    password_hash = db.Column(db.String(255))
    display_name = db.Column(db.String(120), nullable=False)
    slug = db.Column(db.String(160), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow_naive)
    __table_args__ = (
        UniqueConstraint("slug", name="uq_professional_account_slug"),
        UniqueConstraint("email", name="uq_professional_account_email"),
    )


class BookingEmailCode(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    account_id = db.Column(db.Integer, db.ForeignKey("professional_account.id"), nullable=False, index=True)
    email = db.Column(db.String(100), nullable=False, index=True)
    code_hash = db.Column(db.String(64), nullable=False)
    expires_at = db.Column(db.DateTime, nullable=False)
    attempts = db.Column(db.Integer, nullable=False, default=0)
    consumed_at = db.Column(db.DateTime)


oauth = OAuth(app)
if os.environ.get("GOOGLE_CLIENT_ID") and os.environ.get("GOOGLE_CLIENT_SECRET"):
    google_calendar = oauth.register(
        name="google_calendar",
        client_id=os.environ["GOOGLE_CLIENT_ID"],
        client_secret=os.environ["GOOGLE_CLIENT_SECRET"],
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_kwargs={"scope": "openid email profile https://www.googleapis.com/auth/calendar.events"},
    )
else:
    google_calendar = None


def token_cipher():
    key = base64.urlsafe_b64encode(hashlib.sha256(app.config["SECRET_KEY"].encode()).digest())
    return Fernet(key)


@app.after_request
def add_security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    return response


class ClinicalRecord(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    account_id = db.Column(db.Integer, db.ForeignKey("professional_account.id"), nullable=False, index=True)
    appointment_id = db.Column(db.Integer, db.ForeignKey("appointment.id"))
    patient_id = db.Column(db.Integer, db.ForeignKey("patient.id"), nullable=False)
    record_date = db.Column(db.Date, default=lambda: datetime.now(ARGENTINA_TZ).date())
    weight_kg = db.Column(db.Float)
    height_cm = db.Column(db.Float)
    bmi = db.Column(db.Float)
    fat_percentage = db.Column(db.Float)
    muscle_percentage = db.Column(db.Float)
    visceral_fat = db.Column(db.Integer)
    waist_cm = db.Column(db.Float)
    hip_cm = db.Column(db.Float)
    arm_cm = db.Column(db.Float)
    thigh_cm = db.Column(db.Float)
    tricipital_mm = db.Column(db.Float)
    subscapular_mm = db.Column(db.Float)
    suprailiac_mm = db.Column(db.Float)
    abdominal_mm = db.Column(db.Float)
    diet_plan_notes = db.Column(db.Text)
    next_goals = db.Column(db.Text)


TENANT_MODELS = (
    Patient, Service, DoctorSchedule, BlockedDay, Appointment, ClinicSettings,
    ReminderLog, GoogleToken, BookingEmailCode, ClinicalRecord,
)
TENANT_TABLE_NAMES = (
    "patient", "service", "doctor_schedule", "blocked_day", "appointment",
    "clinic_settings", "reminder_log", "google_token", "booking_email_code",
    "clinical_record",
)


@event.listens_for(SQLAlchemySession, "do_orm_execute")
def limit_request_queries_to_professional(execute_state):
    if not has_request_context() or not execute_state.is_select or g.get("tenant_query_unscoped"):
        return
    account_id = g.get("professional_account_id") or -1
    for model in TENANT_MODELS:
        execute_state.statement = execute_state.statement.options(
            with_loader_criteria(
                model,
                lambda entity: entity.account_id == account_id,
                include_aliases=True,
            )
        )


@event.listens_for(SQLAlchemySession, "before_flush")
def populate_new_record_account_id(session, flush_context, instances):
    if not has_request_context():
        return
    account_id = g.get("professional_account_id")
    if account_id is None:
        return
    for record in session.new:
        if isinstance(record, TENANT_MODELS) and record.account_id is None:
            record.account_id = account_id


@app.before_request
def set_request_professional():
    g.professional_account_id = None
    slug = (request.view_args or {}).get("slug")
    if slug:
        account = ProfessionalAccount.query.filter_by(slug=slug).first()
        if account is None or account.google_sub == "__legacy__":
            abort(404)
        g.professional_account_id = account.id
        return
    account_id = session.get("admin_account_id")
    if account_id:
        account = db.session.get(ProfessionalAccount, account_id)
        if account is None or account.google_sub == "__legacy__":
            session.clear()
        else:
            g.professional_account_id = account.id


def current_account_id():
    account_id = getattr(g, "professional_account_id", None) if has_request_context() else None
    if account_id is None:
        raise RuntimeError("No hay una cuenta profesional activa para esta operación.")
    return account_id


def current_professional():
    account_id = current_account_id()
    account = db.session.get(ProfessionalAccount, account_id)
    if account is None:
        raise RuntimeError("La cuenta profesional activa ya no existe.")
    return account


def professional_slug(display_name, account_id):
    normalized = unicodedata.normalize("NFKD", display_name)
    ascii_name = normalized.encode("ascii", "ignore").decode("ascii").casefold()
    base_slug = re.sub(r"[^a-z0-9]+", "-", ascii_name).strip("-") or "profesional"
    return f"{base_slug[:130].rstrip('-')}-{account_id}"


def settings_for_account(account_id):
    settings = ClinicSettings.query.filter_by(account_id=account_id).first()
    return settings


# ---------- Lógica de turnos ----------
def free_slots(
    day, account_id, now=None, exclude_appointment_id=None, duration_minutes=None
):
    """Horarios libres de un día: respeta feriados/bloqueos, turnos tomados y horas pasadas."""
    now = now or datetime.now(ARGENTINA_TZ).replace(tzinfo=None)
    if BlockedDay.query.filter_by(account_id=account_id, day=day).first():
        return []
    schedules = DoctorSchedule.query.filter_by(account_id=account_id, day_of_week=day.weekday()).order_by(DoctorSchedule.start_time).all()
    start, end = datetime.combine(day, time.min), datetime.combine(day, time.max)
    booked_query = Appointment.query.filter(
        Appointment.account_id == account_id,
        Appointment.appointment_date.between(start, end),
        Appointment.status != "cancelado")
    if exclude_appointment_id is not None:
        booked_query = booked_query.filter(Appointment.id != exclude_appointment_id)
    booked = [
        (
            appointment.appointment_date,
            appointment.appointment_date + timedelta(
                minutes=appointment.service.duration_minutes if appointment.service else 45
            ),
        )
        for appointment in booked_query
    ]
    slots = []
    for s in schedules:
        cur, stop = datetime.combine(day, s.start_time), datetime.combine(day, s.end_time)
        step = timedelta(minutes=s.slot_duration)
        appointment_duration = timedelta(minutes=duration_minutes or s.slot_duration)
        while cur + appointment_duration <= stop:
            candidate_end = cur + appointment_duration
            if cur > now and all(candidate_end <= booked_start or cur >= booked_end
                                 for booked_start, booked_end in booked):
                slots.append(cur.strftime("%H:%M"))
            cur += step
    return slots


def argentina_today():
    return datetime.now(ARGENTINA_TZ).date()


def parse_money(value):
    amount = Decimal(value or "0")
    if not amount.is_finite() or amount < 0 or amount >= Decimal("100000000"):
        raise ValueError("El importe debe ser positivo y menor a 100.000.000.")
    return amount.quantize(Decimal("0.01"))


def current_argentina_time():
    return datetime.now(ARGENTINA_TZ).replace(tzinfo=None)


def clinic_settings():
    account_id = current_account_id()
    settings = settings_for_account(account_id)
    if settings is None:
        settings = ClinicSettings(account_id=account_id)
        db.session.add(settings)
        db.session.commit()
    return settings


def deposit_message(appointment):
    settings = settings_for_account(appointment.account_id)
    if not appointment.deposit_amount:
        return ""
    due = appointment.deposit_due_at.strftime("%d/%m/%Y %H:%M") if appointment.deposit_due_at else "dentro de las 24 horas"
    return (
        f"Seña: ${appointment.deposit_amount:,.0f} por transferencia.\n"
        f"Alias: {settings.payment_alias if settings else 'consultorio'}.\n"
        f"{settings.payment_instructions if settings else ''}\n"
        f"El plazo vence el {due}; si no se acredita para entonces, el turno se cancela automáticamente.\n\n"
    )


def appointment_token(appointment):
    return URLSafeTimedSerializer(app.config["SECRET_KEY"], salt="appointment-management").dumps(
        {"appointment_id": appointment.id}
    )


def send_practitioner_notification(appointment):
    settings = settings_for_account(appointment.account_id)
    if not settings or not settings.practitioner_email:
        return
    message = EmailMessage()
    message["Subject"] = f"Nuevo turno: {appointment.patient.full_name}"
    message["From"] = (os.environ.get("SMTP_SENDER") or os.environ.get("SMTP_USERNAME") or "").strip()
    message["To"] = settings.practitioner_email
    message.set_content(
        f"Se reservó un nuevo turno.\n\n"
        f"Paciente: {appointment.patient.full_name}\n"
        f"Fecha: {appointment.appointment_date.strftime('%d/%m/%Y %H:%M')} (Argentina)\n"
        f"Consulta: {appointment.service.name if appointment.service else 'Consulta'}\n"
        f"Seña: ${appointment.deposit_amount:,.0f}\n"
    )
    if not message["From"]:
        raise RuntimeError("Configurá SMTP_SENDER o SMTP_USERNAME para notificar al profesional.")
    send_message(message)


def send_message(message):
    host = (os.environ.get("SMTP_HOST") or "smtp.gmail.com").strip()
    port = int(os.environ.get("SMTP_PORT", "587"))
    use_ssl = os.environ.get("SMTP_USE_SSL", "").strip().lower() in ("1", "true", "yes", "y")
    username = (os.environ.get("SMTP_USERNAME") or "").strip()
    password = os.environ.get("SMTP_PASSWORD") or ""
    if username and not password:
        raise RuntimeError("SMTP_USERNAME está configurado pero falta SMTP_PASSWORD.")
    if use_ssl:
        connection = smtplib.SMTP_SSL(host, port, timeout=15, context=ssl.create_default_context())
    else:
        connection = smtplib.SMTP(host, port, timeout=15)
    with connection as smtp:
        if not use_ssl:
            smtp.starttls(context=ssl.create_default_context())
        if username:
            smtp.login(username, password)
        smtp.send_message(message)


def booking_phone_rate_key():
    payload = request.get_json(silent=True)
    if isinstance(payload, dict):
        phone = payload.get("phone", "")
        if isinstance(phone, str) and phone.strip():
            digits_only = re.sub(r"\D", "", phone)
            if digits_only:
                return hashlib.sha256(digits_only.encode()).hexdigest()
    return get_remote_address()


def sync_google_calendar(appointment, delete=False):
    credential = GoogleToken.query.filter_by(account_id=appointment.account_id).first()
    if credential is None:
        return
    cipher = token_cipher()
    token = json.loads(cipher.decrypt(credential.encrypted_token.encode()).decode())
    if token.get("expires_at", 0) <= datetime.now().timestamp() + 60:
        response = requests.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id": os.environ.get("GOOGLE_CLIENT_ID", ""),
                "client_secret": os.environ.get("GOOGLE_CLIENT_SECRET", ""),
                "refresh_token": token.get("refresh_token", ""),
                "grant_type": "refresh_token",
            },
            timeout=15,
        )
        response.raise_for_status()
        refreshed = response.json()
        token.update(refreshed)
        token["expires_at"] = datetime.now().timestamp() + refreshed.get("expires_in", 3600)
        credential.encrypted_token = cipher.encrypt(json.dumps(token).encode()).decode()
        db.session.commit()
    event_url = "https://www.googleapis.com/calendar/v3/calendars/primary/events"
    headers = {"Authorization": f"Bearer {token['access_token']}"}
    if delete:
        if not appointment.google_event_id:
            return
        response = requests.delete(
            f"{event_url}/{quote(appointment.google_event_id, safe='')}",
            headers=headers,
            timeout=15,
        )
        if response.status_code not in (404, 410):
            response.raise_for_status()
        appointment.google_event_id = None
        db.session.commit()
        return

    start = appointment.appointment_date
    duration = appointment.service.duration_minutes if appointment.service else 45
    event = {
        "summary": appointment.service.name if appointment.service else "Consulta nutricional",
        "description": f"Turno de {appointment.patient.full_name}",
        "start": {
            "dateTime": start.isoformat() + "-03:00",
            "timeZone": "America/Argentina/Buenos_Aires",
        },
        "end": {
            "dateTime": (start + timedelta(minutes=duration)).isoformat() + "-03:00",
            "timeZone": "America/Argentina/Buenos_Aires",
        },
    }
    if appointment.google_event_id:
        response = requests.patch(
            f"{event_url}/{quote(appointment.google_event_id, safe='')}",
            params={"sendUpdates": "none"},
            headers=headers,
            json=event,
            timeout=15,
        )
    else:
        response = requests.post(
            event_url,
            params={"sendUpdates": "none"},
            headers=headers,
            json=event,
            timeout=15,
        )
    response.raise_for_status()
    if not appointment.google_event_id:
        appointment.google_event_id = response.json()["id"]
        db.session.commit()


def send_whatsapp_template(phone, template_name, parameters):
    access_token = os.environ.get("WHATSAPP_ACCESS_TOKEN")
    phone_number_id = os.environ.get("WHATSAPP_PHONE_NUMBER_ID")
    if not access_token or not phone_number_id or not template_name:
        raise RuntimeError("Configurá WHATSAPP_ACCESS_TOKEN, WHATSAPP_PHONE_NUMBER_ID y las plantillas aprobadas.")
    number = re.sub(r"\D", "", phone or "")
    if not number or len(number) < 8 or len(number) > 15:
        raise ValueError("El teléfono no tiene un formato internacional válido para WhatsApp.")
    payload = {
        "messaging_product": "whatsapp",
        "to": number,
        "type": "template",
        "template": {
            "name": template_name,
            "language": {"code": os.environ.get("WHATSAPP_TEMPLATE_LANGUAGE", "es_AR")},
            "components": [{"type": "body", "parameters": [
                {"type": "text", "text": str(value)} for value in parameters
            ]}],
        },
    }
    version = os.environ.get("WHATSAPP_API_VERSION", "v22.0")
    response = requests.post(
        f"https://graph.facebook.com/{version}/{phone_number_id}/messages",
        headers={"Authorization": f"Bearer {access_token}"},
        json=payload,
        timeout=15,
    )
    response.raise_for_status()


def process_automations():
    now = current_argentina_time()
    BookingEmailCode.query.filter(
        db.or_(BookingEmailCode.expires_at <= now, BookingEmailCode.consumed_at.isnot(None))
    ).delete(synchronize_session=False)
    expired = Appointment.query.filter(
        Appointment.status == "pendiente",
        Appointment.payment_status == "pendiente",
        Appointment.deposit_amount > 0,
        Appointment.deposit_due_at <= now,
    ).all()
    for appointment in expired:
        appointment.status = "cancelado"
    db.session.commit()
    for appointment in expired:
        try:
            sync_google_calendar(appointment, delete=True)
        except (requests.RequestException, RuntimeError, KeyError, ValueError, InvalidToken):
            app.logger.exception("No se pudo quitar el turno %s del calendario", appointment.id)
        if not appointment.patient.whatsapp_opt_in:
            continue
        try:
            send_whatsapp_template(
                appointment.patient.phone,
                os.environ.get("WHATSAPP_DEPOSIT_CANCEL_TEMPLATE", ""),
                [appointment.patient.first_name, appointment.appointment_date.strftime("%d/%m/%Y"),
                 appointment.appointment_date.strftime("%H:%M")],
            )
        except (requests.RequestException, RuntimeError, ValueError):
            app.logger.exception("No se pudo notificar la cancelación automática del turno %s", appointment.id)

    reminders = (("24h", 24, 60), ("2h", 2, 20))
    for appointment in Appointment.query.filter(
        Appointment.status.in_(("pendiente", "confirmado")),
        Appointment.appointment_date > now,
    ).all():
        if not appointment.patient.whatsapp_opt_in:
            continue
        for reminder_type, hours, window_minutes in reminders:
            due_at = appointment.appointment_date - timedelta(hours=hours)
            if not now <= due_at <= now + timedelta(minutes=window_minutes):
                continue
            if ReminderLog.query.filter_by(appointment_id=appointment.id, reminder_type=reminder_type).first():
                continue
            try:
                send_whatsapp_template(
                    appointment.patient.phone,
                    os.environ.get("WHATSAPP_APPOINTMENT_TEMPLATE", ""),
                    [appointment.patient.first_name, appointment.appointment_date.strftime("%d/%m/%Y"),
                     appointment.appointment_date.strftime("%H:%M"),
                     appointment.service.name if appointment.service else "Consulta"],
                )
            except (requests.RequestException, RuntimeError, ValueError):
                app.logger.exception("No se pudo enviar recordatorio %s del turno %s", reminder_type, appointment.id)
            else:
                db.session.add(ReminderLog(
                    account_id=appointment.account_id,
                    appointment_id=appointment.id,
                    reminder_type=reminder_type,
                ))
                db.session.commit()



def monthly_followups_due():
    now = current_argentina_time()
    completed = Appointment.query.filter_by(status="completado").order_by(
        Appointment.appointment_date.desc()
    ).all()
    latest_by_patient = {}
    for appointment in completed:
        latest_by_patient.setdefault(appointment.patient_id, appointment)

    upcoming_patient_ids = {
        patient_id for (patient_id,) in db.session.query(Appointment.patient_id).filter(
            Appointment.appointment_date > now,
            Appointment.status.in_(("pendiente", "confirmado")),
        ).all()
    }
    handled_appointment_ids = {
        appointment_id for (appointment_id,) in db.session.query(
            ReminderLog.appointment_id
        ).filter(
            ReminderLog.reminder_type.in_(("monthly", "monthly_manual"))
        ).all()
    }
    return [
        appointment for appointment in latest_by_patient.values()
        if appointment.patient.whatsapp_opt_in
        and appointment.patient_id not in upcoming_patient_ids
        and appointment.id not in handled_appointment_ids
        and appointment.appointment_date + timedelta(days=30) <= now
    ]


def create_database_backup(account_id=None):
    if db.engine.dialect.name != "sqlite":
        raise RuntimeError("La descarga y copia automática de respaldo está implementada para SQLite.")
    backup_dir = Path(os.environ.get("BACKUP_DIR", app.instance_path + os.sep + "backups")).resolve()
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(ARGENTINA_TZ).strftime("%Y%m%d-%H%M%S-%f")
    suffix = f"-account-{account_id}" if account_id is not None else ""
    backup_path = backup_dir / f"nutri-{stamp}{suffix}.db"
    source = db.engine.raw_connection()
    try:
        target = sqlite3.connect(str(backup_path))
        try:
            source.backup(target)
            if account_id is not None:
                target.execute("PRAGMA foreign_keys = OFF")
                for table in TENANT_TABLE_NAMES:
                    target.execute(f"DELETE FROM {table} WHERE account_id != ?", (account_id,))
                target.execute(
                    "DELETE FROM professional_account WHERE id != ?", (account_id,)
                )
                target.commit()
        finally:
            target.close()
    finally:
        source.close()
    cutoff = datetime.now().timestamp() - 14 * 24 * 60 * 60
    for old_backup in backup_dir.glob("nutri-*.db"):
        if (re.fullmatch(r"nutri-\d{8}-\d{6}-\d{6}(?:-account-\d+)?\.db", old_backup.name)
                and old_backup != backup_path and old_backup.stat().st_mtime < cutoff):
            old_backup.unlink()
    return backup_path


def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("admin_account_id") or not getattr(g, "professional_account_id", None):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return wrapper


@app.route("/admin/respaldo")
@login_required
def download_backup():
    try:
        path = create_database_backup(account_id=current_account_id())
    except (OSError, RuntimeError, sqlite3.Error):
        app.logger.exception("No se pudo generar el respaldo de la base")
        abort(500, "No se pudo generar el respaldo.")
    return send_file(path, as_attachment=True, download_name=path.name)


@app.route("/p/<slug>/api/available-slots")
@limiter.limit("30 per minute")
def available_slots(slug):
    try:
        day = datetime.strptime(request.args["date"], "%Y-%m-%d").date()
    except (KeyError, ValueError):
        return jsonify(error="Ingresá una fecha válida."), 400
    if day < argentina_today() or day > argentina_today() + timedelta(days=180):
        return jsonify([])
    duration_minutes = None
    if "service_id" in request.args:
        service_id = request.args.get("service_id", type=int)
        service = db.session.get(Service, service_id) if service_id else None
        if service is None:
            return jsonify(error="Elegí un tipo de consulta válido."), 400
        duration_minutes = service.duration_minutes
    return jsonify(free_slots(
        day, current_account_id(), duration_minutes=duration_minutes
    ))


@app.route("/p/<slug>/api/book", methods=["POST"])
@limiter.limit("5 per hour")
@limiter.limit("3 per hour", key_func=booking_phone_rate_key)
def book(slug):
    d = request.get_json(silent=True)
    if not isinstance(d, dict):
        return jsonify(error="Solicitud inválida."), 400
    if d.get("website"):
        return jsonify(error="No se pudo procesar la solicitud."), 400
    try:
        when = datetime.strptime(f"{d['date']} {d['time']}", "%Y-%m-%d %H:%M")
        service_id = int(d["service_id"])
    except (KeyError, TypeError, ValueError):
        return jsonify(error="Fecha u hora inválida."), 400
    if when.date() < argentina_today() or when.date() > argentina_today() + timedelta(days=180):
        return jsonify(error="Elegí una fecha dentro de los próximos seis meses."), 400
    service = db.session.get(Service, service_id)
    if service is None or not service.is_active:
        return jsonify(error="El tipo de consulta ya no está disponible."), 400
    account_id = current_account_id()
    if d["time"] not in free_slots(
        when.date(), account_id, duration_minutes=service.duration_minutes
    ):
        return jsonify(error="Ese horario ya no está disponible. Elegí otro."), 409
    if not all(isinstance(d.get(k), str) and d[k].strip() for k in ("first_name", "last_name", "phone")):
        return jsonify(error="Completá nombre, apellido y WhatsApp."), 400
    first_name, last_name = d["first_name"].strip(), d["last_name"].strip()
    phone = d["phone"].strip()
    whatsapp_opt_in = d.get("whatsapp_opt_in") is True
    dni_value = d.get("dni", "")
    if not isinstance(dni_value, str):
        return jsonify(error="El DNI ingresado no es válido."), 400
    if len(first_name) > 100 or len(last_name) > 100 or len(phone) > 50 or len(dni_value) > 20:
        return jsonify(error="Revisá la longitud de los datos ingresados."), 400
    phone_e164 = re.sub(r"[\s().-]", "", phone)
    if not re.fullmatch(r"\+[1-9]\d{7,14}", phone_e164):
        return jsonify(error="Ingresá tu WhatsApp con código internacional, por ejemplo +5491112345678."), 400
    dni = dni_value.strip() or None if isinstance(dni_value, str) else None
    patient = Patient.query.filter_by(dni=dni).first() if dni else None
    if not patient:
        patient = Patient(first_name=first_name, last_name=last_name,
                          dni=dni, phone=phone,
                          whatsapp_opt_in=whatsapp_opt_in)
        db.session.add(patient)
        db.session.flush()
    else:
        patient.first_name, patient.last_name, patient.phone = first_name, last_name, phone
        patient.whatsapp_opt_in = whatsapp_opt_in
    settings = clinic_settings()
    deposit_amount = settings.deposit_amount or 0
    appointment = Appointment(
        patient_id=patient.id,
        service_id=service.id,
        appointment_date=when,
        deposit_amount=deposit_amount,
        deposit_due_at=current_argentina_time() + timedelta(hours=24) if deposit_amount else None,
    )
    db.session.add(appointment)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return jsonify(error="Ese horario acaba de ser reservado. Elegí otro."), 409
    try:
        sync_google_calendar(appointment)
    except (requests.RequestException, RuntimeError, KeyError, ValueError, InvalidToken):
        app.logger.exception("No se pudo agregar el turno %s a Google Calendar", appointment.id)
    try:
        send_practitioner_notification(appointment)
    except (OSError, RuntimeError, smtplib.SMTPException, ValueError):
        app.logger.exception("No se pudo notificar el nuevo turno %s al profesional", appointment.id)
    return jsonify(
        ok=True,
        manage_url=url_for("manage_appointment", token=appointment_token(appointment)),
    )


# ---------- Público ----------
@app.route("/")
def index():
    professionals = ProfessionalAccount.query.filter(
        db.or_(
            ProfessionalAccount.google_sub.is_(None),
            ProfessionalAccount.google_sub != "__legacy__",
        )
    ).order_by(ProfessionalAccount.display_name).all()
    return render_template(
        "directory.html",
        professionals=professionals,
        professional_branding=PROFESSIONAL_BRANDING,
    )


@app.route("/p/<slug>")
def public_booking(slug):
    services = Service.query.filter_by(is_active=True).order_by(Service.name).all()
    settings = settings_for_account(current_account_id())
    if settings is None:
        abort(404)
    return render_template(
        "book.html", services=services, today=argentina_today().isoformat(),
        settings=settings, slug=slug,
        branding=PROFESSIONAL_BRANDING.get(slug),
    )


@app.route("/turno/<token>", methods=["GET", "POST"])
@limiter.limit("20 per hour")
def manage_appointment(token):
    serializer = URLSafeTimedSerializer(app.config["SECRET_KEY"], salt="appointment-management")
    try:
        payload = serializer.loads(token, max_age=60 * 60 * 24 * 365)
    except SignatureExpired:
        abort(410, "El enlace para administrar este turno venció.")
    except BadSignature:
        abort(404)
    g.tenant_query_unscoped = True
    try:
        appointment = db.session.get(Appointment, payload.get("appointment_id"))
    finally:
        g.tenant_query_unscoped = False
    if appointment is None:
        abort(404)
    g.professional_account_id = appointment.account_id
    if request.method == "POST":
        action = request.form.get("action")
        if appointment.status in ("cancelado", "completado", "no_asistio"):
            flash("Este turno ya no se puede modificar.")
        elif action == "cancel":
            appointment.status = "cancelado"
            db.session.commit()
            try:
                sync_google_calendar(appointment, delete=True)
            except (requests.RequestException, RuntimeError, KeyError, ValueError, InvalidToken):
                app.logger.exception("No se pudo quitar el turno %s del calendario", appointment.id)
            flash("El turno fue cancelado.")
        elif action == "reschedule":
            try:
                new_date = datetime.strptime(request.form.get("date", ""), "%Y-%m-%d").date()
                new_time = datetime.strptime(request.form.get("time", ""), "%H:%M").time()
            except ValueError:
                flash("Elegí una fecha y hora válidas.")
                return redirect(url_for("manage_appointment", token=token))
            new_datetime = datetime.combine(new_date, new_time)
            if new_datetime <= current_argentina_time() or request.form.get("time") not in free_slots(
                new_date, appointment.account_id,
                exclude_appointment_id=appointment.id,
                duration_minutes=appointment.service.duration_minutes if appointment.service else 45,
            ):
                flash("Ese horario no está disponible.")
                return redirect(url_for("manage_appointment", token=token))
            ReminderLog.query.filter_by(appointment_id=appointment.id).delete(synchronize_session=False)
            appointment.appointment_date = new_datetime
            try:
                db.session.commit()
            except IntegrityError:
                db.session.rollback()
                flash("Ese horario acaba de ser reservado. Elegí otro.")
                return redirect(url_for("manage_appointment", token=token))
            try:
                sync_google_calendar(appointment)
            except (requests.RequestException, RuntimeError, KeyError, ValueError, InvalidToken):
                app.logger.exception("No se pudo actualizar el evento del turno %s", appointment.id)
            flash("El turno fue reprogramado.")
        return redirect(url_for("manage_appointment", token=token))
    professional = db.session.get(ProfessionalAccount, appointment.account_id)
    if professional is None:
        abort(404)
    return render_template("manage.html", appointment=appointment, token=token,
                           slug=professional.slug,
                           today=argentina_today().isoformat())


@app.route("/admin/google/calendar/connect")
@login_required
def google_calendar_connect():
    if google_calendar is None:
        abort(503, "Configurá las credenciales OAuth de Google para habilitar Calendar.")
    callback = url_for("google_calendar_callback", _external=True)
    return google_calendar.authorize_redirect(callback, access_type="offline", prompt="consent")


@app.route("/admin/google/calendar/callback")
@login_required
def google_calendar_callback():
    if google_calendar is None:
        abort(503)
    token = google_calendar.authorize_access_token()
    profile = google_calendar.get("userinfo").json()
    email = profile.get("email", "").strip().casefold()
    if profile.get("email_verified") is not True or not email:
        abort(403, "Google debe confirmar una dirección de correo verificada.")
    account_id = current_account_id()
    credential = GoogleToken.query.filter_by(account_id=account_id).first()
    if credential is None:
        credential = GoogleToken(account_id=account_id, authorized_email=email)
        db.session.add(credential)
    else:
        credential.authorized_email = email
    credential.encrypted_token = token_cipher().encrypt(json.dumps(token).encode()).decode()
    db.session.commit()
    flash("Google Calendar quedó conectado.")
    return redirect(url_for("config"))


@app.route("/service-worker.js")
def service_worker():
    response = send_from_directory(app.static_folder, "service-worker.js", mimetype="application/javascript")
    response.headers["Service-Worker-Allowed"] = "/"
    response.headers["Cache-Control"] = "no-cache"
    return response


# ---------- Admin ----------
@app.route("/admin/login", methods=["GET", "POST"])
@limiter.limit("5 per 15 minutes", methods=["POST"])
def login():
    nxt = request.form.get("next") or request.args.get("next") or ""
    safe = nxt if nxt.startswith("/admin") and not nxt.startswith("//") else None
    if request.method == "POST":
        email = request.form.get("email", "").strip().casefold()
        password = request.form.get("password", "")
        account = ProfessionalAccount.query.filter_by(email=email).first()
        if (account is None or account.google_sub == "__legacy__"
                or not account.password_hash
                or not check_password_hash(account.password_hash, password)):
            flash("Email o contraseña incorrectos.")
            return redirect(url_for("login", next=safe) if safe else url_for("login"))
        session.clear()
        session.permanent = True
        session["admin"] = True
        session["admin_email"] = account.email
        session["admin_account_id"] = account.id
        return redirect(safe or url_for("agenda"))
    return render_template("login.html", next=safe or "")


@app.route("/admin/signup", methods=["GET", "POST"])
@limiter.limit("5 per hour", methods=["POST"])
def signup():
    if request.method == "POST":
        display_name = request.form.get("display_name", "").strip()
        email = request.form.get("email", "").strip().casefold()
        password = request.form.get("password", "")
        password_confirmation = request.form.get("password_confirmation", "")
        if not display_name or len(display_name) > 120:
            flash("Ingresá tu nombre (hasta 120 caracteres).")
            return redirect(url_for("signup"))
        if (len(email) > 254
                or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email)):
            flash("Ingresá un email válido.")
            return redirect(url_for("signup"))
        if len(password) < 8 or len(password) > 128:
            flash("La contraseña debe tener entre 8 y 128 caracteres.")
            return redirect(url_for("signup"))
        if password != password_confirmation:
            flash("Las contraseñas no coinciden.")
            return redirect(url_for("signup"))
        if ProfessionalAccount.query.filter_by(email=email).first():
            flash("Ya existe una cuenta con ese email. Ingresá o usá otro email.")
            return redirect(url_for("signup"))

        account = ProfessionalAccount(
            google_sub=None,
            email=email,
            password_hash=generate_password_hash(password),
            display_name=display_name,
            slug=f"pending-{secrets.token_hex(8)}",
        )
        try:
            db.session.add(account)
            db.session.flush()
            account.slug = professional_slug(display_name, account.id)
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            flash("No se pudo crear la cuenta. Verificá si ese email ya está registrado.")
            return redirect(url_for("signup"))

        session.clear()
        session.permanent = True
        session["admin"] = True
        session["admin_email"] = account.email
        session["admin_account_id"] = account.id
        g.professional_account_id = account.id
        seed(account.id)
        flash("Cuenta creada correctamente.")
        return redirect(url_for("agenda"))
    return render_template("signup.html")


@app.route("/admin/logout", methods=["POST"])
@login_required
def logout():
    session.clear()
    return redirect(url_for("index"))


@app.route("/admin")
@login_required
def agenda():
    try:
        day = datetime.strptime(request.args.get("date", ""), "%Y-%m-%d").date()
    except ValueError:
        day = argentina_today()
    all_items = Appointment.query.filter(
        Appointment.appointment_date.between(datetime.combine(day, time.min), datetime.combine(day, time.max))
    ).order_by(Appointment.appointment_date).all()
    status_filter = request.args.get("status", "")
    payment_filter = request.args.get("payment", "")
    if status_filter not in ESTADOS:
        status_filter = ""
    if payment_filter not in ESTADOS_PAGO:
        payment_filter = ""
    items = [a for a in all_items
             if (not status_filter or a.status == status_filter)
             and (not payment_filter or a.payment_status == payment_filter)]
    summary = {
        "total": len(all_items),
        "pending": sum(a.status == "pendiente" for a in all_items),
        "confirmed": sum(a.status == "confirmado" for a in all_items),
        "payment_pending": sum(a.payment_status == "pendiente" for a in all_items),
    }
    return render_template(
        "agenda.html", items=items, day=day, prev=day - timedelta(days=1),
        next=day + timedelta(days=1), estados=ESTADOS, estados_pago=ESTADOS_PAGO,
        dias=DIAS, today=argentina_today(), summary=summary,
        status_filter=status_filter, payment_filter=payment_filter,
        services=Service.query.filter_by(is_active=True).order_by(Service.name).all(),
        followups=monthly_followups_due(),
    )


@app.route("/admin/seguimiento/<int:appointment_id>", methods=["POST"])
@login_required
def mark_followup_contacted(appointment_id):
    appointment = db.get_or_404(Appointment, appointment_id)
    if ReminderLog.query.filter_by(
        appointment_id=appointment.id, reminder_type="monthly_manual"
    ).first():
        flash("El seguimiento ya estaba marcado como realizado.")
        return redirect(url_for("agenda"))
    if appointment.id not in {item.id for item in monthly_followups_due()}:
        abort(404)
    db.session.add(ReminderLog(
        account_id=appointment.account_id,
        appointment_id=appointment.id,
        reminder_type="monthly_manual",
    ))
    db.session.commit()
    flash("Seguimiento marcado como realizado.")
    return redirect(url_for("agenda"))


def agenda_redirect_url(form, fallback_date):
    try:
        day = datetime.strptime(form.get("return_date", ""), "%Y-%m-%d").date()
    except ValueError:
        day = fallback_date
    status = form.get("return_status", "")
    payment = form.get("return_payment", "")
    return url_for(
        "agenda",
        date=day.isoformat(),
        status=status if status in ESTADOS else "",
        payment=payment if payment in ESTADOS_PAGO else "",
    )


@app.route("/admin/turno/<int:id>", methods=["POST"])
@login_required
def update_appointment(id):
    a = db.get_or_404(Appointment, id)
    redirect_url = agenda_redirect_url(request.form, a.appointment_date.date())
    status = request.form.get("status", a.status)
    payment_status = request.form.get("payment_status", a.payment_status)
    if status not in ESTADOS or payment_status not in ESTADOS_PAGO:
        flash("No se pudieron guardar los estados seleccionados.")
        return redirect(redirect_url)
    previous_status = a.status
    a.status = status
    a.payment_status = payment_status
    db.session.commit()
    if status == "cancelado" or (previous_status == "cancelado" and status in ("pendiente", "confirmado")):
        try:
            sync_google_calendar(a, delete=status == "cancelado")
        except (requests.RequestException, RuntimeError, KeyError, ValueError, InvalidToken):
            app.logger.exception("No se pudo sincronizar el turno %s con Google Calendar", a.id)
    return redirect(redirect_url)


@app.route("/admin/turno/<int:id>/editar", methods=["POST"])
@login_required
def edit_appointment(id):
    appointment = db.get_or_404(Appointment, id)
    form = request.form
    redirect_url = agenda_redirect_url(form, appointment.appointment_date.date())

    try:
        new_date = datetime.strptime(form.get("appointment_date", ""), "%Y-%m-%d").date()
        new_time = datetime.strptime(form.get("appointment_time", ""), "%H:%M").time()
    except ValueError:
        flash("Ingresá una fecha y hora válidas.")
        return redirect(redirect_url)

    first_name = form.get("first_name", "").strip()
    last_name = form.get("last_name", "").strip()
    phone = form.get("phone", "").strip()
    email = form.get("email", "").strip() or None
    dni = form.get("dni", "").strip() or None
    if not first_name or not last_name or not phone:
        flash("Completá nombre, apellido y teléfono.")
        return redirect(redirect_url)
    if email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        flash("Ingresá un email válido o dejá el campo vacío.")
        return redirect(redirect_url)
    if len(first_name) > 100 or len(last_name) > 100 or len(phone) > 50 or (email and len(email) > 100):
        flash("Revisá la longitud de los datos del paciente.")
        return redirect(redirect_url)
    if dni and len(dni) > 20:
        flash("El DNI no puede superar los 20 caracteres.")
        return redirect(redirect_url)

    patient = appointment.patient
    if dni and Patient.query.filter(Patient.dni == dni, Patient.id != patient.id).first():
        flash("Ese DNI ya está asociado a otro paciente.")
        return redirect(redirect_url)

    service_id = form.get("service_id", type=int)
    service = db.session.get(Service, service_id) if service_id else None
    if service is None or (not service.is_active and service.id != appointment.service_id):
        flash("Elegí un tipo de consulta válido.")
        return redirect(redirect_url)

    new_datetime = datetime.combine(new_date, new_time)
    if (new_datetime != appointment.appointment_date or service.id != appointment.service_id):
        if new_time.strftime("%H:%M") not in free_slots(
            new_date, appointment.account_id,
            exclude_appointment_id=appointment.id,
            duration_minutes=service.duration_minutes,
        ):
            flash("Ese horario no está disponible. Elegí un horario libre de la agenda.")
            return redirect(redirect_url)

    patient.first_name = first_name
    patient.last_name = last_name
    patient.phone = phone
    patient.email = email
    patient.dni = dni
    if new_datetime != appointment.appointment_date:
        ReminderLog.query.filter_by(appointment_id=appointment.id).delete(synchronize_session=False)
    appointment.appointment_date = new_datetime
    appointment.service = service
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        flash("Ese horario acaba de ser reservado. Elegí otro.")
        return redirect(redirect_url)
    try:
        sync_google_calendar(appointment)
    except (requests.RequestException, RuntimeError, KeyError, ValueError, InvalidToken):
        app.logger.exception("No se pudo actualizar el evento del turno %s", appointment.id)
    flash("Turno actualizado.")
    return redirect(redirect_url)


@app.route("/admin/pacientes")
@login_required
def patients():
    q = request.args.get("q", "").strip()
    query = Patient.query
    if q:
        like = f"%{q}%"
        query = query.filter(db.or_(Patient.first_name.ilike(like), Patient.last_name.ilike(like), Patient.dni.ilike(like)))
    return render_template("patients.html", patients=query.order_by(Patient.last_name).all(), q=q)


@app.route("/admin/pacientes/<int:id>", methods=["GET", "POST"])
@login_required
def patient(id):
    p = db.get_or_404(Patient, id)
    if request.method == "POST":
        p.medical_history = request.form.get("medical_history")
        db.session.commit()
        flash("Datos guardados.")
        return redirect(url_for("patient", id=id))
    return render_template("patient.html", p=p, now_date=argentina_today().isoformat())


@app.route("/admin/pacientes/<int:id>/medicion", methods=["POST"])
@login_required
def add_record(id):
    db.get_or_404(Patient, id)
    f = request.form
    num = lambda k: float(f[k]) if f.get(k) else None
    r = ClinicalRecord(patient_id=id, record_date=datetime.strptime(f["record_date"], "%Y-%m-%d").date(),
                       diet_plan_notes=f.get("diet_plan_notes"), next_goals=f.get("next_goals"),
                       visceral_fat=int(f["visceral_fat"]) if f.get("visceral_fat") else None)
    for k in ("weight_kg", "height_cm", "fat_percentage", "muscle_percentage", "waist_cm", "hip_cm",
              "arm_cm", "thigh_cm", "tricipital_mm", "subscapular_mm", "suprailiac_mm", "abdominal_mm"):
        setattr(r, k, num(k))
    if r.weight_kg and r.height_cm:
        r.bmi = round(r.weight_kg / (r.height_cm / 100) ** 2, 1)
    db.session.add(r)
    db.session.commit()
    flash("Medición guardada.")
    return redirect(url_for("patient", id=id) + "#evolucion")


@app.route("/admin/config", methods=["GET", "POST"])
@login_required
def config():
    if request.method == "POST":
        f = request.form
        if f.get("action") == "save_site":
            settings = clinic_settings()
            try:
                deposit_amount = parse_money(f.get("deposit_amount", "0"))
            except (InvalidOperation, ValueError):
                flash("Ingresá un importe válido para la seña.")
                return redirect(url_for("config"))
            title = f.get("site_title", "").strip()
            description = f.get("site_description", "").strip()
            alias = f.get("payment_alias", "").strip()
            instructions = f.get("payment_instructions", "").strip()
            practitioner_email = f.get("practitioner_email", "").strip()
            if (not title or not description or len(title) > 120 or len(description) > 3000
                    or len(alias) > 100 or len(instructions) > 3000):
                flash("Completá el título y la descripción del sitio.")
                return redirect(url_for("config"))
            if practitioner_email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", practitioner_email):
                flash("Ingresá un email profesional válido o dejá el campo vacío.")
                return redirect(url_for("config"))
            if deposit_amount and not alias:
                flash("Indicá el alias de transferencia para poder cobrar una seña.")
                return redirect(url_for("config"))
            settings.site_title = title
            settings.site_description = description
            settings.deposit_amount = deposit_amount
            settings.payment_alias = alias
            settings.payment_instructions = instructions
            settings.practitioner_email = practitioner_email
            db.session.commit()
            flash("Información pública y condiciones de seña actualizadas.")
            return redirect(url_for("config"))
        if f.get("action") in ("add_service", "edit_service"):
            name = f.get("name", "").strip()
            description = f.get("description", "").strip()
            try:
                duration = int(f.get("duration_minutes", ""))
                price = parse_money(f.get("price", ""))
            except (InvalidOperation, ValueError):
                flash("Ingresá duración y precio válidos.")
                return redirect(url_for("config"))
            if (not name or len(name) > 100 or len(description) > 2000
                    or duration < 10 or duration > 480):
                flash("Revisá el nombre, la duración (10 a 480 minutos) y el precio.")
                return redirect(url_for("config"))
            if f.get("action") == "edit_service":
                service = db.get_or_404(Service, f.get("id", type=int))
            else:
                service = Service()
                db.session.add(service)
            service.name = name
            service.description = description
            service.duration_minutes = duration
            service.price = price
            service.is_online = f.get("is_online") == "on"
            service.is_active = f.get("is_active") == "on"
            db.session.commit()
            flash("Tipo de consulta guardado.")
            return redirect(url_for("config"))
        if f.get("action") in ("add_schedule", "edit_schedule"):
            try:
                day_of_week = int(f.get("day", ""))
                start_time = datetime.strptime(f.get("start", ""), "%H:%M").time()
                end_time = datetime.strptime(f.get("end", ""), "%H:%M").time()
                slot_duration = int(f.get("slot", ""))
            except ValueError:
                flash("Ingresá un día, horario y duración válidos.")
                return redirect(url_for("config"))
            schedule_minutes = (
                datetime.combine(date.min, end_time) - datetime.combine(date.min, start_time)
            ).total_seconds() / 60
            if (not 0 <= day_of_week < len(DIAS) or start_time >= end_time
                    or not 10 <= slot_duration <= 480 or slot_duration > schedule_minutes):
                flash("Revisá el día, el rango horario y la duración (entre 10 y 480 minutos).")
                return redirect(url_for("config"))

            schedule = None
            if f["action"] == "edit_schedule":
                try:
                    schedule_id = int(f.get("id", ""))
                except ValueError:
                    flash("El horario seleccionado no es válido.")
                    return redirect(url_for("config"))
                schedule = db.get_or_404(DoctorSchedule, schedule_id)

            overlapping = DoctorSchedule.query.filter(
                DoctorSchedule.day_of_week == day_of_week,
                DoctorSchedule.start_time < end_time,
                DoctorSchedule.end_time > start_time,
            )
            if schedule:
                overlapping = overlapping.filter(DoctorSchedule.id != schedule.id)
            if overlapping.first():
                flash("Ese horario se superpone con otro bloque de atención del mismo día.")
                return redirect(url_for("config"))

            if schedule:
                schedule.day_of_week = day_of_week
                schedule.start_time = start_time
                schedule.end_time = end_time
                schedule.slot_duration = slot_duration
                flash("Horario actualizado. Revisá si los turnos futuros siguen dentro de la nueva disponibilidad.")
            else:
                db.session.add(DoctorSchedule(day_of_week=day_of_week, start_time=start_time,
                                              end_time=end_time, slot_duration=slot_duration))
                flash("Horario agregado.")
        elif f["action"] == "del_schedule":
            db.session.delete(db.get_or_404(DoctorSchedule, int(f["id"])))
        elif f["action"] == "add_block" and f.get("day"):
            day = datetime.strptime(f["day"], "%Y-%m-%d").date()
            if not BlockedDay.query.filter_by(day=day).first():
                db.session.add(BlockedDay(day=day, reason=f.get("reason")))
        elif f["action"] == "del_block":
            db.session.delete(db.get_or_404(BlockedDay, int(f["id"])))
        db.session.commit()
        return redirect(url_for("config"))
    account_id = current_account_id()
    google_token = GoogleToken.query.filter_by(account_id=account_id).first()
    return render_template("config.html", schedules=DoctorSchedule.query.order_by(
        DoctorSchedule.day_of_week, DoctorSchedule.start_time).all(),
        blocks=BlockedDay.query.order_by(BlockedDay.day).all(), dias=DIAS,
        services=Service.query.order_by(Service.name).all(),
        settings=clinic_settings(),
        google_connected=google_token is not None,
        google_calendar_email=google_token.authorized_email if google_token else None,
        google_calendar_enabled=google_calendar is not None,
        professional=current_professional())


@app.template_filter("digits")
def digits(s):
    return "".join(c for c in (s or "") if c.isdigit())


def seed(account_id):
    if not Service.query.filter_by(account_id=account_id).first():
        db.session.add_all([
            Service(
                account_id=account_id,
                name="Consulta nutricional",
                description=(
                    "Evaluación de tus hábitos, historia clínica y objetivos, con educación "
                    "nutricional y herramientas prácticas para mejorar tu alimentación. "
                    "Atención presencial o virtual."
                ),
                duration_minutes=60,
                price=30000,
            ),
            Service(
                account_id=account_id,
                name="Consulta + Plan de alimentación",
                description=(
                    "Evaluación nutricional y plan personalizado según tus necesidades, "
                    "objetivos, preferencias y estilo de vida. Atención presencial o virtual."
                ),
                duration_minutes=60,
                price=52000,
            ),
            Service(
                account_id=account_id,
                name="Consulta + Antropometría",
                description=(
                    "Consulta nutricional y evaluación de composición corporal para conocer "
                    "masa muscular, masa grasa y acompañar tu evolución. Atención presencial."
                ),
                duration_minutes=60,
                price=40000,
            ),
            Service(
                account_id=account_id,
                name="Antropometría",
                description=(
                    "Mediciones de composición corporal para registrar masa muscular, masa grasa "
                    "y otros indicadores, y hacer seguimiento. Atención presencial."
                ),
                duration_minutes=30,
                price=20000,
            ),
            Service(
                account_id=account_id,
                name="Consulta + Plan + Antropometría",
                description=(
                    "Evaluación y educación nutricional, plan personalizado y evaluación de "
                    "composición corporal para acompañar tu evolución. Atención presencial."
                ),
                duration_minutes=75,
                price=60000,
            ),
        ])
        for d in range(5):
            db.session.add(DoctorSchedule(
                account_id=account_id, day_of_week=d, start_time=time(14),
                end_time=time(20), slot_duration=45,
            ))
    if not ClinicSettings.query.filter_by(account_id=account_id).first():
        db.session.add(ClinicSettings(account_id=account_id))
    db.session.commit()


@app.cli.command("seed-data")
def seed_data_command():
    """Carga consultas, horarios y configuración iniciales si no existen."""
    accounts = ProfessionalAccount.query.filter(
        db.or_(
            ProfessionalAccount.google_sub.is_(None),
            ProfessionalAccount.google_sub != "__legacy__",
        )
    ).all()
    for account in accounts:
        seed(account.id)
    if not accounts:
        print("Los datos iniciales se crean cuando un profesional se registra.")
    else:
        print("Datos iniciales listos.")

if __name__ == "__main__":
    app.run(debug=False)
