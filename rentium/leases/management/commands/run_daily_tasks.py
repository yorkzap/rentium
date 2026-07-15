"""
CLI entrypoint for daily housekeeping. All actual logic lives in
rentium/ledger/daily.py — this command, and rentium/ledger/tasks.py
(the Celery wrappers), both call into that one module so cron and Celery
can never drift apart or double-implement the same jobs.

Usage:
    python manage.py run_daily_tasks
"""

from django.core.management.base import BaseCommand

from rentium.ledger import daily


class Command(BaseCommand):
    help = (
        "Run daily housekeeping: expire leases, generate rent, reminders, SLA checks."
    )

    def handle(self, *args, **options):
        report = daily.run_all()
        self.stdout.write(self.style.SUCCESS(str(report)))
