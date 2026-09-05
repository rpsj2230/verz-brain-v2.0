"""What must be set, per environment, and what happens when it is not.

The failure this exists to prevent has already happened once here: `DATABASE_URL` was
unset in production, the application skipped migrations because of an `if`, and reported
healthy. Nothing was wrong from the outside. An empty string is a perfectly valid value
for a string field, and a default of `""` turns a missing setting into a working
configuration that does the wrong thing.

So requirements are declared per environment and checked at startup. Development may lack
almost everything — running the documents locally with no Postgres is normal. Production
may lack nothing, and says so loudly rather than degrading into a shape nobody asked for.

The check runs before the server binds a port, so a misconfigured container fails to start
rather than serving wrongly. That is the one case where crashing beats degrading: a
container that will never work should not be in a load balancer's rotation at all.

Task ids: M31.3.1.1, M31.3.1.3
"""

from __future__ import annotations

from dataclasses import dataclass

#: Settings that must be non-empty, by environment. Cumulative: staging inherits
#: development's, production inherits staging's.
REQUIRED: dict[str, tuple[str, ...]] = {
    "development": (),
    "staging": ("database_url",),
    "production": ("database_url", "valkey_url"),
}

#: Settings that must NOT hold an obviously unsafe value outside development. These are
#: the defaults people leave in place, each having been a real breach somewhere.
FORBIDDEN_VALUES: dict[str, tuple[str, ...]] = {
    "database_url": ("postgresql://postgres:postgres@localhost:5432/postgres",),
    "app_role_password": ("change-me", "changeme", "password", "postgres"),
}


@dataclass(frozen=True)
class ConfigProblem:
    setting: str
    problem: str
    fix: str

    def __str__(self) -> str:
        return f"{self.setting}: {self.problem} — {self.fix}"


def required_for(env: str) -> tuple[str, ...]:
    """Cumulative, so a setting required in staging cannot be forgotten in production."""
    order = ("development", "staging", "production")
    if env not in order:
        return REQUIRED["production"]  # unknown means strictest, never laxest
    needed: list[str] = []
    for level in order[: order.index(env) + 1]:
        needed.extend(REQUIRED[level])
    return tuple(dict.fromkeys(needed))


def check(env: str, values: dict[str, str]) -> list[ConfigProblem]:
    """Every problem, not just the first.

    Reporting one at a time turns a misconfigured deployment into a sequence of restarts,
    each revealing the next thing — which is how a five-minute fix takes an hour.
    """
    problems: list[ConfigProblem] = []

    for setting in required_for(env):
        value = (values.get(setting) or "").strip()
        if not value:
            problems.append(
                ConfigProblem(
                    setting=setting,
                    problem=f"required in {env} and not set",
                    fix=f"set {setting.upper()} or BRAIN_{setting.upper()}",
                )
            )

    if env != "development":
        for setting, bad in FORBIDDEN_VALUES.items():
            value = (values.get(setting) or "").strip().lower()
            if value and value in bad:
                problems.append(
                    ConfigProblem(
                        setting=setting,
                        problem="left at a well-known default",
                        fix="generate a real value; this one is in every word list",
                    )
                )

    # A production deployment with the interactive docs reachable publishes the name of
    # every tool and capability in the system. Cheap to check, unpleasant to discover.
    # Per entry, and not only in production. This was `== "*"` against the whole setting, and
    # `serve.py` hands it the origins comma-joined, so `*,https://console.example.com`
    # compared unequal to `*` and passed: a wildcard beside a real origin is the *likely*
    # spelling, because somebody adds the real one and forgets to take the wildcard out.
    # Staging was not covered at all, and staging holds a copy of the same shape of data.
    #
    # For CORS alone this is a browser convenience. It stops being one on the widget mint
    # path, where an allowed origin is permission to mint anonymous credentials against a
    # client's brain, so a wildcard there is every site on the internet.
    #
    # Development keeps the escape hatch: a developer's ports move, and a machine with no
    # client data on it is not what this check is protecting.
    origins = [part.strip() for part in (values.get("cors_origins") or "").split(",")]
    if env != "development" and any(part == "*" for part in origins):
        problems.append(
            ConfigProblem(
                setting="cors_origins",
                problem=f"wildcard origin in {env}",
                fix="name the console and widget origins explicitly; a wildcard beside them "
                "is still a wildcard",
            )
        )

    return problems


def assert_valid(env: str, values: dict[str, str]) -> None:
    """Raise with every problem at once. Called before the server binds a port."""
    problems = check(env, values)
    if not problems:
        return
    lines = "\n".join(f"  - {p}" for p in problems)
    msg = f"configuration is not valid for {env}:\n{lines}"
    raise RuntimeError(msg)
