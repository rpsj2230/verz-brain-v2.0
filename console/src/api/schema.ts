/**
 * The API's own types, re-exported from the generated file.
 *
 * **If this module does not resolve, nothing has gone wrong yet.** The generated file is
 * not committed, and TypeScript is telling you the truth: this console has not been
 * pointed at a version of the API. Run it:
 *
 *     npm run api:generate
 *
 * That is two steps. The first exports the internal OpenAPI document from the application
 * in this repository, offline, with no server running; the second turns it into types.
 * Both are development-time steps by construction rather than by convention, because
 * `brain.app.create_app` sets `openapi_url=None` in production and there is no schema
 * endpoint on a deployed instance to fetch from. See `console/README.md` and
 * `console/scripts/export-openapi.py`.
 *
 * Everything is re-exported through this one module so that the generated file is imported
 * in exactly one place. When the generator changes shape, or somebody swaps it for a
 * different one, this is the file that changes and no call site does.
 *
 * **What is in here today.** `CallerView` and `RecordPage`, from `GET /api/v1/me` and
 * `GET /api/v1/records/{entity}`, alongside the health and build-documentation shapes, and
 * `ErrorBody` on every failing response from both. This paragraph used to say there was
 * nothing under `/api/v1` at all, which was true until those routes landed.
 *
 * `RecordPage.total` is typed as nullable and the API never populates it. That is not an
 * omission waiting to be filled in: a count computed behind a permission predicate tells the
 * reader how many rows were removed, so the console must not render one even if a future
 * response carries it.
 */

// Only the two every consumer needs. The generator also emits `operations`, `webhooks` and
// `$defs`; they are left out rather than re-exported on faith, because this file has never
// been compiled against a generated one and a missing export would be a build error in the
// place a reader is least likely to expect it. Add them when something wants them.
export type { components, paths } from "./generated/schema";
