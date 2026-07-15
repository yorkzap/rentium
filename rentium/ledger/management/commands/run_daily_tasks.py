# rentium/ledger/management/commands/run_daily_tasks.py
from django.core.management.base import BaseCommand

from rentium.ledger import daily


class Command(BaseCommand):
    help = (
        "Run daily housekeeping: expire leases, generate rent, reminders, SLA checks."
    )

    def handle(self, *args, **options):
        report = daily.run_all()
        self.stdout.write(self.style.SUCCESS(str(report)))
