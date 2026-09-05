"""The seed command, and the guard that stops it running somewhere real.

Task ids: M0.4.4, M0.4.5
"""

from __future__ import annotations

import pytest

from brain import seed as seed_mod


def test_seed_rows_come_from_the_same_fixture_the_canaries_use() -> None:
    """One definition, two consumers. If the seed and the fixture drifted, the thing you
    develop against and the thing the canaries protect would stop being the same system,
    and a permission bug could pass CI and appear only in a demo."""
    principals, grants = seed_mod._seed_rows()
    assert len(principals) == 12
    assert len(grants) > 20

    ids = {p["id"] for p in principals}
    assert "u_weiling" in ids  # sees-record-not-money
    assert "u_expired" in ids  # the lapsed contractor
    assert "svc_sentinel" in ids  # scheduled work is a principal too

    # the expiry travels into the seeded row, not just the fixture object
    expired = next(p for p in principals if p["id"] == "u_expired")
    assert expired["not_after"] is not None


def test_grants_carry_their_scope_as_json() -> None:
    _, grants = seed_mod._seed_rows()
    weiling = [g for g in grants if g["principal_id"] == "u_weiling"]
    assert weiling
    assert all("clauses" in g["scope"] for g in weiling)
    # and the money field is absent, which is the whole point of that persona
    assert not any("contract_value" in g["capability"] for g in weiling)


def test_the_owned_table_list_is_explicit() -> None:
    """Seeding truncates what it owns. What it owns is written down rather than inferred,
    so widening it is a visible edit."""
    # Corrected 5 September: the table M1.4.1 actually creates is `capability_grant`.
    # `gate.grant` named nothing, and `looks_like_production` counts rows in tables it
    # does not own, so the first real grant row would have made the seeder refuse to run.
    assert seed_mod.OWNED == ("auth.principal", "gate.capability_grant")


def test_seed_refuses_when_the_database_holds_rows_it_does_not_own(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """'I ran the seed against production' should be impossible, not discouraged."""
    monkeypatch.setattr(
        seed_mod, "looks_like_production", lambda _url: (True, "know.item (4,102 rows)")
    )
    assert seed_mod.seed("postgresql://x", force=False) == 1


def test_force_overrides_the_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(seed_mod, "looks_like_production", lambda _url: (True, "whatever"))
    assert seed_mod.seed("postgresql://x", force=True) == 0


def test_an_empty_database_seeds_without_force(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(seed_mod, "looks_like_production", lambda _url: (False, ""))
    assert seed_mod.seed("postgresql://x") == 0


def test_main_needs_a_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("BRAIN_DATABASE_URL", raising=False)
    assert seed_mod.main([]) == 2
