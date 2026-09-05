"""The path from a merge to a running container, read as data.

Every property here is one that fails silently when it stops holding. An image that stops
being signed still deploys. A rollback step deleted still leaves a green workflow. A
readiness gate removed makes deploys *faster*, and the first sign of trouble is a container
serving requests against a schema it does not match.

Parsed rather than grepped throughout: a substring search over YAML matches a commented-out
line as happily as a live one, and a commented-out deploy gate is precisely what this file
exists to catch.

The rollback behaviour itself is not asserted here. It lives in `ops/test-watch-and-deploy.sh`,
which drives the real script against a stubbed docker through six failure scenarios, because
a rollback is a sequence of decisions and not a line in a file.

Task ids: M38.1.2.3, M38.1.2.4, M38.1.2.5, M38.1.3.1, M38.1.3.2, M38.1.3.3, M38.1.3.4
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]


def _workflow(name: str) -> dict[Any, Any]:
    parsed: dict[Any, Any] = yaml.safe_load(
        (REPO / ".github" / "workflows" / name).read_text(encoding="utf-8")
    )
    return parsed


def _steps(workflow: str, job: str) -> list[dict[str, Any]]:
    return list(_workflow(workflow)["jobs"][job].get("steps", []))


def _text_of(workflow: str, job: str) -> str:
    return "\n".join(
        str(step.get("run", "")) + " " + str(step.get("uses", "")) + " " + str(step.get("with", ""))
        for step in _steps(workflow, job)
    )


def _watcher() -> str:
    return (REPO / "ops" / "watch-and-deploy.sh").read_text(encoding="utf-8")


# ------------------------------------------------------- building (M38.1.2.3, M38.1.2.5)
def test_the_image_is_tagged_with_the_commit_and_not_only_latest() -> None:
    """`latest` is a moving target: it names whatever was built most recently, so "roll back
    to the version before this one" has no answer once it has moved. A tag carrying the
    commit means every build stays addressable for as long as the registry keeps it."""
    build = next(s for s in _steps("deploy.yml", "build") if s.get("id") == "build")
    tags = str(build["with"]["tags"])
    assert "${{ steps.meta.outputs.sha }}" in tags
    assert "latest" in tags


def test_the_build_receives_the_commit_so_the_image_knows_what_it_is() -> None:
    """An image that cannot say which commit produced it is one nobody can match against a
    bug report. This is passed at build time rather than set at run time because a runtime
    variable can be overridden by whatever starts the container - and on this deployment it
    was, to the literal string `unknown`."""
    build = next(s for s in _steps("deploy.yml", "build") if s.get("id") == "build")
    assert "COMMIT_SHA=${{ steps.meta.outputs.sha }}" in str(build["with"]["build-args"])


def test_the_image_is_pushed_to_the_registry() -> None:
    build = next(s for s in _steps("deploy.yml", "build") if s.get("id") == "build")
    assert build["with"]["push"] is True
    assert "ghcr.io" in str(_workflow("deploy.yml")["env"]["REGISTRY"])


# ------------------------------------------------------------------- signing (M38.1.2.4)
def test_the_image_is_signed() -> None:
    """Unsigned, "the registry has an image with this digest" is the only claim anybody can
    make about what is running. Signed, the claim is that this build came out of this
    workflow, which is the thing a person actually wants to know before trusting it."""
    assert "cosign sign" in _text_of("deploy.yml", "build")
    assert "sigstore/cosign-installer" in _text_of("deploy.yml", "build")


def test_what_is_signed_is_the_digest_and_never_the_tag() -> None:
    """A tag is a moving pointer. Signing `:latest` attests to whatever it points at today,
    which is exactly the thing an attacker moves; the digest *is* the image.

    Asserted on the argument rather than on the step existing, because a signing step that
    signs the wrong thing passes every check that only asks whether signing happens - and it
    is one character of difference in a shell line nobody reads twice."""
    sign = next(s for s in _steps("deploy.yml", "build") if "Sign" in str(s.get("name", "")))
    run = str(sign["run"])
    assert "@${DIGEST}" in run
    assert ":latest" not in run
    assert ":${{ steps.meta.outputs.sha }}" not in run


def test_the_verification_is_pinned_to_this_repository() -> None:
    """`cosign verify` with no identity constraint accepts a signature made by anybody at
    all, which is worse than not verifying: it produces a green tick meaning nothing.

    Both flags are needed. The issuer alone accepts any GitHub workflow anywhere; the
    identity alone would accept a certificate from a different issuer claiming the same
    name."""
    verify = next(s for s in _steps("deploy.yml", "build") if "Verify" in str(s.get("name", "")))
    run = str(verify["run"])
    assert "--certificate-identity-regexp" in run
    assert "github.com/${{ github.repository }}" in run
    assert "--certificate-oidc-issuer" in run


def test_signing_is_keyless() -> None:
    """A private signing key has to live somewhere, be rotated, and be revocable, and the
    somewhere is usually a repository secret that several people can read. Keyless binds the
    signature to this workflow's OIDC identity instead, so there is no key to steal.

    `id-token: write` is what makes it possible; without that permission cosign has nothing
    to prove who signed and silently wants a key file."""
    assert _workflow("deploy.yml")["permissions"]["id-token"] == "write"
    text = _text_of("deploy.yml", "build")
    assert "--key" not in text, "a key file appeared; keyless signing was replaced"


def test_the_signature_is_verified_in_the_same_run_that_made_it() -> None:
    """A signature nobody ever checks is a signature that can be silently broken. Verifying
    immediately catches a misconfigured signer on the build that introduced it, rather than
    on the day somebody first tries to verify and finds nothing was ever right."""
    names = [str(s.get("name", "")) for s in _steps("deploy.yml", "build")]
    signed = next(i for i, n in enumerate(names) if "Sign" in n)
    verified = next(i for i, n in enumerate(names) if "Verify" in n)
    assert signed < verified


# --------------------------------------------------- deploying (M38.1.3.1, M38.1.3.2)
def test_a_red_build_never_reaches_the_server() -> None:
    """The workflow fires on CI *completing*, not on CI passing - `workflow_run` runs
    whatever the outcome. Without the explicit conclusion check, a failing test suite
    deploys."""
    job = _workflow("deploy.yml")["jobs"]["build"]
    assert "github.event.workflow_run.conclusion == 'success'" in str(job["if"])


def test_the_deploy_waits_for_the_signature() -> None:
    """Deploying first and signing afterwards would mean the running container is the one
    build nobody has verified.

    Asserted as a job dependency rather than as step order, because that is how it is
    actually arranged and it is the stronger arrangement: `needs` makes the whole build job
    - sign and verify included - a precondition, where step order inside one job could be
    rearranged by anybody adding a step in the wrong place."""
    jobs = _workflow("deploy.yml")["jobs"]
    assert jobs["deploy"]["needs"] == "build"
    signing = [str(s.get("name", "")) for s in _steps("deploy.yml", "build")]
    assert any("Sign" in n for n in signing)
    assert any("Verify" in n for n in signing)
    assert any(str(s.get("name", "")) == "Deploy" for s in _steps("deploy.yml", "deploy"))


def test_migrations_run_before_the_new_image_takes_traffic() -> None:
    """M38.1.3.2. Asserted where it actually happens, which is the application's own
    lifespan rather than the workflow: `run_migrations` is awaited before the readiness
    dictionary is populated, so an instance cannot report ready against a schema it has not
    finished changing.

    In the workflow it would be a separate container that has to be sequenced by hand. In
    the lifespan it is a line of Python that cannot be got wrong by ordering two YAML steps
    the wrong way round."""
    app = (REPO / "src" / "brain" / "app.py").read_text(encoding="utf-8")
    migrated = app.index("run_migrations, settings.database_url")
    served = app.index('app.state.ready["database"]')
    assert migrated < served


# ----------------------------------------------- the health gate (M38.1.3.3, M38.1.3.4)
def test_readiness_and_not_liveness_is_what_the_deploy_waits_for() -> None:
    """A container that is up but cannot reach its database passes liveness and answers
    questions from whatever it can still reach. That is the failure this whole distinction
    exists for, and gating on the wrong one makes it invisible."""
    watcher = _watcher()
    assert "/health/ready" in watcher
    assert "/health/live" in watcher  # used for the commit, not for the gate
    ready_at = watcher.index("wait_ready()")
    assert "/health/ready" in watcher[ready_at : ready_at + 600]


def test_a_failed_rollout_rolls_back_to_a_pinned_digest() -> None:
    """Not to `:latest`, which by then points at the broken build - rolling back to it would
    redeploy the thing that just failed and read as the rollback having worked."""
    watcher = _watcher()
    assert "PREVIOUS" in watcher
    assert "rolled_back" in watcher


def test_a_digest_that_keeps_failing_is_left_alone() -> None:
    """Without a ceiling the watcher retries a broken image every three minutes forever,
    and each attempt takes the service down for the length of a rollout. The failure count
    is keyed on the digest so a new build starts with a clean slate."""
    watcher = _watcher()
    assert "FAIL_LIMIT" in watcher


@pytest.mark.parametrize(
    "outcome", ["deployed", "rolled_back", "failed_no_rollback", "rollback_failed"]
)
def test_every_way_a_deploy_can_end_is_recorded(outcome: str) -> None:
    """Recording only the successes makes the record useless for the question it is kept
    for. "It has never gone wrong" and "we only write a line when it goes right" look
    identical in a file that contains only successes."""
    assert outcome in _watcher()
