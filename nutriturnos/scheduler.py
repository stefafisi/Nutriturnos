import logging
from apscheduler.schedulers.blocking import BlockingScheduler

from app import app, create_database_backup, process_automations


logging.basicConfig(level=logging.INFO)
scheduler = BlockingScheduler(timezone="America/Argentina/Buenos_Aires")


def run_automations():
    with app.app_context():
        process_automations()


def run_backup():
    with app.app_context():
        try:
            path = create_database_backup()
        except (OSError, RuntimeError) as error:
            app.logger.exception("No se pudo crear el respaldo automático: %s", error)
            return
        app.logger.info("Respaldo creado: %s", path)


scheduler.add_job(run_automations, "interval", minutes=5, id="appointments", max_instances=1, coalesce=True)
scheduler.add_job(run_backup, "cron", hour=3, minute=0, id="database-backup", max_instances=1)
scheduler.start()
