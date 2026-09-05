"""The API's description: authentication documented, and nothing else given away.

Task ids: M31.1.4.2
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from pydantic import BaseModel

from brain.api import API_PREFIX
from brain.app import Settings, create_app
from brain.openapi import (
    A_SCHEMA_IS_A_PERMISSION_MAP,
    BEARER_SCHEME,
    DOCUMENTED_BEFORE_IT_IS_ENFORCED,
    PUBLIC_BY_TAG_PRIVATE_BY_DEFAULT,
    PUBLIC_TAGS,
    SCOPES_ARE_NOT_ENUMERATED,
    Audience,
    document,
    is_public_path,
    public_operations,
)


class WaveStatus(BaseModel):
    wave: int
    percent: float


class BuildStatus(BaseModel):
    commit: str
    waves: list[WaveStatus]


class ContractValue(BaseModel):
    client: str
    contract_value: int


def an_app() -> FastAPI:
    """One app carrying every membership case, so one document exercises all of them."""
    app = FastAPI(title="Test", version="1.0")

    @app.get("/health/live", tags=["health"])
    async def live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/build/status", tags=["docs"])
    async def build() -> BuildStatus:
        return BuildStatus(commit="abc", waves=[])

    @app.get(f"{API_PREFIX}/clients", tags=["clients"])
    async def clients() -> ContractValue:
        return ContractValue(client="SNM", contract_value=48_000)

    # A public tag under the versioned prefix. If the prefix rule ever stops holding, this
    # is the route that leaks, and it carries a private model to prove it.
    @app.get(f"{API_PREFIX}/build/status", tags=["docs"])
    async def versioned_build() -> ContractValue:
        return ContractValue(client="SNM", contract_value=48_000)

    @app.get("/internal/agents", tags=["docs", "agents"])
    async def agents() -> ContractValue:
        return ContractValue(client="SNM", contract_value=48_000)

    @app.get("/untagged")
    async def untagged() -> ContractValue:
        return ContractValue(client="SNM", contract_value=48_000)

    return app


def schemas_in(doc: dict[str, Any]) -> set[str]:
    components = doc.get("components", {})
    return set(components.get("schemas", {}))


# ---------------------------------------------------------------- authentication is said
def test_the_document_says_how_a_caller_authenticates() -> None:
    """A document that omits authentication makes every client author guess, and they
    guess the same way: an API key in a query string, which then appears in every access
    log between them and us."""
    doc = document(an_app(), audience=Audience.PUBLIC)
    scheme = doc["components"]["securitySchemes"][BEARER_SCHEME]
    assert scheme["type"] == "http"
    assert scheme["scheme"] == "bearer"
    assert "Authorization: Bearer" in doc["info"]["description"]


def test_authentication_is_the_default_and_each_exception_is_written_down() -> None:
    """The requirement is declared once at the top level and lifted per operation. The
    other way round, marking each private operation, leaves a new route public until
    somebody remembers it."""
    doc = document(an_app(), audience=Audience.INTERNAL)
    assert doc["security"] == [{BEARER_SCHEME: []}]
    assert doc["paths"]["/health/live"]["get"]["security"] == []
    assert "security" not in doc["paths"][f"{API_PREFIX}/clients"]["get"]


def test_the_document_enumerates_no_scopes() -> None:
    """An OAuth2 scheme listing every scope is the capability list again, in the place a
    reader trusts most. What a token may do is computed per request from the holder's
    grants, narrowed by channel and assurance, so there is no fixed list to publish."""
    doc = document(an_app(), audience=Audience.INTERNAL)
    schemes = doc["components"]["securitySchemes"]
    assert all(scheme["type"] != "oauth2" for scheme in schemes.values())
    assert all("scopes" not in scheme for scheme in schemes.values())
    assert "scopes" not in repr(doc["security"])


def test_the_description_says_a_404_is_not_proof_of_absence() -> None:
    """The one sentence that saves a support conversation. A client that treats 404 as
    'does not exist' builds a cache that is wrong for exactly the records the caller is
    not allowed to see, and nobody finds out until the grants change."""
    doc = document(an_app(), audience=Audience.PUBLIC)
    assert "not available to you" in doc["info"]["description"]


# ------------------------------------------------------------------------- membership
def test_an_untagged_route_is_private() -> None:
    """Deny by default. A rule that listed the private routes instead would be one route
    behind the codebase permanently, and the leak would be something somebody forgot."""
    assert is_public_path("/untagged", {}) is False
    assert is_public_path("/untagged", {"tags": []}) is False


def test_a_route_under_the_versioned_prefix_is_private_however_it_is_tagged() -> None:
    """The second, independent condition. The tag rule is the one an author sets on
    purpose; this is the one that holds when they forget."""
    assert is_public_path(f"{API_PREFIX}/anything", {"tags": ["docs"]}) is False


def test_one_private_tag_among_public_ones_makes_an_operation_private() -> None:
    """`all`, not `any`. A route tagged both `docs` and `agents` is an agent route with a
    documentation label on it, and treating it as public publishes the agent surface."""
    assert is_public_path("/internal/agents", {"tags": ["docs", "agents"]}) is False
    assert is_public_path("/build", {"tags": sorted(PUBLIC_TAGS)}) is True


# --------------------------------------------------------------------- the projection
def test_the_public_document_carries_only_the_unauthenticated_routes() -> None:
    """The whole of it. A generated schema served unauthenticated in a permission-aware
    system is a permission map: it names the admin surface and tells anybody which door is
    worth their time."""
    assert public_operations(an_app()) == ("/build/status", "/health/live")


def test_a_private_response_model_is_not_left_behind_in_the_components() -> None:
    """The half of the projection that is easy to forget. FastAPI collects every response
    model in the application into `components.schemas` regardless of which path uses one,
    so removing the private paths and stopping there still publishes the field names of
    every private response, `contract_value` included."""
    doc = document(an_app(), audience=Audience.PUBLIC)
    assert "ContractValue" not in schemas_in(doc)
    assert "contract_value" not in repr(doc)


def test_a_schema_a_public_path_needs_is_kept_with_everything_it_references() -> None:
    """Pruning to a fixed point rather than one pass. A single sweep keeps `BuildStatus`
    and drops the `WaveStatus` it references, leaving a dangling `$ref` that renders as an
    empty object in every viewer rather than as an error anyone would notice."""
    doc = document(an_app(), audience=Audience.PUBLIC)
    assert {"BuildStatus", "WaveStatus"} <= schemas_in(doc)


def test_the_internal_document_keeps_what_the_public_one_drops() -> None:
    """Two documents from one app. If the internal one were also filtered there would be
    nothing for a signed-in integrator to read, and somebody would serve the raw schema."""
    internal = document(an_app(), audience=Audience.INTERNAL)
    assert f"{API_PREFIX}/clients" in internal["paths"]
    assert "ContractValue" in schemas_in(internal)


def test_the_public_document_is_a_subset_of_the_internal_one() -> None:
    """They are projections of one app, not two hand-maintained documents that drift."""
    app = an_app()
    public = set(document(app, audience=Audience.PUBLIC)["paths"])
    internal = set(document(app, audience=Audience.INTERNAL)["paths"])
    assert public < internal


def test_building_a_document_does_not_change_what_the_app_itself_serves() -> None:
    """`FastAPI.openapi()` memoises on the application and hands back the same dict every
    time. Editing it in place would change what `/docs` serves, for the life of the
    process, from anywhere this function happened to be called."""
    app = an_app()
    before = app.openapi()
    document(app, audience=Audience.PUBLIC)
    after = app.openapi()
    assert set(after["paths"]) == set(before["paths"])
    assert f"{API_PREFIX}/clients" in after["paths"]
    assert "securitySchemes" not in after.get("components", {})


# ------------------------------------------------------------- against the real thing
@pytest.fixture
def real_app() -> FastAPI:
    return create_app(Settings(env="development"))


def test_the_running_application_publishes_only_build_and_health_routes(
    real_app: FastAPI,
) -> None:
    """Run against the real application rather than a fixture, so a route added without a
    tag, or under the API prefix with a `docs` tag, shows up here rather than in a scan."""
    paths = public_operations(real_app)
    assert "/health/live" in paths
    assert "/build" in paths
    for path in paths:
        assert not path.startswith(API_PREFIX)


def test_every_public_operation_declares_that_it_needs_nothing(real_app: FastAPI) -> None:
    """Stated rather than left to the reader, who would otherwise apply the top-level
    requirement and send a bearer token to the health check."""
    doc = document(real_app, audience=Audience.PUBLIC)
    for item in doc["paths"].values():
        for operation in item.values():
            assert operation["security"] == []


def test_the_reasons_are_written_down_beside_the_code() -> None:
    """Both of these are rules somebody will be tempted to simplify away: serving the
    generated schema is one line, and adding a scopes map is what every OAuth2 example
    does."""
    assert "permission map" in A_SCHEMA_IS_A_PERMISSION_MAP
    assert "decided per request" in SCOPES_ARE_NOT_ENUMERATED
    assert "one route behind the codebase" in PUBLIC_BY_TAG_PRIVATE_BY_DEFAULT
    # Said out loud rather than implied, because a document describing a check nothing
    # performs is worth less than one that admits the gap.
    assert "No authentication middleware is mounted yet" in DOCUMENTED_BEFORE_IT_IS_ENFORCED
