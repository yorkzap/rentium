# tasks.py
"""
Celery wrappers around rentium/ledger/daily.py.

Cookiecutter-django projects created with use_celery=y expose the app at
config.celery_app and autodiscover any tasks.py. If your project was
generated WITHOUT Celery, don't add this file — use the cron path instead:

    # crontab -e  (host or a sidecar container)
    15 6 * * *  cd /app && python manage.py run_daily_tasks >> /var/log/rentium-daily.log 2>&1

Each task delegates to daily.py, so cron and Celery are interchangeable
and idempotent — running both by accident double-executes nothing.

Beat schedule (config/settings/base.py or production.py):

    from celery.schedules import crontab

    CELERY_BEAT_SCHEDULE = {
        # One combined run early morning local time is enough for all four
        # jobs; they're cheap and idempotent.
        "rentium-daily-housekeeping": {
            "task": "rentium.ledger.tasks.run_daily_housekeeping",
            "schedule": crontab(hour=6, minute=15),
        },
        # SLA breaches are the one time-sensitive job — check hourly so an
        # RTA-emergency work order blowing its deadline at 9am doesn't wait
        # until tomorrow's daily run to alert the landlord.
        "rentium-sla-breach-check": {
            "task": "rentium.ledger.tasks.check_sla_breaches",
            "schedule": crontab(minute=5),  # hourly at :05
        },
    }

And remember beat + a worker must both be running, e.g. in docker compose:
    celeryworker: command: celery -A config.celery_app worker -l info
    celerybeat:   command: celery -A config.celery_app beat -l info
"""

from config.celery_app import app

from . import daily


@app.task(bind=True, max_retries=2, default_retry_delay=300)
def run_daily_housekeeping(self):
    """Everything: expiry -> rent generation -> reminders -> SLA check."""
    try:
        return daily.run_all()
    except Exception as exc:  # let transient DB hiccups retry
        raise self.retry(exc=exc)


@app.task
def generate_rent_charges():
    return daily.generate_all_rent_charges()


@app.task
def expire_leases():
    return daily.expire_ended_leases()


@app.task
def send_due_soon_reminders():
    return daily.publish_charge_due_reminders()


@app.task
def check_sla_breaches():
    return daily.flag_sla_breaches()