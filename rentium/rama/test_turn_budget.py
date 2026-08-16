"""The turn's own deadline must fit inside the caller's.

RAMA's model loop stops itself at TURN_BUDGET_SECONDS and reports whatever it
has. That stop is only reachable if the caller lets the turn live that long.
It didn't: TURN_BUDGET_SECONDS was raised to 75 while CELERY_TASK_SOFT_TIME_LIMIT
stayed at 60, so Celery killed the task at 60s with SoftTimeLimitExceeded and the
landlord was told "Something broke while I was working on that" — on a turn that
had already gathered the answer.

Two numbers in two files drifted apart because nothing compared them. These
tests compare them.
"""

from __future__ import annotations

import inspect
import re

import pytest
from django.conf import settings
from django.test import override_settings

from config.celery_app import app

from .service import MIN_TURN_BUDGET_SECONDS
from .service import TURN_BUDGET_SECONDS
from .service import _turn_budget_seconds


class _FakeRequest:
    def __init__(self, soft):
        self.timelimit = (soft, None)


class _FakeTask:
    soft_time_limit = None

    def __init__(self, soft):
        self.request = _FakeRequest(soft)


@pytest.fixture
def _no_task(monkeypatch):
    """Outside a worker there is no imposed deadline."""
    monkeypatch.setattr("celery.current_task", None)


def test_budget_is_the_configured_one_outside_a_worker(_no_task):
    assert _turn_budget_seconds() == float(TURN_BUDGET_SECONDS)


def test_budget_shrinks_to_fit_a_tighter_caller(monkeypatch):
    """The exact regression: a 60s soft limit under a 90s budget."""
    monkeypatch.setattr("celery.current_task", _FakeTask(60))
    budget = _turn_budget_seconds()
    assert budget < 60, "the loop must stop before Celery kills the task"
    assert budget == 60 - settings.RAMA_TURN_TASK_HEADROOM_SECONDS


def test_budget_never_collapses_to_nothing(monkeypatch):
    """A caller with almost no time still gets a usable floor — answering late
    beats answering nothing, and the caller's hard limit still bounds us."""
    monkeypatch.setattr("celery.current_task", _FakeTask(5))
    assert _turn_budget_seconds() == float(MIN_TURN_BUDGET_SECONDS)


def test_generous_caller_does_not_extend_the_budget(monkeypatch):
    monkeypatch.setattr("celery.current_task", _FakeTask(10_000))
    assert _turn_budget_seconds() == float(TURN_BUDGET_SECONDS)


def test_headroom_covers_a_final_model_round():
    """The loop checks its deadline at loop TOP, so after the last check one
    whole provider round plus persistence still has to fit."""
    assert settings.RAMA_TURN_TASK_HEADROOM_SECONDS >= 30
    assert (
        settings.RAMA_TURN_TASK_SOFT_TIME_LIMIT
        == settings.RAMA_TURN_BUDGET_SECONDS
        + settings.RAMA_TURN_TASK_HEADROOM_SECONDS
    )
    assert (
        settings.RAMA_TURN_TASK_TIME_LIMIT
        > settings.RAMA_TURN_TASK_SOFT_TIME_LIMIT
    ), "the hard limit must leave room for the soft handler to run"


def _modules_that_run_a_turn() -> set[str]:
    """Every rentium module whose source calls run_turn."""
    import pkgutil  # noqa: PLC0415

    import rentium  # noqa: PLC0415

    names = set()
    for info in pkgutil.walk_packages(rentium.__path__, "rentium."):
        if ".test" in info.name or info.name.endswith("_test"):
            continue
        try:
            source = inspect.getsource(__import__(info.name, fromlist=["_"]))
        except Exception:  # noqa: BLE001 - unimportable module can't run a turn
            continue
        # Bare name, not "run_turn(": deliberation.py imports it as
        # `run_turn as turn_runner` and calls it under the alias.
        if re.search(r"\brun_turn\b", source):
            names.add(info.name.rsplit(".", 1)[-1])
    return names


def _tasks_that_run_a_turn():
    """Celery tasks that reach run_turn, directly or through one import hop.

    Deliberately discovered rather than hand-listed: a new task that runs a turn
    is found by this test the day it is written, which is the failure we are
    preventing, not the one we already fixed. Every call site in this codebase
    imports the turn-running module inside the task body, so one hop is enough;
    the cost of the approximation is a false positive, which fails loudly and is
    fixed by granting a limit that was wanted anyway.
    """
    import importlib  # noqa: PLC0415

    from celery.app.task import Task  # noqa: PLC0415

    turn_modules = _modules_that_run_a_turn()
    found = []
    for label in settings.INSTALLED_APPS:
        try:
            module = importlib.import_module(f"{label}.tasks")
        except ImportError:
            continue
        for attr in vars(module).values():
            if not (
                isinstance(attr, Task)
                and inspect.getmodule(attr.run) is module
            ):
                continue
            try:
                body = inspect.getsource(attr.run)
            except (OSError, TypeError):  # pragma: no cover
                continue
            # Either the task calls run_turn itself, or it imports a module
            # that does — however that import is spelled (relative, absolute,
            # or `from . import x`).
            reaches = bool(re.search(r"\brun_turn\b", body)) or any(
                re.search(rf"\bimport\b[^\n]*\b{name}\b", body)
                or re.search(rf"\bfrom\b[^\n]*\.{name}\b", body)
                for name in turn_modules
            )
            if reaches:
                found.append((attr.name, attr))
    return found


def test_every_turn_running_task_grants_the_full_budget():
    tasks = _tasks_that_run_a_turn()
    assert tasks, "expected to find the Telegram/WhatsApp turn tasks"
    required = settings.RAMA_TURN_TASK_SOFT_TIME_LIMIT
    too_tight = [
        name
        for name, task in tasks
        if (task.soft_time_limit or settings.CELERY_TASK_SOFT_TIME_LIMIT)
        < required
    ]
    assert not too_tight, (
        f"these tasks run a model turn but would be killed before it can stop "
        f"itself: {too_tight}. Pass soft_time_limit="
        f"settings.RAMA_TURN_TASK_SOFT_TIME_LIMIT."
    )


@override_settings(RAMA_TURN_TASK_HEADROOM_SECONDS=30)
def test_clamp_reads_the_task_attribute_when_no_per_call_limit(monkeypatch):
    """apply_async(soft_time_limit=…) wins, then the task's own, then the app
    default — a task decorated with a limit must still be honoured."""

    class _TaskLevelOnly:
        soft_time_limit = 60

        class request:  # noqa: N801 - mimicking celery's request object
            timelimit = (None, None)

    monkeypatch.setattr("celery.current_task", _TaskLevelOnly())
    assert _turn_budget_seconds() == 30


def test_a_provider_call_cannot_outlive_the_turn():
    """The same drift as the Celery limit, one layer down.

    The providers hardcoded a 25s timeout while a turn had 90. A call carrying
    the 66KB portfolio snapshot took longer than 25s, so the landlord got
    "Could not reach the openai API" with 65 seconds of budget unspent — and
    the message pointed at the network rather than at the payload.
    """
    from django.conf import settings

    provider = settings.RAMA_PROVIDER_TIMEOUT_SECONDS
    budget = settings.RAMA_TURN_BUDGET_SECONDS
    assert provider < budget, (
        "a single provider call must not be able to consume the whole turn"
    )
    assert provider >= budget * 0.5, (
        "and it must be allowed most of it, or slow-but-fine calls die early"
    )


def test_every_provider_reads_the_setting():
    """A hardcoded default in one adapter is how the two drifted apart."""
    import pathlib

    providers = pathlib.Path(__file__).parent / "providers"
    for name in ("openai_compat.py", "anthropic.py"):
        source = (providers / name).read_text()
        assert "RAMA_PROVIDER_TIMEOUT_SECONDS" in source, name


def test_a_timeout_is_not_reported_as_unreachable():
    """"Could not reach" sends whoever reads the audit log to the network."""
    import pathlib

    providers = pathlib.Path(__file__).parent / "providers"
    for name in ("openai_compat.py", "anthropic.py"):
        source = (providers / name).read_text()
        assert "did not answer within the timeout" in source, name
