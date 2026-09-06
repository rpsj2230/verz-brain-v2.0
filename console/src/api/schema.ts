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
 * **What is in here today.** Nothing useful, and that is a fact about the API rather than
 * about this console: no route is mounted under `/api/v1` yet, so the document describes
 * the health checks and the build documentation pages. The wiring is real; there is simply
 * nothing behind it to be typed against. The export script says so when it runs.
 */

// Only the two every consumer needs. The generator also emits `operations`, `webhooks` and
// `$defs`; they are left out rather than re-exported on faith, because this file has never
// been compiled against a generated one and a missing export would be a build error in the
// place a reader is least likely to expect it. Add them when something wants them.
export type { components, paths } from "./generated/schema";
