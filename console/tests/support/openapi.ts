/**
 * The API's own description of itself, read as a document rather than as a type.
 *
 * `src/api/schema.ts` re-exports types generated from this file, and types are erased: a
 * console that sent a query parameter no route declares would compile, build and ship, and
 * the failure would be a filter that silently does nothing. So the checks that matter about
 * *requests* have to read the document at run time, which is what this module is for.
 *
 * **It is the API's answer and not this console's copy of it.** The file is produced by
 * `scripts/export-openapi.py` straight out of `brain.app.create_app`, so a constant checked
 * against it is checked against the route, not against another line somebody wrote here.
 * That is the same discipline `support/python.ts` applies to the Python source and it exists
 * for the same reason: the repository has been bitten four times in one day by a test
 * comparing a constant with itself.
 *
 * The document is generated and not committed. A missing file therefore throws with the
 * command that produces it, rather than returning an empty object and turning every
 * assertion built on it into a comparison between two nothings.
 */

import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { CONSOLE_ROOT } from "./repo";

const DOCUMENT = "src/api/generated/openapi.internal.json";

interface Parameter {
  readonly name: string;
  readonly in: string;
  readonly schema?: Record<string, unknown>;
}

interface Operation {
  readonly parameters?: readonly Parameter[];
}

/** The whole document, parsed. Throws when it has not been generated. */
export function apiDocument(): Record<string, unknown> {
  const full = join(CONSOLE_ROOT, ...DOCUMENT.split("/"));
  if (!existsSync(full)) {
    throw new Error(
      `${DOCUMENT} is not there, so nothing in this console is being checked against the ` +
        "API's own description. Run `npm run api:generate`, which needs no running server.",
    );
  }
  return JSON.parse(readFileSync(full, "utf8")) as Record<string, unknown>;
}

/** One operation off the document, by path and method. Throws when the route has moved. */
export function operation(path: string, method: string): Operation {
  const paths = apiDocument()["paths"];
  const byPath = (paths as Record<string, unknown> | undefined)?.[path];
  const found = (byPath as Record<string, unknown> | undefined)?.[method];
  if (found === undefined || typeof found !== "object") {
    throw new Error(
      `The API document has no ${method.toUpperCase()} ${path}. Either the route moved or ` +
        "the document is stale, and either way what was checked against it is unchecked.",
    );
  }
  return found as Operation;
}

/** The query parameters one operation declares. Anything else a client sends is ignored. */
export function declaredQueryParameters(path: string, method: string): string[] {
  const names = (operation(path, method).parameters ?? [])
    .filter((parameter) => parameter.in === "query")
    .map((parameter) => parameter.name);
  if (names.length === 0) {
    throw new Error(
      `${method.toUpperCase()} ${path} declares no query parameter at all. That is either a ` +
        "route that changed shape or a parser that stopped reading, and a subset check " +
        "against an empty set passes for a client that sends nothing.",
    );
  }
  return names;
}

/** The schema of one declared parameter, with its bounds as the route states them. */
export function declaredParameterSchema(
  path: string,
  method: string,
  name: string,
): Record<string, unknown> {
  const found = (operation(path, method).parameters ?? []).find(
    (parameter) => parameter.name === name,
  );
  if (!found?.schema) {
    throw new Error(
      `${method.toUpperCase()} ${path} declares no parameter named ${name} with a schema, ` +
        "so the console's copy of its bounds is being compared against nothing.",
    );
  }
  return found.schema;
}
