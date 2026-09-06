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
  readonly requestBody?: {
    readonly content?: Record<string, { readonly schema?: { readonly $ref?: string } }>;
  };
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

/**
 * The parameters one operation declares, in one place in the request, as names.
 *
 * The sibling of `declaredQueryParameters` and deliberately not the same function: that one
 * throws on an empty result, because a subset check against nothing passes for a client that
 * sends nothing. This one returns what it finds, including nothing, and exists for the
 * opposite claim: **a route that declares no query parameter at all is a route where any
 * query string a console sends is discarded in silence and answered 200.** That is a claim
 * about an empty set, so it has to be assertable, and it is only safe to assert beside the
 * other half, which is that the console sent none either.
 *
 * The caller says which half of the request to look in, so the same reader also proves the
 * operation was really parsed: an operation whose path parameters are missing is a stale
 * document or a moved route, and every assertion about its silence would then be vacuous.
 */
export function declaredParameterNames(
  path: string,
  method: string,
  where: "query" | "path" | "header" | "cookie",
): string[] {
  return (operation(path, method).parameters ?? [])
    .filter((parameter) => parameter.in === where)
    .map((parameter) => parameter.name);
}

/**
 * The schema of one property of an operation's request body, with a `$ref` followed.
 *
 * FastAPI emits an enum-typed field as a reference into `components/schemas` rather than
 * inline, so a console checking its copy of a closed vocabulary against the document has to
 * follow one more hop than `declaredRequestBodySchema` does. Following it here rather than
 * at the call site means the call site never names the component, which matters: a component
 * name is a pydantic class name, and a test naming one would keep passing against a document
 * where the field had been repointed at a different enum entirely.
 *
 * Throws on a property the body does not declare, and on a reference that resolves to
 * nothing, for the reason every reader in this directory throws: an empty object turns each
 * bound checked against it into a comparison between two nothings.
 */
export function declaredPropertySchema(
  path: string,
  method: string,
  property: string,
): Record<string, unknown> {
  const body = declaredRequestBodySchema(path, method);
  const properties = body["properties"] as Record<string, unknown> | undefined;
  const found = properties?.[property];
  if (found === undefined || typeof found !== "object" || found === null) {
    throw new Error(
      `${method.toUpperCase()} ${path} declares no body property named ${property}, so the ` +
        "console's copy of its shape is being compared against nothing.",
    );
  }
  const schema = found as Record<string, unknown>;
  const reference = schema["$ref"];
  if (typeof reference !== "string") {
    return schema;
  }
  const name = reference.split("/").at(-1) ?? "";
  const schemas = (apiDocument()["components"] as Record<string, unknown> | undefined)?.[
    "schemas"
  ] as Record<string, Record<string, unknown>> | undefined;
  const resolved = schemas?.[name];
  if (resolved === undefined) {
    throw new Error(
      `${reference} is not in the document's components, so ${property} cannot be read. ` +
        "The document is stale or the reference shape has changed.",
    );
  }
  return resolved;
}

/**
 * The schema of the body one operation accepts, with the `$ref` followed.
 *
 * The request body is where a write route states what it will take, and it is the half of a
 * route's description that a console can be wrong about silently in the other direction from
 * a query parameter: an undeclared query parameter is discarded, an undeclared body key is
 * refused, and a form offering a bound the route does not hold spends a round trip producing
 * `HTTPValidationError`. Both are read from the document rather than agreed out of band.
 *
 * FastAPI emits the body as a `$ref` into `components/schemas`, always, so the reference is
 * resolved here rather than at each call site. A missing or unresolvable reference throws:
 * returning an empty object would turn every bound checked against it into a comparison
 * between two nothings, which is the failure this whole directory exists to prevent.
 */
export function declaredRequestBodySchema(
  path: string,
  method: string,
): Record<string, unknown> {
  const content = operation(path, method).requestBody?.content ?? {};
  const reference = content["application/json"]?.schema?.$ref;
  if (reference === undefined) {
    throw new Error(
      `${method.toUpperCase()} ${path} declares no JSON request body, so anything checked ` +
        "against its shape is being checked against nothing.",
    );
  }
  const name = reference.split("/").at(-1) ?? "";
  const schemas = (apiDocument()["components"] as Record<string, unknown> | undefined)?.[
    "schemas"
  ] as Record<string, Record<string, unknown>> | undefined;
  const found = schemas?.[name];
  if (found === undefined) {
    throw new Error(
      `${reference} is not in the document's components, so the body's declared bounds ` +
        "cannot be read. The document is stale or the reference shape has changed.",
    );
  }
  return found;
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
