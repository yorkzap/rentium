from __future__ import annotations

import json
from pathlib import Path

from django.core.management.base import BaseCommand

from rentium.rama.models import RamaEvalCase


class Command(BaseCommand):
    help = "Load reviewed, redacted RAMA eval cases from JSON."

    def add_arguments(self, parser):
        default = Path(__file__).resolve().parents[2] / "eval_cases"
        parser.add_argument("path", nargs="?", default=str(default))

    def handle(self, *args, **options):
        path = Path(options["path"])
        files = sorted(path.glob("*.json")) if path.is_dir() else [path]
        loaded = 0
        for file_path in files:
            rows = json.loads(file_path.read_text(encoding="utf-8"))
            for row in rows:
                RamaEvalCase.objects.update_or_create(
                    name=row["name"],
                    suite_version=row["suite_version"],
                    defaults={
                        "split": row["split"],
                        "domain_lane": row["domain_lane"],
                        "severity": row.get("severity", "critical"),
                        "transcript": row.get("transcript") or [],
                        "expected": row.get("expected") or {},
                        "prohibited_claims": row.get("prohibited_claims") or [],
                        "active": True,
                    },
                )
                loaded += 1
        self.stdout.write(self.style.SUCCESS(f"Loaded {loaded} RAMA eval cases."))
