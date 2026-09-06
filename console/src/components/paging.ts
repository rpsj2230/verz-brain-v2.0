/**
 * Asking for one page of something, and reading the answer. No client, no React.
 *
 * The split is the one `brain/ops/limits.py` and `brain/ops/limit_store.py` make on the
 * Python side: this module decides what a page request looks like and what a page response
 * means, and `useServerPage.ts` owns the fetching. The reason is the case that is always
 * wrong: you cannot test what happens at the end of a result set through a module that
 * opens a socket.
 *
 * **A page is a cursor, never an offset, and this is not a preference.** `brain.api`
 * argues it: offset pagination re-reads and re-filters on every page, and under a
 * permission predicate that means the same row can appear twice or disappear between pages
 * when somebody's grants change mid-scroll. A cursor is a position in a stable order. The
 * console cannot construct one and does not try; it sends back what it was given.
 *
 * **The total is dropped on the way in, and that is the whole of this module's
 * contribution to the rule.** `brain.api.Page` carries `items`, `next_cursor` and an
 * optional `total`, and the console's page has two fields. Not "we agree not to render the
 * total": there is no path from the payload to a renderer, because the value stops here.
 *
 * The rule it serves is the one `brain.core.redaction` states as no count of hidden items
 * ever reaching a person, and its second half is the dangerous one. A count does not have
 * to be a count of hidden things to disclose them. "Showing 20 of 47" beside a list filtered
 * to what this caller may see has told them there are 27 things they may not, by
 * subtraction, using a number the API was entitled to compute. The redactor withholds a
 * count field whenever the collection it counts was filtered for that caller; a table is
 * the easiest place in a console to rebuild the same disclosure out of two numbers that are
 * each harmless.
 *
 * So: no total, no page count, no page number, and a pager that says only whether there is
 * more. What is lost is real. A person cannot tell how far through they are, and a jump to
 * the last page is not possible at all. That is the cost of the cursor and of the rule, and
 * it is cheaper than the alternative.
 *
 * Task ids: M32.5.2.1
 */

/**
 * Written down because a total is the most natural thing in the world to put in a table
 * footer, and because the person who adds it will be improving the console.
 */
export const A_PAGE_NEVER_CARRIES_A_COUNT =
  "No count of rows reaches a screen from here, including a count the API was entitled to " +
  "compute. A list filtered by what the caller may see, beside a total that was not, " +
  "discloses the difference by subtraction: showing 20 of 47 says there are 27 things you " +
  "may not see, which is 27 facts you did not have. brain.api.Page may send a total; this " +
  "module drops it, so there is no value to render rather than a convention not to.";

/** How many rows to ask for. A request parameter, never a fact about what exists. */
export const LIMIT_PARAMETER = "limit";

/** Where to continue from. Opaque: the console returns what it was handed. */
export const CURSOR_PARAMETER = "cursor";

/**
 * The wire spelling of the parameter one filter term travels in, repeated once per term.
 *
 * **This was `filter.<column>` until 2026-09-06, and it was wrong the whole time.** The note
 * that stood here said no test could check it, because nothing was mounted under `/api/v1`
 * and there was no other spelling to agree with. `GET /api/v1/records/{entity}` now declares
 * a single repeatable `filter` taking `column:value`, so there is an original, this is a copy
 * of it, and the tests read the original out of the Python source rather than comparing two
 * console constants with each other.
 *
 * **The collision the prefix was invented for is still closed, by the declaration instead.**
 * A grid over a table with a column called `limit` sends `filter=limit:9`, which is a term
 * inside the filter parameter and cannot be read as a page size however it is spelled. The
 * property survives; only the mechanism changed.
 */
export const FILTER_PARAMETER = "filter";

/**
 * What joins the column to the value inside one term.
 *
 * The same argument this file makes for `LOCKED_CELL_SEPARATOR` below, and `brain.api_routes`
 * makes it too: a field name matches `[a-z][a-z0-9_.]*` and so cannot hold a colon, which
 * makes the split at the first one unambiguous and lets a value carry as many more as it
 * likes. A separator either half could contain would make `due:2026` and `due` plus `2026`
 * the same term.
 */
export const FILTER_SEPARATOR = ":";

/**
 * The longest term the route accepts, and therefore the longest this console builds.
 *
 * Spent at the point of entry rather than at the point of sending: a grid bounds what its
 * filter box will hold, so an over-long term cannot be constructed. See
 * `A_TERM_THE_CONSOLE_DROPS_IS_A_FILTER_THAT_SILENTLY_DID_NOTHING` for why the alternative,
 * trimming on the way out, is the failure this module exists to refuse.
 */
export const MAX_FILTER_TERM_LENGTH = 256;

/** How many terms one request may carry, from `brain.api_routes.MAX_FILTERS`. */
export const MAX_FILTERS = 16;

/**
 * The column half of a term, as the route's own pattern admits it.
 *
 * Anchored and applied to the column alone, because that is the half this console chooses.
 * The value half is whatever a person typed, and the route bounds it to text that cannot
 * span two lines in a log; a single-line input cannot produce either character.
 */
export const FILTER_COLUMN_PATTERN = /^[a-z][a-z0-9_.]{0,119}$/;

/** Whether a column name can be asked about at all. A grammar question, never a permission. */
export function filterableColumn(column: string): boolean {
  return FILTER_COLUMN_PATTERN.test(column);
}

/**
 * How much of a term is left for the value, once the column and the separator have taken
 * theirs. What a filter box for that column may hold.
 */
export function filterValueBudget(column: string): number {
  return Math.max(0, MAX_FILTER_TERM_LENGTH - column.length - FILTER_SEPARATOR.length);
}

/** One term, as the parameter carries it. */
export function filterTerm(column: string, value: string): string {
  return `${column}${FILTER_SEPARATOR}${value}`;
}

/**
 * Written down because trimming a term on the way out looks like defensive programming and
 * is the exact failure the rest of this module is built to avoid.
 */
export const A_TERM_THE_CONSOLE_DROPS_IS_A_FILTER_THAT_SILENTLY_DID_NOTHING =
  "A term the route would refuse is sent, not dropped and not truncated. A dropped term is " +
  "a filter box with a word in it and every row still on the screen, read as the matching " +
  "ones; a truncated one asks a different question and answers it convincingly. Both are " +
  "invisible. A refused one is a failure the reader can see, and between an invisible wrong " +
  "answer and a visible bad one the visible one wins. What is bounded is what the box will " +
  "hold, so the term that would be refused is one a person cannot type in the first place.";

/** How many rows a grid asks for when it does not say. */
export const DEFAULT_PAGE_SIZE = 25;

/** One page of results, as this console holds it. Two fields, deliberately. */
export interface PageEnvelope<T> {
  readonly items: readonly T[];
  /** Null when the API said there is no more. Opaque; never parsed. */
  readonly nextCursor: string | null;
}

/** A body that was not a page. A bug in the console or the API, never an answer. */
export class UnreadablePage extends Error {}

/**
 * Read `brain.api.Page` out of a response body.
 *
 * Throws rather than returning an empty page, because an empty page is a legitimate answer
 * and a malformed body is not, and rendering one as the other would turn a broken endpoint
 * into a screen that says there is nothing here. The caller turns this into a failure a
 * person can see; see `useServerPage.ts`.
 */
export function readPage<T>(payload: unknown): PageEnvelope<T> {
  if (typeof payload !== "object" || payload === null) {
    throw new UnreadablePage("A page body must be an object.");
  }
  const fields = payload as { items?: unknown; next_cursor?: unknown };
  if (!Array.isArray(fields.items)) {
    throw new UnreadablePage("A page body must carry an items array.");
  }
  const cursor = fields.next_cursor;
  if (cursor !== undefined && cursor !== null && typeof cursor !== "string") {
    throw new UnreadablePage("A next cursor must be a string or absent.");
  }
  // `total` is read by nothing. It is not omitted here by oversight: see
  // `A_PAGE_NEVER_CARRIES_A_COUNT` above.
  return { items: fields.items as T[], nextCursor: cursor ?? null };
}

/** Where a grid currently is, and how it got there. */
export interface PagePosition {
  /** The cursor this page was fetched with. Null on the first page. */
  readonly cursor: string | null;
  /**
   * The cursors of the pages behind this one, oldest first.
   *
   * The console remembers where it has been because the API does not send a previous
   * cursor. This is a record of what the caller was already given, so it discloses nothing
   * they did not already have, and its length is never rendered: a page number is the
   * shortest path back to "of 47".
   */
  readonly trail: readonly (string | null)[];
}

export const FIRST_PAGE: PagePosition = Object.freeze({ cursor: null, trail: [] });

/** Move on, using the cursor the API sent with the current page. */
export function forward(position: PagePosition, nextCursor: string): PagePosition {
  return { cursor: nextCursor, trail: [...position.trail, position.cursor] };
}

/** Go back to the page before. The first page has nowhere to go and stays put. */
export function back(position: PagePosition): PagePosition {
  if (position.trail.length === 0) {
    return position;
  }
  const previous = position.trail[position.trail.length - 1] ?? null;
  return { cursor: previous, trail: position.trail.slice(0, -1) };
}

/** What a grid is asking for right now. */
export interface PageRequest {
  readonly limit: number;
  readonly cursor: string | null;
  readonly filters: Readonly<Record<string, string>>;
}

/**
 * The query string for one page request, including the leading `?`.
 *
 * Filters are sorted and blank ones are dropped, so the same request produces the same URL
 * every time. That matters more than tidiness: the hook keys its effect on this string, and
 * a query whose spelling depended on the order somebody typed in would refetch on every
 * keystroke that changed nothing.
 *
 * **A blank filter is dropped and a filled one never is.** Those look like the same rule and
 * are opposites. An empty box is a filter nobody asked for, so sending it would ask the API
 * to match the empty string and answer with nothing, which reads as a permission problem;
 * dropping it is what the reader meant. A box with a word in it is a question that was asked,
 * and this builds the term whatever it says, including one too long or one more than the
 * route will take. See `A_TERM_THE_CONSOLE_DROPS_IS_A_FILTER_THAT_SILENTLY_DID_NOTHING`.
 *
 * `append` rather than `set`, because the parameter is repeated once per term and `set`
 * would leave the last column typed in as the only filter sent.
 */
export function pageQuery(request: PageRequest): string {
  const parameters = new URLSearchParams();
  parameters.set(LIMIT_PARAMETER, String(request.limit));
  if (request.cursor !== null) {
    parameters.set(CURSOR_PARAMETER, request.cursor);
  }
  for (const column of Object.keys(request.filters).sort()) {
    const value = (request.filters[column] ?? "").trim();
    if (value !== "") {
      parameters.append(FILTER_PARAMETER, filterTerm(column, value));
    }
  }
  return `?${parameters.toString()}`;
}

/**
 * What joins a record id to a field name in a locked-cell key.
 *
 * **A character neither half can contain, so the join is reversible.** A record id matches
 * `_RECORD_ID_RE` in `brain.core.redaction`, which is `[A-Za-z0-9_.@-]`, and a field name
 * matches `_NAME_RE`, which is `[a-z][a-z0-9_]*`. A separator either of them could hold
 * would make `contract value` and `contract` plus `value` the same key, and a grid would
 * then lock a cell nobody withheld or fail to lock one somebody did.
 *
 * Written as an escape rather than as the character itself, and that is a repair rather
 * than a preference. It was a literal NUL byte in this file until 2026-09-06: the same
 * value, and not the same thing to work with. `grep`, `file` and everything else that stops
 * at a NUL reported this module as binary, so the one module holding the lock lookup was
 * the one module nobody could search.
 */
export const LOCKED_CELL_SEPARATOR = "\u0000";

/**
 * How a locked cell is named, so a set of them can be looked up while rendering.
 *
 * Two parts joined rather than a nested map, because a lookup during render should be one
 * hash and because the shape a payload carries is a flat list. The record id and the field
 * name both come from the API.
 */
export function lockedCellKey(recordId: string, field: string): string {
  return `${recordId}${LOCKED_CELL_SEPARATOR}${field}`;
}

/**
 * The field name inside a key `lockedCellKey` built. Its inverse, and beside it.
 *
 * **A grid whose columns are not known in advance cannot do without this**, because of the
 * half of the lock rule that is easiest to miss: `brain.core.redaction` deletes a withheld
 * key from the record rather than blanking it, and reports it separately in `locked`. So a
 * column list derived only from the keys that arrived has no column for a withheld field,
 * and the lock the API took the trouble to send renders nowhere at all. The lock is the
 * product rather than an apology, so the field name has to come back out of the key.
 *
 * A key with no separator in it yields the empty string rather than the whole key. A column
 * named after a whole key would be a column headed with somebody's record id, which is one
 * row's fact printed across every row.
 */
export function fieldOfLockedCell(key: string): string {
  const gap = key.indexOf(LOCKED_CELL_SEPARATOR);
  return gap === -1 ? "" : key.slice(gap + LOCKED_CELL_SEPARATOR.length);
}

/**
 * The locked cells in a payload, as keys.
 *
 * **Everything except the record id and the field name is dropped, and that is the point.**
 * `brain.core.redaction.LockedField` carries exactly `entity`, `record_id` and `field`, and
 * its docstring says why: it carries no reason, because the reason is the part that leaks.
 * A payload that grew a `reason` field, from a middleware being helpful or a source being
 * copied into place, would find nothing here to carry it: this reads two names and builds a
 * key, so there is no value for a cell renderer to reach for.
 *
 * Anything unreadable is skipped rather than throwing. A lock is a fact about a field, and
 * a malformed entry means one cell renders its value where it should have rendered a lock,
 * which is a disclosure bug worth being loud about. It is not loud here because this is not
 * where it can be caught: the entry that matters is the one that never arrived, and no
 * amount of care in a browser detects a lock the API did not send. The API is the trust
 * boundary; this is a renderer.
 */
export function lockedCellsFrom(value: unknown): ReadonlySet<string> {
  const keys = new Set<string>();
  if (!Array.isArray(value)) {
    return keys;
  }
  for (const entry of value) {
    if (typeof entry !== "object" || entry === null) {
      continue;
    }
    const { record_id: recordId, field } = entry as { record_id?: unknown; field?: unknown };
    if (typeof recordId === "string" && typeof field === "string") {
      keys.add(lockedCellKey(recordId, field));
    }
  }
  return keys;
}
