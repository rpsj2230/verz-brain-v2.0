"""A cache hit reaches a person with its age attached, or it does not reach them.

`CachedAnswer.age_label` has existed since the key module was written and its docstring
says plainly that a cached answer which does not say it is cached is a lie of omission.
Nothing in `src/` called it. The label existed, the rule was written down in prose, and
every path that could have served a hit would have served `found.payload` on its own.

**So the rule is enforced by a constructor rather than by a habit.** `return found.payload`
is the obvious thing to write, it is what a reader assumes was computed just now, and no
test anywhere would have failed. `ServedAnswer` turns that into an error instead.

Task ids: M3.5.3
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from brain.gate.answer_cache import (
    AGE_MARKER,
    AgeNotSurfacedError,
    ServedAnswer,
    serve_cached,
    serve_fresh,
)
from brain.gate.cache_key import CachedAnswer

NOW = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)


def _cached(minutes: int) -> CachedAnswer:
    return CachedAnswer(
        key="k_1",
        payload="SNM Construction has 12 hours remaining.",
        stored_at=NOW - timedelta(minutes=minutes),
        source_epochs={"lark_base": 4},
    )


# --------------------------------------------------------------- the hit carries its age
def test_a_served_cache_hit_tells_the_reader_how_old_it_is() -> None:
    """The rule itself. A reader deciding whether to act on an answer needs to know the
    system looked fifteen minutes ago rather than just now, because that is the difference
    between a number they can quote and one they should refresh.

    Delete this and the label can be dropped from `serve_cached` with nothing failing."""
    served = serve_cached(_cached(minutes=4), NOW)

    assert "answered 4 minutes ago" in served.text
    assert served.from_cache is True
    assert served.age_seconds == 240


def test_there_is_no_accessor_that_hands_back_the_answer_without_its_age() -> None:
    """Deliberately absent. A `payload_only()` convenience would be the escape hatch this
    whole type exists to remove, and it would be reached for by the first caller who wanted
    to put the age somewhere else on screen.

    A channel that wants to render the age its own way has `age_seconds` beside the text,
    which is the number rather than a sentence, and does not require taking the sentence
    apart. Delete this and the hatch gets added as a helper.

    Checked as the field set plus the absence of any public method, because `dir` on a
    frozen dataclass lists only the fields that carry defaults and would have reported a
    surface of one."""
    assert set(ServedAnswer.__dataclass_fields__) == {"text", "from_cache", "age_seconds"}

    methods = {
        name
        for name in dir(ServedAnswer)
        if not name.startswith("_") and callable(getattr(ServedAnswer, name, None))
    }
    assert methods == set(), f"a way to get the answer without its age reappeared: {methods}"


def test_the_answer_itself_survives_beside_its_age() -> None:
    """The label is added, not substituted. An enforcement that replaced the answer would
    satisfy every other test here and serve nobody."""
    served = serve_cached(_cached(minutes=90), NOW)

    assert served.text.startswith("SNM Construction has 12 hours remaining.")
    assert "answered 1 hour ago" in served.text


def test_a_cache_hit_whose_text_lost_the_age_is_refused_rather_than_served() -> None:
    """**The failure this type exists for**, and it is not exotic: `return found.payload` is
    the obvious line to write, and it produces an answer the reader assumes is current.

    Constructed directly rather than through `serve_cached`, because the point is that the
    guard holds for a caller who assembled one themselves. Delete this and the check becomes
    a convention that only `serve_cached` follows."""
    with pytest.raises(AgeNotSurfacedError, match="does not say so"):
        ServedAnswer(
            text="SNM Construction has 12 hours remaining.", from_cache=True, age_seconds=240
        )


def test_a_cache_hit_with_no_age_at_all_is_refused() -> None:
    """The other half of the same omission. Text that happens to contain the words while
    the number is missing is a hit nobody can render an age for, and a channel that wanted
    the number would reach into the sentence for it."""
    with pytest.raises(AgeNotSurfacedError, match="a cache hit has an age"):
        ServedAnswer(text=f"answer {AGE_MARKER}4 minutes ago", from_cache=True)


# --------------------------------------------------------------- the fresh answer
def test_a_freshly_computed_answer_carries_no_age_and_needs_none() -> None:
    """The positive case that keeps the guard honest. A rule enforced by requiring the label
    everywhere would put "answered just now" on every answer the system ever computes, which
    is noise that teaches people to ignore the line that matters."""
    served = serve_fresh("SNM Construction has 12 hours remaining.")

    assert served.from_cache is False
    assert served.age_seconds is None
    assert AGE_MARKER not in served.text


def test_a_fresh_answer_claiming_an_age_is_refused() -> None:
    """The inverse mistake, and it matters because it is the one that misleads in the other
    direction: an answer computed this second described as a lookup makes a reader distrust
    something that was in fact current."""
    with pytest.raises(AgeNotSurfacedError, match="no age to declare"):
        ServedAnswer(text="fresh", from_cache=False, age_seconds=0)


# --------------------------------------------------------------- the two cases are distinct
def test_there_is_no_single_entry_point_that_makes_the_age_optional() -> None:
    """`serve_cached` and `serve_fresh` are separate on purpose. One `serve(payload,
    age=None)` would make the cached case the one somebody forgets, which is precisely the
    omission being prevented, and the default would make forgetting it silent.

    Delete this and the two can be merged back into a convenience with a default."""
    assert serve_cached.__doc__ is not None
    assert serve_fresh.__doc__ is not None
    with pytest.raises(TypeError):
        serve_cached(_cached(minutes=1))  # type: ignore[call-arg]


@pytest.mark.parametrize("minutes", [0, 1, 4, 59, 60, 120])
def test_every_age_the_label_can_produce_satisfies_the_guard(minutes: int) -> None:
    """The guard checks for a marker and the label produces several phrasings: just now,
    one minute, several minutes, one hour, several hours. If any of them stopped matching,
    a real hit at that age would be refused and the person would get nothing at all.

    Delete this and a reworded label breaks serving rather than breaking a test."""
    served = serve_cached(_cached(minutes=minutes), NOW)

    assert AGE_MARKER in served.text
    assert served.age_seconds == minutes * 60
