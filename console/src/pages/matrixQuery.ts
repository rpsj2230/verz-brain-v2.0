/**
 * What the matrix screen asks the API for, what a rung's columns are, and what may be sent
 * back. No React.
 *
 * The split is `recordsQuery.ts`'s, and the same one `paging.ts` and `useServerPage.ts`
 * make: this decides what may be asked and what a column is, `Matrix.tsx` renders it. The
 * reason is the case that is always wrong. What an editor does about a field it must not
 * send cannot be tested through a component that mounts a form library and a table.
 *
 * **The role is shown and is never an input, and that is the whole of what this screen has
 * to get right.** `brain.models.routing.RungRole` gives the reason: "a label a human types
 * drifts from the position and provider it is supposed to describe, and then the console
 * shows a primary sitting third in the chain." M5.3.2 derives the column in a trigger. So
 * `role` is a column here and is absent from `RUNG_EDIT_SCHEMA`, and `submittedEdit` builds
 * its body from four named keys rather than from whatever the form handed back, which means
 * a role appearing in form state has nowhere to travel. The API refuses one anyway; this is
 * the half that stops the console asking. See `A_DERIVED_LABEL_IS_NEVER_AN_INPUT`.
 *
 * **The columns are a fixed list and are deliberately not sorted.** `recordsQuery.ts` sorts
 * its column names and says why: a grid over company data laid out in payload order lets a
 * reader compare two screens and read a source's schema off the difference. Nothing of the
 * sort applies here. `RungView` is one compiled shape, identical in every install and for
 * every caller, so the order is a display decision like `Overview`'s `CALLER_FIELDS`, and
 * the order chosen is the one the chain reads in: which tier, which position, what that
 * makes it, what it points at, and then the four dials.
 *
 * **The matrix is one yes or no, so this screen may say a page was cut short.** The route
 * applies no per-caller row filter, which `brain.routing_routes` states as
 * `THE_MATRIX_IS_NOT_FILTERED_PER_CALLER`: every reader of the matrix is answered every
 * live rung. There is therefore no difference between what exists and what was shown for a
 * number on the page to disclose by subtraction. `truncated` is still carried as a flag and
 * never as a count, because the rule this console keeps is about what a screen may render
 * rather than about which endpoint happens to make a number harmless.
 *
 * **The bounds in the edit form are the route's own bounds.** A form offering a number the
 * API refuses spends a round trip producing a 422, whose body is `HTTPValidationError` and
 * not `ErrorBody`, and which therefore reaches a person as "Something went wrong." The four
 * numbers below are checked in `tests/matrix-page.test.tsx` against the request body schema
 * the route publishes, rather than against each other.
 *
 * Task ids: M5.3.3
 */

import type { RJSFSchema, UiSchema } from "@rjsf/utils";
import type { ReactNode } from "react";
import { chipCell, valueCell } from "../components/cells";
import type { GridColumn } from "../components/DataTable";
import type { components } from "../api/schema";

/** One rung, as `brain.routing_routes.RungView` sends it. */
export type RungRow = components["schemas"]["RungView"];

/**
 * Written down because correcting a wrong-looking role by hand is the most obvious thing a
 * person will want from this screen, and because it is the one edit that must never work.
 */
export const A_DERIVED_LABEL_IS_NEVER_AN_INPUT =
  "A rung's role is what its position and its provider make it. A box for it is a second " +
  "answer to a question the chain has already answered, and the two disagree the moment a " +
  "rung moves: the console then shows a primary sitting third and believes itself. So the " +
  "role has a column and no input, the edit body is built from four named keys rather than " +
  "from the form's own object, and the API forbids the key as well. Three refusals for one " +
  "field, because the day the trigger lands the console must not have to be corrected.";

/**
 * Written down because a matrix is a short list and a heading saying how short is one line.
 */
export const A_MATRIX_PAGE_STILL_CARRIES_NO_COUNT =
  "No number of rungs reaches this screen, and the reason is not that this collection is " +
  "filtered, because it is not. It is that a console keeps one rule about counts rather " +
  "than one rule per endpoint, and a screen that counted here would be the worked example " +
  "somebody copies onto a screen where the collection is filtered. `truncated` says a page " +
  "came back full, which is a fact with no arithmetic in it.";

/**
 * Written down because hiding the editor is presentation and looks exactly like enforcement.
 */
export const A_HIDDEN_EDITOR_IS_NOT_A_REFUSAL =
  "`editable` comes from the API, on the response, recomputed for every request. It decides " +
  "whether an edit control is drawn and it decides nothing else: the request this screen " +
  "makes is identical whichever way it reads, and every save is refused or accepted by the " +
  "route. A console that skipped a request because the flag was false would be enforcing a " +
  "rule in the copy an attacker edits, and one that trusted the flag to mean a save will " +
  "succeed would be holding a permission model with no way to know it had gone stale.";

/** Where the API keeps the matrix. */
export const MATRIX_API_PATH = "/routing/rungs";

/** The one query parameter the matrix route declares. */
export const LIMIT_PARAMETER = "limit";

/**
 * How many rungs to ask for.
 *
 * Below the route's declared maximum, which is asserted rather than assumed: a console
 * asking for more than the route admits is refused with `HTTPValidationError`, which reaches
 * a person as the least useful sentence this console has, and it would do so on every load
 * rather than on an unusual one.
 *
 * There is no control for this and no pager, because the route sends no cursor. A matrix
 * larger than this pages nowhere, and `truncated` is what says so. It is a real gap and a
 * small one: this many rungs across four tiers is twenty-five deep in each.
 */
export const MATRIX_PAGE_SIZE = 100;

/** The console address for the matrix, and for the matrix with one rung open. */
export const MATRIX_PATH = "/routing";

/** Where the API answers one rung's edit. Encoded, because the id travels in the address. */
export function rungApiPath(rungId: string): string {
  return `${MATRIX_API_PATH}/${encodeURIComponent(rungId)}`;
}

/** The whole request this screen makes, query string included. */
export function matrixApiPath(): string {
  return `${MATRIX_API_PATH}?${LIMIT_PARAMETER}=${String(MATRIX_PAGE_SIZE)}`;
}

/**
 * The console address for one rung's editor.
 *
 * Built from a constant prefix and an encoded id, for the reason `recordsAddress` gives:
 * GHSA-wrjc-x8rr-h8h6 is an open redirect through a backslash reaching `useNavigate`, it
 * covers every react-router this project can install, and the defence is that the address
 * starts with a literal path segment.
 */
export function rungAddress(rungId: string): string {
  return `${MATRIX_PATH}/${encodeURIComponent(rungId)}`;
}

/** One page of the matrix, as this console holds it. Three fields, deliberately. */
export interface MatrixPage {
  readonly rungs: readonly RungRow[];
  /** Whether this caller may change what is on the page. Presentation only. */
  readonly editable: boolean;
  /** The page came back full. Never how much more there is. */
  readonly truncated: boolean;
}

const NOTHING: MatrixPage = Object.freeze({ rungs: [], editable: false, truncated: false });

/**
 * Read `brain.routing_routes.RungPage` out of a response body.
 *
 * **`total` and `next_cursor` stop here**, in the same way and for the same reason
 * `readPage` in `paging.ts` drops the total: not "we agree not to render it" but no path
 * from the payload to a renderer. `RungPage` inherits `total` from `brain.api.Page` and
 * never populates it, and a screen holding the field is a screen one line away from
 * showing it.
 *
 * An unreadable body yields an empty page rather than throwing, which is the opposite of
 * what `readPage` does, and the difference is what the caller can do about it. A grid over
 * an entity distinguishes "the API answered nothing" from "the API answered rubbish"
 * because both are reachable in production; here the shape is fixed by a response model in
 * this repository, so a body that is not a page is a console built against a different API
 * and there is no sentence worth composing about it. `Matrix.tsx` shows the API's own
 * failure when there is one, and an empty grid when there is not.
 */
export function readMatrixPage(payload: unknown): MatrixPage {
  if (typeof payload !== "object" || payload === null) {
    return NOTHING;
  }
  const body = payload as { items?: unknown; editable?: unknown; truncated?: unknown };
  if (!Array.isArray(body.items)) {
    return NOTHING;
  }
  return {
    rungs: body.items as RungRow[],
    editable: body.editable === true,
    truncated: body.truncated === true,
  };
}

/** The rung with this id among the ones on the page, or null. */
export function rungById(rungs: readonly RungRow[], rungId: string): RungRow | null {
  return rungs.find((rung) => rung.id === rungId) ?? null;
}

/**
 * One clause of a rung's scope, in the API's own three words.
 *
 * The console supplies the spaces and nothing else: the field name, the operator and the
 * value are all `brain.core.scope.Clause`'s, unchanged. There is no table of friendlier
 * operator words here for the reason `ui/Status.tsx` renders a state word exactly as it
 * arrived: `prefix` is the word the grant tables, the query compiler and every support
 * conversation use, and "starts with" would be a fourth vocabulary.
 *
 * A value that is not a string is rendered as its own JSON. `Op.IN` carries a list and
 * `Op.ANY` carries nothing, and both are payload shapes rather than prose; joining a list
 * with commas would read as a conjunction, which is the opposite of what IN means.
 */
export function clauseText(clause: unknown): string {
  if (typeof clause !== "object" || clause === null) {
    return "";
  }
  const { field, op, value } = clause as { field?: unknown; op?: unknown; value?: unknown };
  if (typeof field !== "string" || typeof op !== "string") {
    return "";
  }
  if (value === null || value === undefined) {
    return `${field} ${op}`;
  }
  return `${field} ${op} ${typeof value === "string" ? value : JSON.stringify(value)}`;
}

/**
 * The clauses of one rung's scope, as text.
 *
 * A scope with no clauses yields no lines, and the cell is empty. That is the honest
 * rendering: an unrestricted scope narrows nothing, and a word like "all" would be this
 * console naming a state the payload does not carry. Every other empty cell in this console
 * means the same thing, which is that there was nothing there to show.
 */
export function scopeLines(scope: unknown): string[] {
  if (typeof scope !== "object" || scope === null) {
    return [];
  }
  const clauses = (scope as { clauses?: unknown }).clauses;
  if (!Array.isArray(clauses)) {
    return [];
  }
  return clauses.map(clauseText).filter((line) => line !== "");
}

/**
 * How each column of a rung is rendered.
 *
 * A list rather than twelve column definitions, so that "every field the API sends about a
 * rung reaches the screen" is a property a test can hold against `brain.routing_routes.
 * RungView` rather than something somebody checks by eye. `tests/matrix-page.test.tsx`
 * reads the model's field names out of the Python source and fails in either direction: a
 * field added there and not here would arrive and be dropped silently, which is the failure
 * nobody notices.
 *
 * `chip` says the value is a short word from a closed vocabulary. It carries no colour and
 * no severity, so nothing here can decide that a disabled rung is alarming; `enabled` is a
 * plain value for exactly that reason, rendered as the API's own `true` or `false`.
 */
export const MATRIX_COLUMNS: readonly {
  readonly name: keyof RungRow;
  readonly as: "value" | "chip" | "scope";
}[] = [
  { name: "tier", as: "chip" },
  { name: "position", as: "value" },
  { name: "role", as: "chip" },
  { name: "provider", as: "chip" },
  { name: "model", as: "value" },
  { name: "deployment_id", as: "value" },
  { name: "scope", as: "scope" },
  { name: "attempts", as: "value" },
  { name: "timeout_seconds", as: "value" },
  { name: "max_concurrency", as: "value" },
  { name: "enabled", as: "value" },
  { name: "id", as: "value" },
];

/**
 * The id of the column carrying the edit control.
 *
 * Not a field of `RungView`, and named here so the test that compares the grid's columns
 * with the model's fields can account for exactly one that is not one. A column with a
 * field's name would be an edit control masquerading as data.
 */
export const EDIT_COLUMN = "edit";

/** What the edit column's cell is given: the rung's id, and a renderer supplied by the page. */
export type EditCell = (rungId: string) => ReactNode;

/**
 * The grid's columns, given whether this caller may change anything.
 *
 * The only difference the flag makes is whether the edit column exists. Nothing about the
 * request changes, nothing about the other columns changes, and no row is withheld: see
 * `A_HIDDEN_EDITOR_IS_NOT_A_REFUSAL`.
 *
 * The header is the API's own field name, unchanged, for the reason `recordsQuery.ts` gives:
 * the API owns the vocabulary, and a console renaming `max_concurrency` to "Concurrency"
 * would show a word no constraint, no docstring and no support conversation uses.
 *
 * No column carries `meta.filterLabel`. The matrix route declares `limit` and nothing else,
 * and a filter box whose parameter no route declares is discarded by FastAPI without a word,
 * leaving a person reading unfiltered rows as the matching ones.
 */
export function matrixColumns(editable: boolean, editCell: EditCell): GridColumn<RungRow>[] {
  const columns: GridColumn<RungRow>[] = MATRIX_COLUMNS.map((column) => ({
    id: column.name,
    header: column.name,
    // `accessorFn` rather than `accessorKey`, for the reason the records grid gives: a key
    // is a path expression to the table library, so a field named `a.b` would silently read
    // a nested object that is not there.
    accessorFn: (rung: RungRow) => rung[column.name],
    cell: column.as === "chip" ? chipCell : column.as === "scope" ? scopeCell : valueCell,
  }));
  if (!editable) {
    return columns;
  }
  return [
    ...columns,
    {
      id: EDIT_COLUMN,
      header: EDIT_COLUMN,
      accessorFn: (rung: RungRow) => rung.id,
      cell: (context: { getValue: () => unknown }) => {
        const rungId = context.getValue();
        return typeof rungId === "string" && rungId !== "" ? editCell(rungId) : null;
      },
    },
  ];
}

/** A scope's clauses, one line each. Declared beside the columns that use it. */
function scopeCell(context: { getValue: () => unknown }) {
  const lines = scopeLines(context.getValue());
  return lines.length === 0 ? null : lines.join("\n");
}

/**
 * The bounds `PATCH /api/v1/routing/rungs/{rung_id}` declares, copied here so the form can
 * refuse a value without spending a request.
 *
 * A copy, and therefore checked against the original rather than against itself: the test
 * reads the request body schema out of the generated OpenAPI document, which is produced
 * from `brain.app.create_app` and carries the bounds pydantic derived from the column's own
 * `Numeric(6, 2)` and from the table's check constraints.
 */
export const MIN_ATTEMPTS = 1;
export const MIN_CONCURRENCY = 1;
export const MAX_SMALLINT = 32767;
export const MAX_TIMEOUT_SECONDS = 9999.99;

/** The four fields a rung's editor may send, in the order the form shows them. */
export const EDITABLE_FIELDS = ["attempts", "timeout_seconds", "max_concurrency", "enabled"] as const;

/**
 * The form one rung is edited through.
 *
 * **A schema written here, from the route's own request model, and not one the API sent.**
 * The same honesty `RECORDS_QUERY_SCHEMA` states about itself: no route returns a JSON
 * Schema, so `formShape` is exercised over a document this console assembled. What makes it
 * more than a hand-written form is that every bound in it is the route's bound and is
 * checked against the route's own description.
 *
 * There is no `role`, no `tier`, no `position` and no deployment field, and each is absent
 * for a reason `brain.routing_routes` sets out: the role is derived, the tier and position
 * are the rung's place in a chain that a unique index governs, and the three deployment
 * fields name a deployment M5.1 has not built a registry for.
 *
 * Frozen and declared once at module level, because `SchemaForm` memoises on the schema's
 * identity and the ajv validator recompiles whenever it changes.
 */
export const RUNG_EDIT_SCHEMA: RJSFSchema = Object.freeze<RJSFSchema>({
  type: "object",
  required: [...EDITABLE_FIELDS],
  properties: {
    attempts: {
      type: "integer",
      title: "attempts",
      minimum: MIN_ATTEMPTS,
      maximum: MAX_SMALLINT,
    },
    timeout_seconds: {
      type: "number",
      title: "timeout_seconds",
      exclusiveMinimum: 0,
      maximum: MAX_TIMEOUT_SECONDS,
    },
    max_concurrency: {
      type: "integer",
      title: "max_concurrency",
      minimum: MIN_CONCURRENCY,
      maximum: MAX_SMALLINT,
    },
    enabled: { type: "boolean", title: "enabled" },
  },
});

/** Presentation only. The submit text is the sentence, so the button says what it does. */
export const RUNG_EDIT_UI: UiSchema = Object.freeze<UiSchema>({
  "ui:submitButtonOptions": { submitText: "Save this rung" },
});

/** What one rung's form starts from. The four values the API answered, and nothing else. */
export function editableDefaults(rung: RungRow): Record<string, unknown> {
  return {
    attempts: rung.attempts,
    timeout_seconds: rung.timeout_seconds,
    max_concurrency: rung.max_concurrency,
    enabled: rung.enabled,
  };
}

/** The body of one edit, as the route's `RungEdit` model declares it. */
export interface RungEdit {
  readonly attempts: number;
  readonly timeout_seconds: number;
  readonly max_concurrency: number;
  readonly enabled: boolean;
}

/**
 * What the editor submitted, or null if it was not an edit.
 *
 * **Assembled from four named keys rather than passed through.** That is the difference
 * between a body this console composed and a body the form library handed over, and it is
 * the property `A_DERIVED_LABEL_IS_NEVER_AN_INPUT` rests on: a `role` in the form's state,
 * from a schema change, a merge or a browser extension, has nowhere here to travel. The
 * route forbids the key as well, and both are wanted: the route stops it arriving and this
 * stops it being sent, so a person never sees a 422 about a field they did not fill in.
 *
 * Null means the shape was not what this screen asked for, and the caller does nothing. A
 * PATCH assembled out of values nobody recognised is a write nobody meant to make, and this
 * is the one screen in the console where a wrong request changes something.
 */
export function submittedEdit(data: unknown): RungEdit | null {
  if (typeof data !== "object" || data === null) {
    return null;
  }
  const fields = data as Record<string, unknown>;
  const attempts = fields["attempts"];
  const timeout = fields["timeout_seconds"];
  const concurrency = fields["max_concurrency"];
  const enabled = fields["enabled"];
  if (
    typeof attempts !== "number" ||
    typeof timeout !== "number" ||
    typeof concurrency !== "number" ||
    typeof enabled !== "boolean"
  ) {
    return null;
  }
  return {
    attempts,
    timeout_seconds: timeout,
    max_concurrency: concurrency,
    enabled,
  };
}
