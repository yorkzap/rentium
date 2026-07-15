"""
Non-interactive superuser creation. Idempotent — safe to run every time you
reset the database, since it promotes an existing matching user instead of
erroring out.

Usage:
    python manage.py create_admin --email admin@example.com --password changeme
    # or via environment:
    DJANGO_ADMIN_EMAIL=admin@example.com DJANGO_ADMIN_PASSWORD=changeme \
        python manage.py create_admin
"""

import os

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.core.management.base import CommandError

User = get_user_model()


class Command(BaseCommand):
    help = "Create (or promote) a superuser account non-interactively."

    def add_arguments(self, parser):
        parser.add_argument("--email", default=os.environ.get("DJANGO_ADMIN_EMAIL"))
        parser.add_argument(
            "--password", default=os.environ.get("DJANGO_ADMIN_PASSWORD")
        )
        parser.add_argument(
            "--name", default=os.environ.get("DJANGO_ADMIN_NAME", "Admin")
        )

    def handle(self, *args, **options):
        email = options["email"]
        password = options["password"]
        name = options["name"]

        if not email or not password:
            raise CommandError(
                "Provide --email/--password, or set DJANGO_ADMIN_EMAIL / "
                "DJANGO_ADMIN_PASSWORD in the environment."
            )

        user, created = User.objects.get_or_create(
            email__iexact=email,
            defaults={"email": email, "name": name},
        )
        user.name = user.name or name
        user.is_staff = True
        user.is_superuser = True
        user.set_password(password)
        user.save()

        verb = "Created" if created else "Updated"
        self.stdout.write(self.style.SUCCESS(f"{verb} admin user: {email}"))
