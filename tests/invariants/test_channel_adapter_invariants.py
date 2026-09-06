"""Rules every channel adapter obeys, checked over all of them at once.

There are six adapters now and each was written on its own: Lark, WhatsApp, the widget,
email, Slack, Telegram. Each argued its own capabilities in its own docstring, and several
of them reached the same conclusion independently, which is a good sign about the reasoning
and a bad sign about where the rule lives. A conclusion four people reach separately is a
conclusion the fifth can miss.

**The adapters are discovered rather than listed.** A hand-written list is exactly what fails
to include the seventh one, and the seventh is the one nobody reviews as carefully as the
first. Anything in `brain.channels` exposing `capabilities()` is in scope, so a new adapter
joins these rules by existing.

The rule worth reading twice is the cards one. `gate.admission.CHANNEL_VERBS` decides what a
channel may carry, and a channel without `approve` cannot honour a button press. Declaring
`Feature.CARDS` there builds an approval that fails at the press rather than refusing at the
build, and the person who pressed it reasonably believes they approved something. Slack's
author and Telegram's author each worked that out alone and wrote it down; this is the same
argument stated once, where the eighth adapter has to meet it.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import pkgutil
from pathlib import Path
from typing import Any

import pytest

import brain.channels
from brain.channels.adapter import ChannelCapabilities, Feature
from brain.gate.admission import CHANNEL_VERBS
from brain.gate.context import Channel

CHANNELS_DIR = Path(brain.channels.__file__).parent

#: Modules that carry no adapter and are not expected to. Named rather than inferred so a
#: module that quietly stops exposing `capabilities()` is a discovery failure somebody sees.
NOT_ADAPTERS = frozenset({"adapter", "api_keys", "binding", "cards", "room", "webhook", "__init__"})


def _adapters() -> list[tuple[str, type[Any]]]:
    """Every class in `brain.channels` that answers `capabilities()`, by module name."""
    found: list[tuple[str, type[Any]]] = []
    for info in pkgutil.iter_modules([str(CHANNELS_DIR)]):
        if info.name in NOT_ADAPTERS:
            continue
        module = importlib.import_module(f"brain.channels.{info.name}")
        for _, obj in inspect.getmembers(module, inspect.isclass):
            if obj.__module__ != module.__name__:
                continue
            if not callable(getattr(obj, "capabilities", None)):
                continue
            found.append((info.name, obj))
    return found


ADAPTERS = _adapters()


def _capabilities(cls: type[Any]) -> ChannelCapabilities:
    """The declaration, built with no arguments. Every adapter here is constructible bare."""
    result: ChannelCapabilities = cls().capabilities()
    return result


def test_the_discovery_actually_found_the_adapters() -> None:
    """**The guard on the guard, and it is not ceremony.** Every parametrised test below is
    vacuously green over an empty list, so a discovery that silently stops matching turns
    this whole file into a suite that passes while checking nothing. That has happened here
    before: `sweep_traceability` asked whether a set was empty rather than whether a member
    was in it and printed "all traceable" for its entire life.

    Pinned at a floor rather than an exact number, so adding an adapter does not fail a test
    about discovery, and removing all of them does."""
    names = {module for module, _ in ADAPTERS}

    assert len(ADAPTERS) >= 5, f"discovery found only {names}"
    assert {"whatsapp", "email", "slack", "telegram"} <= names


@pytest.mark.parametrize(("module", "cls"), ADAPTERS, ids=lambda v: getattr(v, "__name__", v))
def test_an_adapter_declares_the_channel_it_is_for(module: str, cls: type[Any]) -> None:
    """The declaration names a real `Channel`, and `assert_can_send` and every classification
    check downstream key on it. An adapter answering for the wrong channel is checked against
    another surface's ceiling."""
    del module
    assert _capabilities(cls).channel in set(Channel)


@pytest.mark.parametrize(("module", "cls"), ADAPTERS, ids=lambda v: getattr(v, "__name__", v))
def test_no_adapter_offers_cards_on_a_channel_that_cannot_approve(
    module: str, cls: type[Any]
) -> None:
    """**A button on a surface that cannot carry `approve` is an approval that fails at the
    press.** `CHANNEL_VERBS` is the authority on what a channel may do, and a card is only
    honest where a press could be honoured. The alternative is a person pressing Approve, the
    gate refusing the verb, and the person reasonably believing they approved something.

    Slack's author and Telegram's author each reached this conclusion alone and wrote it into
    their own docstrings. This is the same argument in one place, where the next adapter has
    to meet it rather than rediscover it.

    Delete this and `Feature.CARDS` can be declared on a read-only surface because the vendor
    supports buttons, which is true and is not the question."""
    del module
    declared = _capabilities(cls)
    if Feature.CARDS not in declared.features:
        return

    verbs = CHANNEL_VERBS[declared.channel]
    assert "approve" in verbs, (
        f"{declared.channel} declares CARDS and carries {sorted(verbs)}; a press there could "
        "never be honoured, so the card fails at the press instead of refusing at the build"
    )


@pytest.mark.parametrize(("module", "cls"), ADAPTERS, ids=lambda v: getattr(v, "__name__", v))
def test_every_adapter_states_a_classification_ceiling(module: str, cls: type[Any]) -> None:
    """A surface with no declared ceiling is one `assert_can_send` cannot refuse anything on.
    The ceiling is the difference between a restricted field reaching a console and reaching
    a personal handset, and it is a decision each adapter argues rather than inherits."""
    del module
    assert _capabilities(cls).max_classification is not None


@pytest.mark.parametrize(("module", "cls"), ADAPTERS, ids=lambda v: getattr(v, "__name__", v))
def test_no_two_adapters_claim_the_same_channel(module: str, cls: type[Any]) -> None:
    """Two adapters for one channel means `assert_can_send` is asked of whichever the caller
    happened to import, and the two can disagree about the ceiling. Checked per adapter so
    the failure names the duplicate."""
    del module
    mine = _capabilities(cls).channel
    others = [c for m, c in ADAPTERS if c is not cls and _capabilities(c).channel is mine]

    assert not others, f"{mine} is claimed by {cls.__name__} and {[o.__name__ for o in others]}"


#: Modules a channel adapter must not import. Transport belongs on the other side of the
#: adapter, which is what makes the case worth testing - a template filled from the wrong
#: person's payload - testable without a live account.
FORBIDDEN_IMPORTS = frozenset(
    {"socket", "requests", "httpx", "urllib.request", "aiohttp", "smtplib"}
)


@pytest.mark.parametrize("module", sorted({m for m, _ in ADAPTERS}))
def test_no_adapter_imports_a_transport(module: str) -> None:
    """Every adapter here says in its own docstring that it opens no socket and holds no
    credential. That is a claim about the source, so it is checked against the source.

    Parsed rather than searched, because each of these modules discusses transport at length
    in prose: a substring test would match the paragraph explaining why there is no HTTP
    client. `tests/unit/test_email.py` records two attempts that failed exactly that way.

    Delete this and the first adapter to grow a convenience `httpx` call makes the whole
    channel layer untestable without a live vendor account."""
    tree = ast.parse((CHANNELS_DIR / f"{module}.py").read_text(encoding="utf-8"))

    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)

    offenders = sorted(
        name
        for name in imported
        if name in FORBIDDEN_IMPORTS or name.split(".")[0] in {"requests", "httpx", "aiohttp"}
    )
    assert not offenders, f"brain.channels.{module} imports transport: {offenders}"
