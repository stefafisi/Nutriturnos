import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from datetime import datetime, time, timedelta
from unittest.mock import patch
from sqlalchemy.exc import IntegrityError
from werkzeug.security import check_password_hash, generate_password_hash

os.environ.setdefault("SECRET_KEY", "tests-use-a-temporary-secret-key")
os.environ.setdefault("SMTP_SENDER", "test@example.com")
os.environ.setdefault("COOKIE_SECURE", "false")
os.environ.setdefault("DATABASE_URL", "sqlite://")

from app import (
    Appointment,
    ClinicSettings,
    Patient,
    ProfessionalAccount,
    ReminderLog,
    Service,
    app,
    current_argentina_time,
    create_database_backup,
    db,
    free_slots,
    process_automations,
    seed,
)


class BookingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app.config.update(TESTING=True, WTF_CSRF_ENABLED=False, RATELIMIT_ENABLED=False)
        with app.app_context():
            db.create_all()

    def setUp(self):
        with app.app_context():
            db.drop_all()
            db.create_all()
            account = ProfessionalAccount(
                google_sub="test-professional",
                email="professional@example.com",
                display_name="Test Professional",
                slug="test-professional",
            )
            db.session.add(account)
            db.session.commit()
            self.account_id = account.id
            self.slug = account.slug
            seed(account.id)
        self.client = app.test_client()

    def tearDown(self):
        with app.app_context():
            db.session.remove()
            db.drop_all()
            db.engine.dispose()

    def next_business_day(self):
        day = current_argentina_time().date() + timedelta(days=1)
        while day.weekday() > 4:
            day += timedelta(days=1)
        return day

    def make_patient(self):
        patient = Patient(account_id=self.account_id, first_name="Ana", last_name="Prueba",
                          phone="+5491112345678", email="ana@example.com")
        db.session.add(patient)
        db.session.flush()
        return patient

    def login_as(self, client, account_id=None):
        with client.session_transaction() as browser_session:
            browser_session["admin"] = True
            browser_session["admin_account_id"] = account_id or self.account_id

    def test_default_database_url_uses_instance_directory(self):
        env = os.environ.copy()
        env.pop("DATABASE_URL", None)
        command = (
            "from app import app; "
            "print(app.config['SQLALCHEMY_DATABASE_URI'])"
        )
        default_uri = subprocess.check_output(
            [sys.executable, "-c", command],
            cwd=Path(__file__).resolve().parents[1],
            env=env,
            text=True,
        ).strip().replace("\\", "/")
        expected_path = str(
            Path(__file__).resolve().parents[1] / "instance" / "nutri.db"
        ).replace("\\", "/")
        self.assertTrue(default_uri.startswith("sqlite:///"))
        self.assertIn(expected_path, default_uri)

    def test_email_signup_creates_account_and_logs_in(self):
        response = self.client.post("/admin/signup", data={
            "display_name": "New Professional",
            "email": "new.professional@example.com",
            "password": "testpass",
            "password_confirmation": "testpass",
        }, follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Agenda", response.data)
        with app.app_context():
            account = ProfessionalAccount.query.filter_by(
                email="new.professional@example.com"
            ).one()
            self.assertIsNone(account.google_sub)
            self.assertTrue(check_password_hash(
                account.password_hash, "testpass"
            ))
            self.assertTrue(account.slug.startswith("new-professional-"))
            services = Service.query.filter_by(account_id=account.id).all()
            self.assertEqual(len(services), 5)
            self.assertEqual(
                [(service.name, int(service.price), service.duration_minutes, service.is_online)
                 for service in services],
                [
                    ("Consulta nutricional", 30000, 60, False),
                    ("Consulta + Plan de alimentación", 52000, 60, False),
                    ("Consulta + Antropometría", 40000, 60, False),
                    ("Antropometría", 20000, 30, False),
                    ("Consulta + Plan + Antropometría", 60000, 75, False),
                ],
            )
            self.assertLess(max(len(service.description) for service in services), 200)
            self.assertTrue(all("Atención presencial." in service.description for service in services))
            settings = ClinicSettings.query.filter_by(account_id=account.id).one()
            self.assertIn("acompañamiento continuo por WhatsApp", settings.site_description)
        with self.client.session_transaction() as browser_session:
            self.assertTrue(browser_session["admin"])
            self.assertEqual(browser_session["admin_email"], "new.professional@example.com")
            self.assertEqual(browser_session["admin_account_id"], account.id)
        directory = self.client.get("/")
        self.assertIn(f"/p/{account.slug}".encode(), directory.data)

    def test_signup_page_does_not_require_google_credentials(self):
        response = self.client.get("/admin/login")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Email", response.data)
        self.assertIn(b"Crear cuenta", response.data)
        self.assertEqual(self.client.get("/admin/signup").status_code, 200)

    def test_email_login_and_signup_validation(self):
        bad_signup = self.client.post("/admin/signup", data={
            "display_name": "New Professional",
            "email": "new.professional@example.com",
            "password": "short",
            "password_confirmation": "short",
        })
        self.assertEqual(bad_signup.status_code, 302)
        with app.app_context():
            self.assertIsNone(ProfessionalAccount.query.filter_by(
                email="new.professional@example.com"
            ).first())

        valid_signup = self.client.post("/admin/signup", data={
            "display_name": "Eight Character",
            "email": "eight.character@example.com",
            "password": "12345678",
            "password_confirmation": "12345678",
        })
        self.assertEqual(valid_signup.status_code, 302)
        with app.app_context():
            self.assertIsNotNone(ProfessionalAccount.query.filter_by(
                email="eight.character@example.com"
            ).first())

        with app.app_context():
            account = db.session.get(ProfessionalAccount, self.account_id)
            account.password_hash = generate_password_hash("existing-password-long")
            db.session.commit()
        login = self.client.post("/admin/login", data={
            "email": "PROFESSIONAL@example.com",
            "password": "existing-password-long",
        })
        self.assertEqual(login.status_code, 302)
        self.assertEqual(self.client.get("/admin").status_code, 200)

        bad_client = app.test_client()
        bad_login = bad_client.post("/admin/login", data={
            "email": "professional@example.com",
            "password": "wrong-password",
        })
        self.assertEqual(bad_login.status_code, 302)
        with bad_client.session_transaction() as browser_session:
            self.assertNotIn("admin_account_id", browser_session)

        unknown_client = app.test_client()
        bad_login = unknown_client.post("/admin/login", data={
            "email": "unknown@example.com",
            "password": "does-not-matter",
        })
        self.assertEqual(bad_login.status_code, 302)
        with app.app_context():
            self.assertEqual(
                ProfessionalAccount.query.filter_by(email="unknown@example.com").count(),
                0,
            )

    def test_booking_and_duplicate_slot_rejection(self):
        with app.app_context():
            service = Service.query.first()
            ClinicSettings.query.filter_by(account_id=self.account_id).one().deposit_amount = 5000
            db.session.commit()
            day = self.next_business_day()
            payload = {
                "first_name": "Ana",
                "last_name": "Prueba",
                "phone": "+5491112345678",
                "dni": "",
                "service_id": service.id,
                "date": day.isoformat(),
                "time": "14:00",
                "whatsapp_opt_in": False,
            }
        api_base = f"/p/{self.slug}/api"
        response = self.client.post(f"{api_base}/book", json=payload)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json["ok"])
        with app.app_context():
            appointment = Appointment.query.first()
            self.assertIsNone(appointment.patient.email)
            self.assertEqual(appointment.deposit_amount, 5000)
            self.assertGreater(
                appointment.deposit_due_at - current_argentina_time(),
                timedelta(hours=23, minutes=59),
            )
        duplicate = self.client.post(f"{api_base}/book", json=payload)
        self.assertEqual(duplicate.status_code, 409)

    def test_public_booking_does_not_request_patient_email(self):
        response = self.client.get(f"/p/{self.slug}")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(b'name="email"', response.data)
        self.assertNotIn(b"send-verification-code", response.data)
        self.assertIn(b"WhatsApp", response.data)
        self.assertIn(b'name="whatsapp_opt_in"', response.data)

    def test_franco_branding_only_appears_after_selecting_his_profile(self):
        response = self.client.get(f"/p/{self.slug}")
        self.assertNotIn(b"professional-branded", response.data)
        self.assertNotIn(b"Zeballos 2920", response.data)
        self.assertIn(b'<meta name="theme-color" content="#1E3B2C">', response.data)
        self.assertIn(b'/static/icon.svg', response.data)

        with app.app_context():
            account = db.session.get(ProfessionalAccount, self.account_id)
            account.slug = "franco-paporello-3"
            settings = ClinicSettings.query.filter_by(account_id=self.account_id).one()
            settings.deposit_amount = 10000
            settings.payment_alias = "francopaporello"
            settings.payment_instructions = "CVU: 0000003100053964226560"
            db.session.commit()
        self.slug = "franco-paporello-3"

        response = self.client.get(f"/p/{self.slug}")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"professional-branded", response.data)
        self.assertIn(b"Zeballos 2920 (Gimnasio Gefitness)", response.data)
        self.assertIn(b"/static/images/franco-paporello-logo.png", response.data)
        self.assertIn(
            "Para confirmar tu turno, aboná una seña de $10.000 por transferencia dentro de las 24 horas posteriores a la reserva.".encode(),
            response.data,
        )
        self.assertIn(b"francopaporello", response.data)
        self.assertIn(b"CVU: 0000003100053964226560", response.data)
        self.assertIn(b"wa.me/5493424053538", response.data)
        self.assertIn("el turno se cancelará automáticamente".encode(), response.data)
        self.assertIn("Políticas de reprogramación y cancelación".encode(), response.data)
        self.assertIn("la seña podrá ser devuelta".encode(), response.data)
        self.assertIn("No se realizará devolución de la seña".encode(), response.data)
        self.assertIn(b'<meta name="theme-color" content="#006C90">', response.data)
        self.assertIn(b'/static/images/franco-paporello-icon.png', response.data)
        logo_response = self.client.get("/static/images/franco-paporello-logo.png")
        self.assertEqual(logo_response.status_code, 200)
        logo_response.close()
        icon_response = self.client.get("/static/images/franco-paporello-icon.png")
        self.assertEqual(icon_response.status_code, 200)
        icon_response.close()
        directory_response = self.client.get("/")
        self.assertNotIn(b"professional-branded", directory_response.data)
        self.assertNotIn(b"Zeballos 2920", directory_response.data)
        self.assertIn(b"professional-card-branded", directory_response.data)
        self.assertIn(b"/static/images/franco-paporello-logo.png", directory_response.data)
        self.assertIn(b'<meta name="theme-color" content="#1E3B2C">', directory_response.data)
        self.assertIn(b"/static/icon.svg", directory_response.data)

    def test_monthly_followup_appears_in_agenda_and_can_be_marked_done(self):
        with app.app_context():
            patient = self.make_patient()
            patient.whatsapp_opt_in = True
            service = Service.query.first()
            appointment = Appointment(
                account_id=self.account_id,
                patient_id=patient.id,
                service_id=service.id,
                appointment_date=current_argentina_time() - timedelta(days=31),
                status="completado",
            )
            db.session.add(appointment)
            db.session.commit()
            appointment_id = appointment.id

        self.login_as(self.client)
        response = self.client.get("/admin")
        self.assertIn(b"Seguimientos pendientes", response.data)
        self.assertIn(b"https://wa.me/5491112345678", response.data)
        self.assertNotIn(b"https://wa.me/545491112345678", response.data)

        response = self.client.post(
            f"/admin/seguimiento/{appointment_id}", follow_redirects=True
        )
        self.assertIn(b"Seguimiento marcado como realizado.", response.data)
        self.assertNotIn(b"Seguimientos pendientes", response.data)
        response = self.client.post(
            f"/admin/seguimiento/{appointment_id}", follow_redirects=True
        )
        self.assertIn(b"El seguimiento ya estaba marcado como realizado.", response.data)
        with app.app_context():
            self.assertEqual(
                ReminderLog.query.filter_by(
                    appointment_id=appointment_id,
                    reminder_type="monthly_manual",
                ).count(),
                1,
            )

    def test_monthly_followup_excludes_patients_without_consent_or_with_future_booking(self):
        with app.app_context():
            service = Service.query.first()
            past_date = current_argentina_time() - timedelta(days=31)
            no_consent = self.make_patient()
            no_consent.first_name = "Sin"
            db.session.add(Appointment(
                account_id=self.account_id,
                patient_id=no_consent.id,
                service_id=service.id,
                appointment_date=past_date,
                status="completado",
            ))

            upcoming = self.make_patient()
            upcoming.first_name = "ConTurno"
            upcoming.whatsapp_opt_in = True
            db.session.add(Appointment(
                account_id=self.account_id,
                patient_id=upcoming.id,
                service_id=service.id,
                appointment_date=past_date - timedelta(hours=1),
                status="completado",
            ))
            db.session.add(Appointment(
                account_id=self.account_id,
                patient_id=upcoming.id,
                service_id=service.id,
                appointment_date=current_argentina_time() + timedelta(days=2),
                status="confirmado",
            ))
            db.session.commit()

        self.login_as(self.client)
        response = self.client.get("/admin")
        self.assertNotIn(b"Seguimientos pendientes", response.data)
        self.assertNotIn(b"Sin Prueba", response.data)
        self.assertNotIn(b"ConTurno Prueba", response.data)

    def test_monthly_followup_is_not_sent_automatically(self):
        with app.app_context():
            patient = self.make_patient()
            patient.whatsapp_opt_in = True
            service = Service.query.first()
            db.session.add(Appointment(
                account_id=self.account_id,
                patient_id=patient.id,
                service_id=service.id,
                appointment_date=current_argentina_time() - timedelta(days=31),
                status="completado",
            ))
            db.session.commit()
            with patch("app.send_whatsapp_template") as send_template:
                process_automations()
            send_template.assert_not_called()

    def test_database_rejects_simultaneous_active_slot_duplicates(self):
        with app.app_context():
            patient = self.make_patient()
            service = Service.query.first()
            start = datetime.combine(self.next_business_day(), time(14))
            db.session.add(Appointment(
                account_id=self.account_id, patient_id=patient.id,
                service_id=service.id, appointment_date=start,
            ))
            db.session.commit()
            with self.assertRaises(IntegrityError):
                db.session.add(Appointment(
                    account_id=self.account_id, patient_id=patient.id,
                    service_id=service.id, appointment_date=start,
                ))
                db.session.commit()
            db.session.rollback()

    def test_expired_unpaid_deposit_cancels_appointment(self):
        with app.app_context():
            patient = self.make_patient()
            service = Service.query.first()
            appointment = Appointment(
                account_id=self.account_id,
                patient_id=patient.id,
                service_id=service.id,
                appointment_date=current_argentina_time() + timedelta(days=2),
                deposit_amount=1000,
                deposit_due_at=current_argentina_time() - timedelta(seconds=1),
            )
            db.session.add(appointment)
            db.session.commit()
            process_automations()
            self.assertEqual(db.session.get(Appointment, appointment.id).status, "cancelado")

    def test_dashboard_requires_login_and_page_settings_are_editable(self):
        worker_response = self.client.get("/service-worker.js")
        manifest_response = self.client.get("/static/manifest.webmanifest")
        self.assertEqual(worker_response.status_code, 200)
        self.assertEqual(manifest_response.status_code, 200)
        worker_response.close()
        manifest_response.close()
        self.assertEqual(self.client.get("/admin").status_code, 302)
        self.login_as(self.client)
        agenda_response = self.client.get("/admin")
        self.assertEqual(agenda_response.status_code, 200)
        self.assertNotIn(b'href="/admin/respaldo"', agenda_response.data)
        self.assertEqual(self.client.get("/admin/config").status_code, 200)
        self.assertEqual(self.client.get("/admin/pacientes").status_code, 200)
        response = self.client.post("/admin/config", data={
            "action": "save_site",
            "site_title": "Nutrición Ana",
            "site_description": "Atención personalizada.",
            "deposit_amount": "5000",
            "payment_alias": "ana.consulta",
            "payment_instructions": "Transferencia bancaria.",
            "practitioner_email": "",
        })
        self.assertEqual(response.status_code, 302)
        public_page = self.client.get(f"/p/{self.slug}")
        self.assertIn("Nutrición Ana".encode(), public_page.data)
        self.assertIn("ana.consulta".encode(), public_page.data)
        directory_page = self.client.get("/")
        self.assertIn(f"/p/{self.slug}".encode(), directory_page.data)

    def test_logout_requires_csrf_token(self):
        app.config["WTF_CSRF_ENABLED"] = True
        try:
            client = app.test_client()
            self.login_as(client)
            response = client.post("/admin/logout")
            self.assertEqual(response.status_code, 400)
            page = client.get("/admin")
            token = re.search(rb'name="csrf-token" content="([^"]+)"', page.data).group(1).decode()
            response = client.post("/admin/logout", data={"csrf_token": token})
            self.assertEqual(response.status_code, 302)
        finally:
            app.config["WTF_CSRF_ENABLED"] = False

    def test_management_link_can_cancel_an_appointment(self):
        with app.app_context():
            patient = self.make_patient()
            appointment = Appointment(
                account_id=self.account_id,
                patient_id=patient.id,
                service_id=Service.query.first().id,
                appointment_date=current_argentina_time() + timedelta(days=2),
            )
            db.session.add(appointment)
            db.session.commit()
            appointment_id = appointment.id
            from app import appointment_token
            token = appointment_token(appointment)
        self.assertEqual(self.client.get(f"/turno/{token}").status_code, 200)
        response = self.client.post(f"/turno/{token}", data={"action": "cancel"})
        self.assertEqual(response.status_code, 302)
        with app.app_context():
            self.assertEqual(db.session.get(Appointment, appointment_id).status, "cancelado")

    def test_free_slots_uses_supplied_local_time(self):
        with app.app_context():
            day = self.next_business_day()
            slots = free_slots(day, self.account_id, now=datetime.combine(day, time(13)))
            self.assertIn("14:00", slots)

    def test_service_duration_prevents_overlapping_available_slots(self):
        with app.app_context():
            day = self.next_business_day()
            patient = self.make_patient()
            service = Service.query.filter_by(
                account_id=self.account_id, name="Consulta nutricional"
            ).one()
            db.session.add(Appointment(
                account_id=self.account_id,
                patient_id=patient.id,
                service_id=service.id,
                appointment_date=datetime.combine(day, time(14)),
            ))
            db.session.commit()
            slots = free_slots(
                day, self.account_id, now=datetime.combine(day, time(13)),
                duration_minutes=30,
            )
            self.assertNotIn("14:00", slots)
            self.assertNotIn("14:45", slots)
            self.assertIn("15:30", slots)

    def test_professionals_have_separate_settings_and_booking_slots(self):
        with app.app_context():
            other = ProfessionalAccount(
                google_sub="other-professional",
                email="other@example.com",
                display_name="Other Professional",
                slug="other-professional",
            )
            db.session.add(other)
            db.session.commit()
            seed(other.id)
            day = self.next_business_day()
            patient = self.make_patient()
            patient.dni = "123"
            service = Service.query.filter_by(account_id=self.account_id).first()
            other_patient = Patient(
                account_id=other.id,
                first_name="Bea",
                last_name="Prueba",
                phone="+5491112345679",
                dni="123",
            )
            other_service = Service.query.filter_by(account_id=other.id).first()
            settings = ClinicSettings.query.filter_by(account_id=other.id).one()
            settings.site_title = "Other clinic"
            db.session.add(Appointment(
                account_id=self.account_id,
                patient_id=patient.id,
                service_id=service.id,
                appointment_date=datetime.combine(day, time(14)),
            ))
            db.session.add(other_patient)
            db.session.flush()
            other_appointment = Appointment(
                account_id=other.id,
                patient_id=other_patient.id,
                service_id=other_service.id,
                appointment_date=datetime.combine(day, time(14)),
            )
            db.session.add(other_appointment)
            db.session.commit()
            db.session.delete(other_appointment)
            db.session.commit()
            other_id, other_slug = other.id, other.slug
            foreign_service_id = service.id
            service_id = service.id
            other_service_id = other_service.id
            self.assertNotEqual(
                ClinicSettings.query.filter_by(account_id=self.account_id).one().site_title,
                settings.site_title,
            )

        self.assertNotIn(
            "14:00",
            self.client.get(
                f"/p/{self.slug}/api/available-slots?date={day.isoformat()}&service_id={service_id}"
            ).json,
        )
        self.assertIn(
            "14:00",
            self.client.get(
                f"/p/{other_slug}/api/available-slots?date={day.isoformat()}&service_id={other_service_id}"
            ).json,
        )
        self.assertEqual(self.client.post(
            f"/p/{other_slug}/api/book",
            json={
                "date": day.isoformat(),
                "time": "14:00",
                "service_id": foreign_service_id,
            },
        ).status_code, 400)

        other_client = app.test_client()
        self.login_as(other_client, other_id)
        with app.app_context():
            patient_id = Patient.query.filter_by(account_id=self.account_id).one().id
        self.assertEqual(other_client.get(f"/admin/pacientes/{patient_id}").status_code, 404)

    def test_sqlite_backup_is_a_valid_database(self):
        previous_dir = os.environ.get("BACKUP_DIR")
        with tempfile.TemporaryDirectory() as backup_dir:
            os.environ["BACKUP_DIR"] = backup_dir
            try:
                with app.app_context():
                    backup_path = create_database_backup()
                connection = sqlite3.connect(backup_path)
                try:
                    self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                finally:
                    connection.close()
            finally:
                if previous_dir is None:
                    os.environ.pop("BACKUP_DIR", None)
                else:
                    os.environ["BACKUP_DIR"] = previous_dir

    def test_downloaded_backup_contains_only_the_current_professional(self):
        with app.app_context():
            other = ProfessionalAccount(
                google_sub="backup-other",
                email="backup-other@example.com",
                display_name="Other",
                slug="backup-other",
            )
            db.session.add(other)
            db.session.commit()
            db.session.add_all([
                Patient(
                    account_id=self.account_id,
                    first_name="Owner",
                    last_name="Patient",
                    phone="+5491112345678",
                ),
                Patient(
                    account_id=other.id,
                    first_name="Other",
                    last_name="Patient",
                    phone="+5491112345679",
                ),
            ])
            db.session.commit()
            other_id = other.id

        with tempfile.TemporaryDirectory() as backup_dir:
            previous_dir = os.environ.get("BACKUP_DIR")
            os.environ["BACKUP_DIR"] = backup_dir
            try:
                with app.app_context():
                    backup_path = create_database_backup(account_id=self.account_id)
                connection = sqlite3.connect(backup_path)
                try:
                    self.assertEqual(
                        connection.execute("SELECT id FROM professional_account").fetchall(),
                        [(self.account_id,)],
                    )
                    self.assertEqual(
                        connection.execute("SELECT first_name FROM patient").fetchall(),
                        [("Owner",)],
                    )
                finally:
                    connection.close()
                self.assertNotEqual(other_id, self.account_id)
            finally:
                if previous_dir is None:
                    os.environ.pop("BACKUP_DIR", None)
                else:
                    os.environ["BACKUP_DIR"] = previous_dir


if __name__ == "__main__":
    unittest.main()
