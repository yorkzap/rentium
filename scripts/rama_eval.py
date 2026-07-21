#!/usr/bin/env python
"""RAMA smartness evals — scripted multi-turn scenarios, graded
deterministically (DB side-effects + tools_used + regex; no LLM judge).

The pass bar is WEAK models: if Mistral Small can't pass, the scaffolding —
not the model — needs work.

Docker:
  docker compose -f docker-compose.local.yml exec -T django \\
    python /app/scripts/rama_eval.py

Env:
  RAMA_EVAL_MODELS  "mistral:mistral-small-latest,xai:grok-4.3"
                    (default: the landlord's currently configured model)
  RAMA_EVAL_ONLY    substring filter on scenario names
  RAMA_EVAL_DRY=1   set up + tear down fixtures only, no chat (plumbing check)
  RAMA_BASE_URL / RAMA_EMAIL / RAMA_TOKEN — as in rama_smoke.py
  RAMA_SLEEP        seconds between turns (default 0.5)

Fixtures are [RAMA-EVAL]-marked and torn down after every scenario; the
landlord's provider prefs are restored at the end. DEV ONLY (needs DEBUG).
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import uuid

APP = "/app" if os.path.isdir("/app") else os.path.dirname(os.path.dirname(__file__))
if APP not in sys.path:
    sys.path.insert(0, APP)
if os.path.dirname(os.path.abspath(__file__)) not in sys.path:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(APP)
os.environ.setdefault(
    "DATABASE_URL",
    os.environ.get("DATABASE_URL", "postgres://debug:debug@postgres:5432/rentium"),
)
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.local")

import django

django.setup()

from django.conf import settings as dj_settings

from rama_eval_scenarios import SCENARIOS

BASE_URL = os.environ.get("RAMA_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
EMAIL = os.environ.get("RAMA_EMAIL", "rajgurshersingh@gmail.com")
EVAL_EMAIL = os.environ.get("RAMA_EVAL_EMAIL", "rama-eval@rentium.local")
SLEEP_S = float(os.environ.get("RAMA_SLEEP", "0.5"))
DRY = os.environ.get("RAMA_EVAL_DRY", "0") == "1"
ONLY = (os.environ.get("RAMA_EVAL_ONLY") or "").strip().lower()


def get_landlord():
    """A DEDICATED eval landlord whose portfolio holds ONLY [RAMA-EVAL]
    fixtures — so a scenario that scopes over "all listings" can never touch
    real data. Provider prefs + BYOK key are copied from RAMA_EMAIL's account
    so the same keys work."""
    from django.contrib.auth import get_user_model

    from rentium.rama.models import RamaPreferences
    from rentium.users.models import LandlordProfile

    User = get_user_model()
    source = User.objects.filter(email=EMAIL).first()
    if not source or not getattr(source, "landlord_profile", None):
        raise SystemExit(f"No landlord for {EMAIL} (needed for provider prefs)")

    user = User.objects.filter(email=EVAL_EMAIL).first()
    if not user:
        user = User.objects.create_user(
            email=EVAL_EMAIL, password=None, name="RAMA Eval"
        )
        user.user_type = getattr(source, "user_type", "") or "LANDLORD"
        user.set_unusable_password()
        user.save()
    landlord, _ = LandlordProfile.objects.get_or_create(user=user)

    src_prefs = RamaPreferences.for_landlord(source.landlord_profile)
    prefs = RamaPreferences.for_landlord(landlord)
    prefs.enabled = True
    prefs.provider = src_prefs.provider
    prefs.model = src_prefs.model
    if src_prefs.api_key and not prefs.api_key:
        prefs.api_key = src_prefs.api_key
    prefs.save()
    return user, landlord


def get_token(user) -> str:
    if os.environ.get("RAMA_TOKEN"):
        return os.environ["RAMA_TOKEN"].strip()
    from rest_framework.authtoken.models import Token

    token, _ = Token.objects.get_or_create(user=user)
    return token.key


def chat(token: str, message: str, conversation_id: str) -> dict:
    body = json.dumps({"message": message, "conversation_id": conversation_id})
    req = urllib.request.Request(
        f"{BASE_URL}/api/rama/chat/",
        data=body.encode("utf-8"),
        headers={
            "Authorization": f"Token {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        return {"detail": raw[:500], "status": exc.code}


def grade(expect: dict, response: dict, landlord, ctx) -> list[str]:
    """Every failed expectation as a message; [] = turn passed."""
    problems: list[str] = []
    reply = response.get("reply") or ""
    tools = response.get("tools_used") or []
    plan = response.get("pending_plan")

    if "status" in response:
        problems.append(f"HTTP {response['status']}: {response.get('detail')}")
        return problems
    for name in expect.get("tools_any", []) or []:
        if name in tools:
            break
    else:
        if expect.get("tools_any"):
            problems.append(
                f"none of {expect['tools_any']} in tools_used={tools}"
            )
    for name in expect.get("tools_none", []) or []:
        if name in tools:
            problems.append(f"forbidden tool {name} was used")
    for rx in expect.get("reply_regex", []) or []:
        if not re.search(rx, reply, re.IGNORECASE):
            problems.append(f"reply missing /{rx}/")
    for rx in expect.get("reply_not_regex", []) or []:
        if re.search(rx, reply, re.IGNORECASE):
            problems.append(f"reply wrongly matches /{rx}/")
    if "pending_plan" in expect:
        if bool(plan) is not expect["pending_plan"]:
            problems.append(
                f"pending_plan is {'present' if plan else 'absent'}, "
                f"expected {'present' if expect['pending_plan'] else 'absent'}"
            )
    if "awaiting_step" in expect and plan is not None:
        if bool(plan.get("awaiting_own_confirm")) is not expect["awaiting_step"]:
            problems.append(
                f"awaiting_own_confirm={plan.get('awaiting_own_confirm')}, "
                f"expected {expect['awaiting_step']}"
            )
    db_check = expect.get("db")
    if db_check is not None:
        ok, msg = db_check(landlord, ctx)
        if not ok:
            problems.append(f"DB: {msg}")
    return problems


def run_scenario(scenario: dict, token: str, landlord) -> tuple[bool, list[str]]:
    ctx = scenario["setup"](landlord)
    conv = str(uuid.uuid4())
    failures: list[str] = []
    try:
        if DRY:
            return True, ["(dry run — fixtures only)"]
        for i, turn in enumerate(scenario["turns"], start=1):
            response = chat(token, turn["say"], conv)
            problems = grade(turn.get("expect") or {}, response, landlord, ctx)
            if problems:
                failures.append(
                    f"turn {i} ({turn['say'][:60]!r}):\n      - "
                    + "\n      - ".join(problems)
                    + f"\n      reply: {str(response.get('reply'))[:300]!r}"
                )
            time.sleep(SLEEP_S)
    finally:
        scenario["teardown"](landlord, ctx)
    return not failures, failures


def parse_models() -> list[tuple[str, str]]:
    raw = (os.environ.get("RAMA_EVAL_MODELS") or "").strip()
    if not raw:
        return []
    pairs = []
    for item in raw.split(","):
        provider, _, model = item.strip().partition(":")
        if provider:
            pairs.append((provider.strip().lower(), model.strip()))
    return pairs


def main() -> int:
    if not dj_settings.DEBUG:
        print("Refusing: evals mutate portfolio data — dev only (DEBUG off).")
        return 1
    user, landlord = get_landlord()
    token = get_token(user)

    from rentium.rama.models import RamaPreferences

    prefs = RamaPreferences.for_landlord(landlord)
    original = (prefs.provider, prefs.model)
    matrix = parse_models() or [original]

    scenarios = [
        s for s in SCENARIOS if not ONLY or ONLY in s["name"].lower()
    ]
    print("=" * 70)
    print(f"RAMA evals  base={BASE_URL}  email={EMAIL}  dry={DRY}")
    print(f"models: {', '.join(f'{p}:{m or 'default'}' for p, m in matrix)}")
    print(f"scenarios: {len(scenarios)} (pass bar = weak models)")
    print("=" * 70)

    results: dict[str, dict[str, bool]] = {}
    try:
        for provider, model in matrix:
            label = f"{provider}:{model or 'default'}"
            prefs.provider = provider
            prefs.model = model
            prefs.save(update_fields=["provider", "model", "updated_at"])
            print(f"\n### {label}")
            for scenario in scenarios:
                ok, failures = run_scenario(scenario, token, landlord)
                results.setdefault(scenario["name"], {})[label] = ok
                print(f"  [{'PASS' if ok else 'FAIL'}] {scenario['name']}")
                for f in failures if not ok else []:
                    print(f"    {f}")
    finally:
        prefs.provider, prefs.model = original
        prefs.save(update_fields=["provider", "model", "updated_at"])

    print("\n" + "=" * 70)
    print("Summary (rows: scenarios, cols: models)")
    labels = [f"{p}:{m or 'default'}" for p, m in matrix]
    for name, by_model in results.items():
        cells = "  ".join(
            f"{label}={'✓' if by_model.get(label) else '✗'}" for label in labels
        )
        print(f"  {name}\n    {cells}")
    all_ok = all(ok for by_model in results.values() for ok in by_model.values())
    print("=" * 70)
    print("ALL PASS" if all_ok else "FAILURES — transcripts above")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
