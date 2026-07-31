#!/usr/bin/env python
"""API ↔ RAMA capability parity report.

Compares landlord-facing Django REST endpoints (viewsets + @api_view +
@action methods) against RAMA's registered tool surface and a curated
coverage map. Use this to find business operations the UI/API can do that
RAMA still cannot, and vice versa.

Run (Docker):
  docker compose -f docker-compose.local.yml exec -T django \\
    python /app/scripts/rama_api_parity_report.py

Host (with Django settings):
  cd rentium && DJANGO_SETTINGS_MODULE=config.settings.test \\
    python scripts/rama_api_parity_report.py

Flags:
  --json          machine-readable JSON to stdout
  --fail-on-gap   exit 1 if any high-priority API action lacks a RAMA tool
  --out PATH      also write markdown report to PATH
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from dataclasses import asdict
from dataclasses import dataclass
from pathlib import Path

# ---------------------------------------------------------------------------
# Django bootstrap
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.local")

import django  # noqa: E402

django.setup()

from rest_framework.decorators import MethodMapper  # noqa: E402
from rest_framework.views import APIView  # noqa: E402
from rest_framework.viewsets import ViewSetMixin  # noqa: E402

from rentium.rama.registry import REGISTRY  # noqa: E402
from rentium.rama.tool_meta import meta_for  # noqa: E402

# ---------------------------------------------------------------------------
# Curated map: high-value API operations → RAMA tool(s)
# Keys are "{app}:{ViewSetOrView}.{action}" or free-form labels.
# Value is one or more RAMA tool names, or None if intentionally uncovered.
# ---------------------------------------------------------------------------

COVERAGE_MAP: dict[str, list[str] | None] = {
    # leases
    "leases:LeaseViewSet.create": ["create_lease"],
    "leases:LeaseViewSet.partial_update": ["update_lease", "adjust_lease"],
    "leases:LeaseViewSet.destroy": ["delete_draft_lease"],
    "leases:LeaseViewSet.terminate": ["terminate_lease"],
    "leases:LeaseViewSet.renew": ["renew_lease"],
    "leases:LeaseViewSet.landlord_sign": ["landlord_sign_lease"],
    "leases:LeaseViewSet.list": ["list_leases", "find_leases"],
    "leases:LeaseViewSet.retrieve": ["lease_state", "open_lease"],
    "leases:LeaseTenantViewSet.create": [
        "invite_tenant_to_lease",
        "add_roommate_to_lease",
    ],
    "leases:RentAdjustmentViewSet.create": ["apply_rent_adjustment"],
    "leases:ConditionInspectionViewSet.create": [
        "create_condition_inspection",
        "complete_inspection_package",
    ],
    "leases:ConditionInspectionViewSet.start_move_out": [
        "complete_inspection_package",
    ],
    "leases:MoveOutViewSet.create": ["settle_moveout"],
    "leases:MoveOutViewSet.settle_deposit": ["settle_moveout"],
    "leases:MoveOutViewSet.accept": ["settle_moveout"],
    # ledger
    "ledger:utility_bill_view": ["record_utility_bill"],
    "ledger:LedgerEntryViewSet.create": ["create_expense", "record_payment"],
    "ledger:LedgerEntryViewSet.expense": ["create_expense"],
    "ledger:LedgerEntryViewSet.record_payment": ["record_payment"],
    "ledger:LedgerEntryViewSet.reallocate": ["reallocate_expense"],
    "ledger:summary_view": ["month_money", "portfolio_snapshot"],
    "ledger:tenant_statement_view": ["tenant_statement"],
    # appointments / showcase
    "appointments:AppointmentViewSet.create": ["schedule_viewing"],
    "appointments:AppointmentViewSet.reschedule": ["reschedule_viewing"],
    "showcase:InquiryViewSet.mark_replied": ["mark_inquiry_replied"],
    "showcase:InquiryViewSet.to_appointment": ["convert_inquiry_to_viewing"],
    "showcase:InquiryViewSet.list": ["list_inquiries"],
    # maintenance
    "maintenance:WorkOrderViewSet.create": ["create_work_order"],
    "maintenance:WorkOrderViewSet.partial_update": ["update_work_order"],
    # properties
    "properties:PropertyViewSet.create": [
        "create_property",
        "create_property_structure",
    ],
    "properties:PropertyViewSet.partial_update": ["update_property"],
    "properties:PropertyViewSet.destroy": ["delete_property"],
}

# API apps we care about for landlord operations (skip auth/admin noise).
SCAN_APP_PREFIXES = (
    "rentium.leases",
    "rentium.ledger",
    "rentium.properties",
    "rentium.appointments",
    "rentium.showcase",
    "rentium.maintenance",
    "rentium.messaging",
    "rentium.agenda",
    "rentium.events",
    "rentium.users",
    "rentium.comms",
    "rentium.rama",
)


@dataclass
class ApiEndpoint:
    key: str
    app: str
    view: str
    action: str
    methods: list[str]
    detail: bool | None


@dataclass
class GapRow:
    api_key: str
    methods: list[str]
    mapped_tools: list[str] | None
    status: str  # covered | missing_tool | unmapped | intentional
    note: str


def _app_label(module: str) -> str:
    parts = module.split(".")
    if "rentium" in parts:
        i = parts.index("rentium")
        if i + 1 < len(parts):
            return parts[i + 1]
    return parts[0] if parts else "unknown"


def _iter_api_objects() -> list[tuple[str, object]]:
    """Import known API modules; return (export_name, object) pairs."""
    modules = [
        "rentium.leases.api.views",
        "rentium.leases.api.moveout_views",
        "rentium.leases.api.inspection_views",
        "rentium.leases.api.document_views",
        "rentium.ledger.api.views",
        "rentium.ledger.api.import_views",
        "rentium.properties.api.views",
        "rentium.appointments.api.views",
        "rentium.showcase.api.views",
        "rentium.maintenance.api.views",
        "rentium.messaging.api.views",
        "rentium.agenda.api.views",
        "rentium.events.api.views",
        "rentium.users.api.views",
        "rentium.comms.api.views",
        "rentium.rama.views",
    ]
    found: list[tuple[str, object]] = []
    for mod_name in modules:
        try:
            mod = __import__(mod_name, fromlist=["*"])
        except Exception as exc:  # noqa: BLE001
            print(f"# skip import {mod_name}: {exc}", file=sys.stderr)
            continue
        for attr in dir(mod):
            if attr.startswith("_"):
                continue
            obj = getattr(mod, attr)
            if isinstance(obj, type):
                if obj is APIView or obj is ViewSetMixin:
                    continue
                try:
                    if issubclass(obj, (APIView, ViewSetMixin)):
                        found.append((attr, obj))
                except TypeError:
                    continue
            elif callable(obj) and hasattr(obj, "cls"):
                # @api_view — export name is attr (fn.__name__ is always "view")
                found.append((attr, obj))
    return found


def _endpoint_from_viewset(cls: type) -> list[ApiEndpoint]:
    app = _app_label(cls.__module__)
    name = cls.__name__
    rows: list[ApiEndpoint] = []

    # Standard CRUD
    for action, methods, detail in (
        ("list", ["get"], False),
        ("retrieve", ["get"], True),
        ("create", ["post"], False),
        ("update", ["put"], True),
        ("partial_update", ["patch"], True),
        ("destroy", ["delete"], True),
    ):
        if hasattr(cls, action):
            rows.append(
                ApiEndpoint(
                    key=f"{app}:{name}.{action}",
                    app=app,
                    view=name,
                    action=action,
                    methods=methods,
                    detail=detail,
                )
            )

    # @action methods
    for attr_name in dir(cls):
        if attr_name.startswith("_"):
            continue
        attr = getattr(cls, attr_name, None)
        mapping = getattr(attr, "mapping", None)
        if mapping is None and hasattr(attr, "kwargs"):
            # older DRF
            mapping = getattr(attr, "kwargs", {}).get("url_path")
        if not isinstance(getattr(attr, "mapping", None), (dict, MethodMapper)):
            # Check for detail/url_path on action decorator
            if not getattr(attr, "detail", None) and not getattr(attr, "url_path", None):
                # DRF marks actions with .mapping
                if not hasattr(attr, "mapping"):
                    continue
        if not hasattr(attr, "mapping"):
            continue
        methods = sorted(
            m.upper() for m, bound in dict(attr.mapping).items() if bound
        )
        if not methods:
            continue
        action_name = attr_name
        rows.append(
            ApiEndpoint(
                key=f"{app}:{name}.{action_name}",
                app=app,
                view=name,
                action=action_name,
                methods=methods,
                detail=bool(getattr(attr, "detail", True)),
            )
        )
    return rows


def _endpoint_from_api_view_fn(export_name: str, fn) -> list[ApiEndpoint]:
    """Handle @api_view function views (e.g. utility_bill_view)."""
    module = getattr(fn, "__module__", "") or getattr(
        getattr(fn, "cls", None), "__module__", ""
    )
    app = _app_label(module)
    cls = getattr(fn, "cls", None)
    methods = []
    if cls is not None:
        methods = [
            m.upper()
            for m in ("get", "post", "put", "patch", "delete")
            if hasattr(cls, m)
        ]
    if not methods:
        actions = getattr(fn, "actions", {}) or {}
        methods = sorted(m.upper() for m in actions) if actions else ["POST"]
    return [
        ApiEndpoint(
            key=f"{app}:{export_name}",
            app=app,
            view=str(export_name),
            action="call",
            methods=methods,
            detail=None,
        )
    ]


def collect_api_endpoints() -> list[ApiEndpoint]:
    by_key: dict[str, ApiEndpoint] = {}
    for export_name, obj in _iter_api_objects():
        if isinstance(obj, type) and issubclass(obj, ViewSetMixin):
            for ep in _endpoint_from_viewset(obj):
                by_key[ep.key] = ep
        elif callable(obj) and hasattr(obj, "cls"):
            for ep in _endpoint_from_api_view_fn(export_name, obj):
                by_key[ep.key] = ep
        elif isinstance(obj, type) and issubclass(obj, APIView):
            app = _app_label(obj.__module__)
            name = obj.__name__
            methods = sorted(
                m for m in ("get", "post", "put", "patch", "delete") if hasattr(obj, m)
            )
            if methods:
                key = f"{app}:{name}"
                by_key[key] = ApiEndpoint(
                    key=key,
                    app=app,
                    view=name,
                    action="dispatch",
                    methods=[m.upper() for m in methods],
                    detail=None,
                )
    return sorted(by_key.values(), key=lambda e: e.key)


def collect_rama_tools() -> list[dict]:
    rows = []
    for name, tool in sorted(REGISTRY.items()):
        meta = meta_for(name)
        rows.append(
            {
                "name": name,
                "description": tool.description[:160],
                "risk": meta.risk,
                "has_confirm": "confirm" in tool.parameters.get("properties", {}),
                "autonomy": getattr(meta.autonomy, "value", str(meta.autonomy)),
            }
        )
    return rows


def classify(endpoints: list[ApiEndpoint], tools: set[str]) -> list[GapRow]:
    out: list[GapRow] = []
    for ep in endpoints:
        mapped = COVERAGE_MAP.get(ep.key, "__UNMAPPED__")
        if mapped == "__UNMAPPED__":
            # Heuristic: list/retrieve are often covered by generic reads
            if ep.action in ("list", "retrieve"):
                status = "unmapped"
                note = "No curated map; often covered by list_* / read tools"
                mapped_tools = None
            elif ep.action in ("update",) and f"{ep.app}:{ep.view}.partial_update" in COVERAGE_MAP:
                status = "unmapped"
                note = "Full PUT often unused; partial_update is mapped"
                mapped_tools = None
            else:
                status = "unmapped"
                note = "Not in COVERAGE_MAP — review if landlord-facing"
                mapped_tools = None
        elif mapped is None:
            status = "intentional"
            note = "Explicitly not exposed to RAMA"
            mapped_tools = None
        else:
            missing = [t for t in mapped if t not in tools]
            if missing:
                status = "missing_tool"
                note = f"Mapped tools missing from REGISTRY: {missing}"
                mapped_tools = mapped
            else:
                status = "covered"
                note = "All mapped tools registered"
                mapped_tools = mapped
        out.append(
            GapRow(
                api_key=ep.key,
                methods=ep.methods,
                mapped_tools=mapped_tools,
                status=status,
                note=note,
            )
        )
    return out


def render_markdown(
    endpoints: list[ApiEndpoint],
    tools: list[dict],
    gaps: list[GapRow],
) -> str:
    by_status = defaultdict(list)
    for g in gaps:
        by_status[g.status].append(g)

    lines = [
        "# API ↔ RAMA parity report",
        "",
        f"Generated tools: **{len(tools)}** · API endpoints scanned: **{len(endpoints)}**",
        "",
        "## Summary",
        "",
        f"| Status | Count |",
        f"|---|---:|",
        f"| covered | {len(by_status['covered'])} |",
        f"| missing_tool | {len(by_status['missing_tool'])} |",
        f"| unmapped | {len(by_status['unmapped'])} |",
        f"| intentional | {len(by_status['intentional'])} |",
        "",
        "## High-priority gaps (mapped but tool missing)",
        "",
    ]
    missing = by_status["missing_tool"]
    if not missing:
        lines.append("_None — every curated mapping points at a registered tool._")
        lines.append("")
    else:
        lines.append("| API | Methods | Expected RAMA tools |")
        lines.append("|---|---|---|")
        for g in missing:
            lines.append(
                f"| `{g.api_key}` | {', '.join(g.methods)} | "
                f"{', '.join(g.mapped_tools or [])} |"
            )
        lines.append("")

    lines += [
        "## Curated coverage (covered)",
        "",
        "| API | RAMA tools |",
        "|---|---|",
    ]
    for g in by_status["covered"]:
        lines.append(f"| `{g.api_key}` | {', '.join(g.mapped_tools or [])} |")
    lines.append("")

    lines += [
        "## Unmapped API actions (review)",
        "",
        "These exist on the API but have no entry in `COVERAGE_MAP`. "
        "Many are fine (admin-ish, nested mirrors, list/retrieve). "
        "Promote high-value ones into the map + a RAMA composite.",
        "",
        "| API | Methods | Note |",
        "|---|---|---|",
    ]
    for g in sorted(by_status["unmapped"], key=lambda x: x.api_key):
        if g.api_key.endswith(".list") or g.api_key.endswith(".retrieve"):
            continue  # noise
        lines.append(f"| `{g.api_key}` | {', '.join(g.methods)} | {g.note} |")
    lines.append("")

    lines += [
        "## RAMA tools with no curated API reverse-map",
        "",
        "Not necessarily a problem (composites, reads, treasury). Listed for awareness.",
        "",
    ]
    mapped_tools = set()
    for v in COVERAGE_MAP.values():
        if v:
            mapped_tools.update(v)
    orphans = [t["name"] for t in tools if t["name"] not in mapped_tools]
    lines.append(f"Count: **{len(orphans)}** of {len(tools)}")
    lines.append("")
    lines.append("<details><summary>Tool names</summary>")
    lines.append("")
    lines.append(", ".join(f"`{n}`" for n in orphans))
    lines.append("")
    lines.append("</details>")
    lines.append("")
    lines.append("---")
    lines.append(
        "Update `COVERAGE_MAP` in `scripts/rama_api_parity_report.py` when "
        "adding composites. Re-run after each phase."
    )
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--fail-on-gap", action="store_true")
    parser.add_argument("--out", type=str, default="")
    args = parser.parse_args(argv)

    endpoints = collect_api_endpoints()
    tools = collect_rama_tools()
    tool_names = {t["name"] for t in tools}
    gaps = classify(endpoints, tool_names)

    if args.json:
        payload = {
            "tool_count": len(tools),
            "endpoint_count": len(endpoints),
            "tools": tools,
            "endpoints": [asdict(e) for e in endpoints],
            "gaps": [asdict(g) for g in gaps],
            "summary": {
                s: sum(1 for g in gaps if g.status == s)
                for s in ("covered", "missing_tool", "unmapped", "intentional")
            },
        }
        print(json.dumps(payload, indent=2, default=str))
    else:
        md = render_markdown(endpoints, tools, gaps)
        print(md)
        if args.out:
            Path(args.out).write_text(md, encoding="utf-8")
            print(f"\n# wrote {args.out}", file=sys.stderr)

    if args.fail_on_gap and any(g.status == "missing_tool" for g in gaps):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
