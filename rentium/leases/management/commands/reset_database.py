"""
DANGEROUS — dev-only. Wipes the database and re-runs migrations so you can
start over. Refuses to run unless settings.DEBUG is True, and prompts for
confirmation unless --force is passed.

Usage:
    python manage.py reset_database --force --seed --with-admin
"""

import os

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand
from django.core.management.base import CommandError
from django.db import connection


class Command(BaseCommand):
    help = "DANGEROUS: wipes the database and re-runs migrations from scratch."

    def add_arguments(self, parser):
        parser.add_argument(
            "--force",
            action="store_true",
            help="Skip the interactive confirmation prompt.",
        )
        parser.add_argument(
            "--seed",
            action="store_true",
            help="Re-run seed_inspection_templates after migrating.",
        )
        parser.add_argument(
            "--with-admin",
            action="store_true",
            help="Create the default admin user afterward (uses create_admin).",
        )

    def handle(self, *args, **options):
        if not settings.DEBUG:
            raise CommandError(
                "Refusing to run with DEBUG=False. This command is for local/dev use only."
            )

        db = connection.settings_dict
        vendor = connection.vendor

        if not options["force"]:
            confirm = input(
                f"This will PERMANENTLY DELETE ALL DATA in database "
                f"'{db.get('NAME')}' ({vendor}) and re-run migrations.\n"
                f"Type 'yes' to continue: "
            )
            if confirm.strip().lower() != "yes":
                self.stdout.write(self.style.WARNING("Aborted — nothing was changed."))
                return

        if vendor == "postgresql":
            self.stdout.write("Dropping and recreating the public schema...")
            with connection.cursor() as cursor:
                cursor.execute("DROP SCHEMA public CASCADE;")
                cursor.execute("CREATE SCHEMA public;")
                cursor.execute("GRANT ALL ON SCHEMA public TO public;")
        elif vendor == "sqlite":
            db_path = db.get("NAME")
            connection.close()
            if db_path and os.path.exists(db_path):
                self.stdout.write(f"Deleting SQLite file at {db_path}...")
                os.remove(db_path)
            else:
                self.stdout.write(
                    self.style.WARNING("No SQLite file found — nothing to delete.")
                )
        else:
            self.stdout.write(
                self.style.WARNING(
                    f"No schema-drop support for vendor '{vendor}' — falling back to `flush`."
                )
            )
            call_command("flush", "--noinput")

        self.stdout.write("Running migrations...")
        call_command("migrate")

        if options["seed"]:
            self.stdout.write("Seeding inspection templates...")
            call_command("seed_inspection_templates")

        if options["with_admin"]:
            self.stdout.write("Creating default admin user...")
            call_command("create_admin")

        self.stdout.write(self.style.SUCCESS("Database reset complete."))
