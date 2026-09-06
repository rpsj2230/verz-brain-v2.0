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
 * What a filter parameter is called, given the column it filters.
 *
 * Prefixed rather than sent as a bare column name, because a bare one collides: a grid over
 * anything with a column called `limit` or `cursor` would send a filter that reads as
 * paging, and the failure would be a page size silently set to whatever somebody typed. The
 * prefix costs nothing and the collision cannot be predicted from here.
 *
 * **This is the one thing in this file that no test can check.** Nothing is mounted under
 * `/api/v1` yet, so no route declares its query parameters and there is no other spelling
 * to agree with. Whoever writes the first list endpoint has to make it match, or change
 * this; a console and an API that disagree here fail as a filter that silently does nothing,
 * which is the worst failure available, because the grid still returns rows.
 */
export function filterParameter(column: string): string {
  return `filter.${column}`;
}

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
      parameters.set(filterParameter(column), value);
    }
  }
  return `?${parameters.toString()}`;
}

/**
 * How a locked cell is named, so a set of them can be looked up while rendering.
 *
 * Two parts joined rather than a nested map, because a lookup during render should be one
 * hash and because the shape a payload carries is a flat list. The record id and the field
 * name both come from the API.
 */
export function lockedCellKey(recordId: string, field: string): string {
  return `${recordId} ${field}`;
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
