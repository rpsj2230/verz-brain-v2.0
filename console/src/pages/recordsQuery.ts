/**
 * What a records screen asks the API for, and what it does with the answer. No React.
 *
 * The split is the one `paging.ts` and `useServerPage.ts` make, and the one the Python side
 * keeps between a module that holds a rule and a module that holds a client: this decides
 * what may be asked and what a column is, `Records.tsx` renders it. The reason is the case
 * that is always wrong. What a grid does about a field the caller may not see cannot be
 * tested through a component that mounts a form library and a table.
 *
 * **The console does not know what entities exist, and must not learn.** There is no
 * catalogue route, and adding one to the console by listing names here would be worse than
 * none: `brain.api_routes` answers an unclassified entity, an unregistered one and one this
 * caller reaches no column of with a single 404, precisely so that an installation's shape
 * cannot be mapped by trying names. A list in the console would publish the guess it was
 * built from. So the entity is typed by the person asking, every name is asked the same
 * way, and every refusal comes back the same. See
 * `THE_CONSOLE_DOES_NOT_KNOW_WHAT_ENTITIES_EXIST`.
 *
 * **The columns are a function of the answer.** Not a fixed list, and not the source's
 * order. Two callers entitled to different fields get two grids that are each packed, in
 * the same way and for the same reason `graph.ts` packs each row of a canvas: the sharpest
 * placeholder is not an element, it is a space, and a column reserved for a field that did
 * not arrive is a hole in the shape of what was withheld.
 *
 * **A withheld field still gets a column, because it still gets a lock.**
 * `brain.core.redaction` deletes the key from the record and reports the field in `locked`,
 * so the two halves of a column list are the keys that arrived and the fields that were
 * locked. Deriving columns from the rows alone loses every lock the API sent, which is the
 * quiet version of this failure: nothing looks wrong, and the one thing the screen exists
 * to show is missing.
 *
 * **A column carries a filter because the route declares one, and for no other reason.**
 * `GET /api/v1/records/{entity}` declares `limit`, `cursor` and a repeatable `filter` taking
 * `column:value`. Until 2026-09-06 it declared only `limit`, no column here carried a filter
 * label, and this docstring said so: a box would have sent `filter.owner`, FastAPI would have
 * discarded it without a word, and the grid would have shown unfiltered rows with nothing
 * saying anything was ignored. The route closed that, so the box is offered. See
 * `A_FILTER_IS_A_QUESTION_THE_SERVER_ANSWERS`.
 *
 * Task ids: M32.5.2.1, M32.5.2.2
 */

import type { RJSFSchema, UiSchema } from "@rjsf/utils";
import { valueCell } from "../components/cells";
import type { GridColumn } from "../components/DataTable";
import { DEFAULT_PAGE_SIZE, fieldOfLockedCell, filterableColumn } from "../components/paging";

/**
 * Written down because a dropdown of entities is the first thing anybody will ask for, and
 * because it would be an improvement to the screen and a hole in the system.
 */
export const THE_CONSOLE_DOES_NOT_KNOW_WHAT_ENTITIES_EXIST =
  "The entity is typed, never chosen from a list this console holds. Whether a company " +
  "runs a price list, an HR table or a finance ledger is a fact about that company, and " +
  "brain.api_routes answers an unknown entity, an unclassified one and one this caller " +
  "reaches no column of with one 404 and one sentence so that the difference cannot be " +
  "read off. A list in the browser would publish the guess it was built from, to everybody, " +
  "before anybody asked.";

/**
 * Written down because filtering in the browser is one line shorter than filtering on the
 * server and looks like the same feature.
 */
export const A_FILTER_IS_A_QUESTION_THE_SERVER_ANSWERS =
  "A filter box sends a term to the route and renders whatever comes back. It never narrows " +
  "the rows already on the page, and the difference is the whole of it: a browser-side " +
  "filter searches the rows this caller was given, which cannot reach a row the API withheld " +
  "and looks exactly like a filter that did. The route answers a filter against the compiled " +
  "projection, so a term naming a column this caller may not read is answered as one naming " +
  "a column that does not exist, and that answer is the product rather than an apology.";

/**
 * Written down because a column that cannot be filtered still looks like one that can, and
 * the difference here is grammar rather than permission.
 */
export const WHICH_COLUMNS_MAY_BE_FILTERED_IS_NOT_THIS_CONSOLES_QUESTION =
  "A column gets a box when its name builds a term the route's declared grammar admits, and " +
  "that is the only test applied here. Whether this caller may filter on it is answered by " +
  "the compiled projection, in the API, where the same bound decides what may be selected. " +
  "brain.api_routes refused to hold a second list for the same reason: the entity's " +
  "classification names every column the entity has rather than the ones this caller " +
  "reaches, so checking against one would admit precisely the filter the projection refuses.";

/** The path parameter's name, and the field the query form collects it in. */
export const ENTITY_FIELD = "entity";

/** The one query parameter the records route declares. `paging.ts` sends it. */
export const LIMIT_FIELD = "limit";

/**
 * The bounds the API puts on `limit`, copied from `brain.knowledge.rows`.
 *
 * A copy, and therefore checked against the original rather than against itself:
 * `tests/records-page.test.tsx` reads `MAX_ROW_LIMIT` out of the Python source and reads
 * the same figure out of the generated OpenAPI document, because a form that offers a
 * number the route will refuse spends a round trip producing a 422, whose body is
 * `HTTPValidationError` and not `ErrorBody`, and therefore reaches a person as "Something
 * went wrong."
 */
export const MIN_LIMIT = 1;
export const MAX_LIMIT = 500;

/**
 * The form the records screen is asked for through.
 *
 * **A schema written here, from the route's own parameters, and not one the API sent.**
 * That distinction is the whole of what is honest about this: no route returns a JSON
 * Schema, so `formShape` is exercised over a document this console assembled. What makes it
 * more than a hand-written form is that the two numbers in it are the route's numbers and
 * are checked against the route's own description, so a form that offers a limit the API
 * will refuse fails a test rather than a person.
 *
 * `entity` carries no pattern and no enumeration. Constraining it here would be this
 * console deciding which names are worth asking about, which is the same disclosure as a
 * dropdown arrived at from the other side: a name the form refuses to send is a name the
 * person learns something about without the API ever answering.
 *
 * Frozen and declared once at module level, because `SchemaForm` memoises on the schema's
 * identity and the ajv validator recompiles whenever it changes. A schema built inside the
 * component would be a new schema on every keystroke.
 */
export const RECORDS_QUERY_SCHEMA: RJSFSchema = Object.freeze<RJSFSchema>({
  type: "object",
  required: [ENTITY_FIELD],
  properties: {
    [ENTITY_FIELD]: {
      type: "string",
      title: "Entity",
      minLength: 1,
    },
    [LIMIT_FIELD]: {
      type: "integer",
      title: "Rows to ask for",
      minimum: MIN_LIMIT,
      maximum: MAX_LIMIT,
      default: DEFAULT_PAGE_SIZE,
    },
  },
});

/** Presentation only. The submit text is the sentence, so the button says what it does. */
export const RECORDS_QUERY_UI: UiSchema = Object.freeze<UiSchema>({
  "ui:submitButtonOptions": { submitText: "Show records" },
});

/** A record as it arrives: keys the API chose, values it did not describe. */
export type RecordRow = Record<string, unknown>;

/**
 * The limit to ask for, given whatever the address carried.
 *
 * Anything that is not an integer inside the API's own bounds is treated as unstated. That
 * is parsing rather than correcting: a hand-edited address saying `limit=abc` is not a
 * question the route can answer, and sending it spends a round trip to be told so in a body
 * shaped `HTTPValidationError`, which reaches a person as the console's least useful
 * sentence. The form cannot produce one, because the same bounds are in the schema.
 *
 * It does not clamp. A limit of 900 becomes the default rather than 500, because silently
 * answering a different question from the one in the address is the failure this whole
 * console is arranged against, in miniature.
 */
export function readLimit(raw: string | null): number {
  if (raw === null || raw.trim() === "") {
    return DEFAULT_PAGE_SIZE;
  }
  const value = Number(raw);
  if (!Number.isInteger(value) || value < MIN_LIMIT || value > MAX_LIMIT) {
    return DEFAULT_PAGE_SIZE;
  }
  return value;
}

/** Where the API's rows for one entity live. Encoded, because the name is typed by hand. */
export function recordsApiPath(entity: string): string {
  return `/records/${encodeURIComponent(entity)}`;
}

/** The console address for one entity at one limit. The address is the whole of the state. */
export function recordsAddress(entity: string, limit: number): string {
  return `/records/${encodeURIComponent(entity)}?${LIMIT_FIELD}=${String(limit)}`;
}

/**
 * What the query form submitted, or null if it was not a query.
 *
 * `Form` hands back an object typed `any` by the library, and the fields have already been
 * through the schema, so this is a narrowing rather than a validation. Null means the shape
 * was not what this screen asked for, and the caller does nothing: navigating to an address
 * assembled out of a value nobody recognises is how a typo becomes a request.
 */
export function submittedQuery(data: unknown): { entity: string; limit: number } | null {
  if (typeof data !== "object" || data === null) {
    return null;
  }
  const fields = data as Record<string, unknown>;
  const entity = fields[ENTITY_FIELD];
  if (typeof entity !== "string" || entity.trim() === "") {
    return null;
  }
  const limit = fields[LIMIT_FIELD];
  return {
    entity: entity.trim(),
    limit: typeof limit === "number" ? limit : DEFAULT_PAGE_SIZE,
  };
}

/**
 * The column names for one page: what arrived, and what was locked.
 *
 * Sorted, so the order is a function of the names and not of the order a source returned
 * its columns in. `brain.core.redaction` sorts its own redaction list for that reason, and
 * the same argument applies one level up: a grid that laid its columns out in payload order
 * would let a reader compare two screens and read a source's schema off the difference.
 */
export function columnNamesFor(
  rows: readonly RecordRow[],
  lockedCells: ReadonlySet<string>,
): string[] {
  const names = new Set<string>();
  for (const row of rows) {
    for (const key of Object.keys(row)) {
      names.add(key);
    }
  }
  for (const key of lockedCells) {
    const field = fieldOfLockedCell(key);
    if (field !== "") {
      names.add(field);
    }
  }
  return [...names].sort();
}

/**
 * The columns for one page.
 *
 * The header is the API's own field name, unchanged. There is no table of prettier titles
 * here for the same reason `ui/Status.tsx` renders a state word exactly as it arrived: the
 * API owns the vocabulary, and a console that renamed `contract_value` to "Value" would be
 * showing a word no citation, no grant and no support conversation uses.
 *
 * `accessorFn` rather than `accessorKey`, because a key is a path expression to the table
 * library and a field named `a.b` would silently read a nested object that is not there.
 * Field names cannot contain a dot today; the reader should not have to know that.
 *
 * **A column gets `meta.filterLabel` when the route's grammar admits its name**, which is
 * what gives it a filter box, and the label is the field name for the same reason the header
 * is. Nothing here asks whether this caller may filter on the column: see
 * `WHICH_COLUMNS_MAY_BE_FILTERED_IS_NOT_THIS_CONSOLES_QUESTION`.
 *
 * A locked column gets one too. The lock is per record and per field, so a column withheld on
 * one row is readable on another, and withholding the box on that basis would make the grid's
 * controls a map of one row's permissions drawn across every row.
 */
export function columnsFor(
  rows: readonly RecordRow[],
  lockedCells: ReadonlySet<string>,
): GridColumn<RecordRow>[] {
  return columnNamesFor(rows, lockedCells).map((name) => ({
    id: name,
    header: name,
    accessorFn: (row: RecordRow) => row[name],
    cell: valueCell,
    ...(filterableColumn(name) ? { meta: { filterLabel: name } } : {}),
  }));
}

/**
 * The prefix for a row that arrived without one.
 *
 * It contains a space, which `_RECORD_ID_RE` in `brain.core.redaction` does not admit, so
 * a positional key can never collide with a real record id and can never match a lock.
 */
export const POSITION_KEY_PREFIX = "row ";

/**
 * What identifies each row on this page, by object identity.
 *
 * The API's own id where there is one, because a lock arrives as a record id and a field
 * name and is matched against exactly this. A record with no usable id cannot reach a
 * caller: `brain.core.redaction` drops it rather than serialising it, so the positional
 * fallback is for a body this console did not get from the redactor. It exists anyway
 * because the alternative is two rows sharing the empty string, which React renders as one.
 */
export function rowIdentity(rows: readonly RecordRow[]): ReadonlyMap<RecordRow, string> {
  const identity = new Map<RecordRow, string>();
  rows.forEach((row, index) => {
    const id = row["id"];
    identity.set(row, typeof id === "string" && id !== "" ? id : `${POSITION_KEY_PREFIX}${index}`);
  });
  return identity;
}
