/**
 * Asking the API for one page, and what the console refuses to carry back from the answer.
 *
 * Two properties run through everything below. **The server pages and the server filters**,
 * so every page and every filter is a request, and the console's contribution is a cursor
 * it was handed and a query string. And **no count reaches a screen**: `brain.api.Page` may
 * send a total, the console's page has two fields, and the value stops at the reader. That
 * is a missing path rather than a rule about rendering, which is the difference between a
 * property and an intention.
 *
 * The API's own page model and its locked-field model are read out of the Python source, so
 * a field added to either fails here rather than being quietly ignored by a console that
 * was written against an older shape.
 *
 * **The hook is exercised against a stand-in**, which checks this console's half of the
 * conversation and nothing about the API's. The parameter names used to be the part that
 * could not be checked against anything, and that is no longer true:
 * `GET /api/v1/records/{entity}` declares `limit`, `cursor` and a repeatable `filter` taking
 * `column:value`, so the spellings are read out of `brain/api_routes.py` and compared against
 * the console's copies. That check is the point of this file rather than a detail of it: an
 * undeclared query parameter is discarded by FastAPI without a word, so the console's half of
 * a filter failing silently is a screen full of unfiltered rows read as matching ones.
 *
 * Task ids: M32.5.2.1
 */

import { act, renderHook, waitFor } from "@testing-library/react";
import { describe, expect, test, vi } from "vitest";
import {
  CURSOR_PARAMETER,
  FIRST_PAGE,
  LIMIT_PARAMETER,
  UnreadablePage,
  FILTER_PARAMETER,
  FILTER_SEPARATOR,
  MAX_FILTERS,
  MAX_FILTER_TERM_LENGTH,
  back,
  filterTerm,
  filterValueBudget,
  filterableColumn,
  forward,
  lockedCellKey,
  lockedCellsFrom,
  pageQuery,
  readPage,
} from "../src/components/paging";
import {
  backendFilterGrammar,
  backendLockedFieldFields,
  backendPageFields,
  backendRedactionReasons,
} from "./support/python";

interface Row extends Record<string, unknown> {
  readonly id: string;
}

/** A body in the shape `brain.api.Page` describes, plus anything a test wants to smuggle. */
function page(items: Row[], extra: Record<string, unknown> = {}): Response {
  return new Response(JSON.stringify({ items, next_cursor: null, ...extra }), {
    status: 200,
    headers: { "content-type": "application/json" },
  });
}

function failure(status: number, message: string): Response {
  return new Response(JSON.stringify({ message, trace_id: "trace-1" }), {
    status,
    headers: { "content-type": "application/json" },
  });
}

interface StandInApi {
  /** Every URL the console asked for, in order. */
  readonly urls: string[];
  /** Answer the next request with this, and the one after that with the next. */
  answer(...responses: Response[]): void;
  readonly useServerPage: typeof import("../src/components/useServerPage").useServerPage;
}

/**
 * A fresh copy of the hook wired to a stand-in API.
 *
 * Modules are reloaded rather than reset for the reason `tests/support/auth.ts` gives: the
 * client and the session hold state in module-level variables. Nothing signs in here, and
 * that is deliberate: `accessToken` returns null with no session and the request still goes
 * out, which is the console's own rule that a client-side guard must change nothing about
 * what the server is asked.
 */
async function standInApi(): Promise<StandInApi> {
  vi.resetModules();
  vi.stubEnv("VITE_KEYCLOAK_ISSUER", "https://idp.test/realms/brain");
  const urls: string[] = [];
  const queued: Response[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: unknown) => {
      urls.push(String(input));
      const next = queued.shift();
      if (!next) {
        throw new Error(
          `The API was asked for ${String(input)} with nothing queued. Queue an answer so ` +
            "the test says what the API returned instead of relying on a default.",
        );
      }
      return next;
    }),
  );
  const module = await import("../src/components/useServerPage");
  return {
    urls,
    answer(...responses: Response[]) {
      queued.push(...responses);
    },
    useServerPage: module.useServerPage,
  };
}

/** The query part of a URL, parsed. */
function queryOf(url: string): URLSearchParams {
  return new URL(url, "https://console.test").searchParams;
}

/** Every filter term one query carries, in the order it carries them. */
function termsOf(query: URLSearchParams): string[] {
  return query.getAll(FILTER_PARAMETER);
}

describe("reading a page", () => {
  test("the console's page carries the API's items and cursor and nothing else", () => {
    // What breaks if this is deleted: the console's envelope drifts from the API's without
    // anybody noticing which way. The API's field names are read out of the Python model,
    // so a field added there fails here and somebody decides what to do about it, rather
    // than the console silently ignoring a field that turns out to matter.
    const declared = backendPageFields();
    expect(declared).toContain("items");
    expect(declared).toContain("next_cursor");
    expect(declared).toContain("total");
    expect(declared).toHaveLength(3);

    const read = readPage<Row>({ items: [{ id: "a" }], next_cursor: "c1", total: 4700 });
    expect(Object.keys(read).sort()).toEqual(["items", "nextCursor"]);
  });

  test("a total the API sends reaches nothing", () => {
    // What breaks if this is deleted: "showing 20 of 47". The dangerous count is not a count
    // of hidden rows, which nobody would write; it is the honest total beside a list the API
    // filtered by what this caller may see, because the difference is the disclosure and
    // subtraction is free. `brain.core.redaction` withholds a count field whenever the
    // collection it counts was filtered, and this is the console's half: there is no value
    // to render rather than an agreement not to render one.
    const read = readPage<Row>({ items: [], next_cursor: null, total: 4700 });
    expect(JSON.stringify(read)).not.toContain("4700");
    expect(Object.values(read)).not.toContain(4700);
  });

  test("a body that is not a page is a failure rather than an empty page", () => {
    // What breaks if this is deleted: a broken endpoint renders as a screen saying there is
    // nothing here. An empty page is a legitimate answer about somebody's data and a
    // malformed body is a bug; showing the second as the first is the console telling a
    // person something about their records that nobody said.
    expect(() => readPage<Row>(null)).toThrow(UnreadablePage);
    expect(() => readPage<Row>({ next_cursor: "c" })).toThrow(UnreadablePage);
    expect(() => readPage<Row>({ items: [], next_cursor: 7 })).toThrow(UnreadablePage);
  });

  test("a page with no cursor after it is the last page", () => {
    // What breaks if this is deleted: the end of a result set. A missing cursor and a null
    // one both mean there is no more, and reading either as a value would leave a Next
    // button enabled at the end of every list.
    expect(readPage<Row>({ items: [] }).nextCursor).toBeNull();
    expect(readPage<Row>({ items: [], next_cursor: null }).nextCursor).toBeNull();
    expect(readPage<Row>({ items: [], next_cursor: "c1" }).nextCursor).toBe("c1");
  });
});

describe("asking for a page", () => {
  test("a request asks for one page and says where to continue from", () => {
    // What breaks if this is deleted: the console stops asking for a page at all, and the
    // first list endpoint returns whatever its own default is to a console that believed it
    // had said. The cursor is sent back exactly as it was given, because the console cannot
    // construct one and the ordering it encodes is the API's to change.
    const first = queryOf(pageQuery({ limit: 25, cursor: null, filters: {} }));
    expect(first.get(LIMIT_PARAMETER)).toBe("25");
    expect(first.has(CURSOR_PARAMETER)).toBe(false);

    const second = queryOf(pageQuery({ limit: 25, cursor: "OPAQUE-1", filters: {} }));
    expect(second.get(CURSOR_PARAMETER)).toBe("OPAQUE-1");
  });

  test("a filter is spelled the way the route declares it, or it is silently discarded", () => {
    // What breaks if this is deleted: the failure this whole leaf exists to close. FastAPI
    // ignores a query parameter no signature names and answers 200 with unfiltered rows, so
    // a console that spells this wrong shows every row it was already showing while a person
    // reads them as the matching ones. Nothing on the screen or in the body says a filter was
    // dropped. The names are read out of `brain/api_routes.py` rather than compared with the
    // console's own copies, because two copies agree for every value they could hold, and the
    // value these two agreed on until 2026-09-06 was `filter.<column>`, which no route
    // declares.
    const declared = backendFilterGrammar();

    expect(FILTER_PARAMETER).toBe(declared.parameter);
    expect(FILTER_SEPARATOR).toBe(declared.separator);
    expect(MAX_FILTER_TERM_LENGTH).toBe(declared.maxTermLength);
    expect(MAX_FILTERS).toBe(declared.maxFilters);
  });

  test("every term the console builds is one the route's declared pattern admits", () => {
    // What breaks if this is deleted: the console sends a term the route refuses, and the
    // answer is a 422 carrying `HTTPValidationError` rather than `ErrorBody`, which reaches a
    // person as the least useful sentence this console has. The pattern is compiled from the
    // route's own source, so it is the route's grammar being applied and not a second copy of
    // it written here.
    const declared = new RegExp(backendFilterGrammar().termPattern);
    const query = queryOf(
      pageQuery({
        limit: 25,
        cursor: null,
        filters: { owner: "Ada", "contract.value": "400", team_name: "Ops: EMEA" },
      }),
    );

    const terms = termsOf(query);
    expect(terms).toHaveLength(3);
    for (const term of terms) {
      expect(declared.test(term)).toBe(true);
    }
    // The value half may hold further colons; only the first one splits.
    expect(terms).toContain("team_name:Ops: EMEA");
  });

  test("a filter cannot be mistaken for a paging parameter", () => {
    // What breaks if this is deleted: a grid over anything with a column called limit or
    // cursor sends a filter that reads as paging. The failure is a page size set to whatever
    // somebody typed into a filter box, and it looks like a rendering bug. The prefix this
    // module used to carry is gone and the property is not: every term travels inside the
    // declared parameter, so a column named `limit` cannot collide with the limit however it
    // is spelled.
    const query = queryOf(
      pageQuery({ limit: 25, cursor: "OPAQUE-1", filters: { limit: "9", cursor: "x" } }),
    );
    expect(query.get(LIMIT_PARAMETER)).toBe("25");
    expect(query.get(CURSOR_PARAMETER)).toBe("OPAQUE-1");
    expect(termsOf(query).sort()).toEqual(["cursor:x", "limit:9"]);
  });

  test("two filters travel as two terms rather than as the last one typed", () => {
    // What breaks if this is deleted: `set` in place of `append`. The parameter is repeated
    // once per term, so setting it keeps only the column somebody touched last, and the grid
    // answers a narrower question than the boxes on the screen are asking. Every row it shows
    // is a real match, which is what makes it invisible: the reader sees rows matching one
    // filter and believes they match both.
    const query = queryOf(
      pageQuery({ limit: 25, cursor: null, filters: { owner: "Ada", team: "Ops" } }),
    );

    expect(termsOf(query)).toEqual(["owner:Ada", "team:Ops"]);
  });

  test("a blank filter is not a filter and a filled one is never dropped", () => {
    // What breaks if this is deleted: two opposite failures that look like one rule. An empty
    // box sent as a term asks the API to match the empty string and answers with nothing, so
    // clearing a filter empties the list and reads as a permission problem. And a filled box
    // dropped for being too long, or for being one more than the route takes, is a filter
    // that silently did nothing, which is the failure this module refuses: a refused term is
    // visible and a discarded one is not.
    const blank = queryOf(pageQuery({ limit: 25, cursor: null, filters: { owner: "   " } }));
    expect(termsOf(blank)).toEqual([]);

    const tooLong = "x".repeat(MAX_FILTER_TERM_LENGTH * 2);
    const overLong = queryOf(
      pageQuery({ limit: 25, cursor: null, filters: { owner: tooLong } }),
    );
    expect(termsOf(overLong)).toEqual([filterTerm("owner", tooLong)]);

    const many = Object.fromEntries(
      Array.from({ length: MAX_FILTERS + 4 }, (_, index) => [`c${index}`, "v"]),
    );
    const overMany = queryOf(pageQuery({ limit: 25, cursor: null, filters: many }));
    expect(termsOf(overMany)).toHaveLength(MAX_FILTERS + 4);
  });

  test("a filter box holds only what fits in a term, so the column takes its share first", () => {
    // What breaks if this is deleted: the bound is spent on the value alone and a term built
    // from a long column name goes over the route's limit anyway. The budget is the term
    // length less the column and the separator, which is why it is computed per column rather
    // than being one number for every box on the screen.
    expect(filterValueBudget("owner") + "owner:".length).toBe(MAX_FILTER_TERM_LENGTH);
    expect(filterValueBudget("a".repeat(MAX_FILTER_TERM_LENGTH * 2))).toBe(0);
  });

  test("a column name the route's grammar refuses is not one this console asks about", () => {
    // What breaks if this is deleted: a grid over a source whose field names are not the
    // lowercase form the route accepts offers a filter box that can only ever produce a 422.
    // This is a grammar question and never a permission one: which columns a caller may
    // filter on is answered by the compiled projection, in the API, and a second list here
    // would be the wrong list. See `A_FILTER_IS_A_QUESTION_ABOUT_A_COLUMNS_VALUES`.
    const declared = new RegExp(backendFilterGrammar().termPattern);

    for (const column of ["owner", "contract.value", "team_name", "a".repeat(120)]) {
      expect(filterableColumn(column)).toBe(true);
      expect(declared.test(filterTerm(column, "v"))).toBe(true);
    }
    for (const column of ["Owner", "9lives", "", "has space", "a".repeat(121)]) {
      expect(filterableColumn(column)).toBe(false);
      expect(declared.test(filterTerm(column, "v"))).toBe(false);
    }
  });

  test("the same request produces the same query however it was typed", () => {
    // What breaks if this is deleted: the hook keys its effect on this string, so a query
    // whose spelling depended on the order the filters were set in would refetch on changes
    // that changed nothing, and every keystroke in one box would re-ask for the others.
    const one = pageQuery({ limit: 25, cursor: null, filters: { owner: "Ada", team: "Ops" } });
    const other = pageQuery({ limit: 25, cursor: null, filters: { team: "Ops", owner: "Ada" } });
    expect(one).toBe(other);
  });

  test("going back returns to the page before, and the first page has nowhere to go", () => {
    // What breaks if this is deleted: Previous stops working, or starts working at the
    // start of the list. The API sends only a forward cursor, so the console remembers the
    // cursors it was already given; that record is of what the caller already had, and its
    // length is never rendered, because a page number is the shortest route back to a total.
    const second = forward(FIRST_PAGE, "OPAQUE-1");
    const third = forward(second, "OPAQUE-2");

    expect(third.cursor).toBe("OPAQUE-2");
    expect(back(third).cursor).toBe("OPAQUE-1");
    expect(back(back(third)).cursor).toBeNull();
    expect(back(FIRST_PAGE)).toEqual(FIRST_PAGE);
  });
});

describe("a locked cell in a payload", () => {
  test("a locked field is two names and a reason attached to it reaches nothing", () => {
    // What breaks if this is deleted: the reason arrives in the browser and something
    // renders it. `LockedField` carries entity, record id and field and no reason, because
    // the reason is the part that discloses: out of scope says the field exists on records
    // in another department. A payload that grew one, from a middleware being helpful or a
    // model being copied into place, must find nothing here to carry it. The reasons are
    // read out of the Python enum rather than listed, so a new one is covered too.
    const reasons = backendRedactionReasons();
    expect(reasons.length).toBeGreaterThan(0);

    const keys = lockedCellsFrom([
      { entity: "client", record_id: "two", field: "contract_value", reason: reasons[0] },
    ]);

    expect([...keys]).toEqual([lockedCellKey("two", "contract_value")]);
    for (const reason of reasons) {
      expect([...keys].join(" ")).not.toContain(reason);
    }
  });

  test("the locked field model is the three names this reads and no more", () => {
    // What breaks if this is deleted: the API's model grows a field and the console keeps
    // reading two names out of it, which is safe today and is exactly the state that stops
    // being safe quietly. Failing here forces somebody to look at what was added and decide,
    // which is the only review a payload shape gets.
    expect(backendLockedFieldFields().sort()).toEqual(["entity", "field", "record_id"]);
  });

  test("an entry with no record or no field locks nothing", () => {
    // What breaks if this is deleted: a malformed entry produces a key of "undefined
    // undefined", which matches nothing and silently locks no cell, or matches a real cell
    // and locks the wrong one. Both are worse than reading nothing, and neither is visible.
    expect(lockedCellsFrom([{ field: "owner" }, { record_id: "two" }, 7, null]).size).toBe(0);
    expect(lockedCellsFrom("not an array").size).toBe(0);
  });
});

describe("the hook that asks", () => {
  test("a page is asked for from the API, once, with a limit", async () => {
    // What breaks if this is deleted: every other test here is satisfied by a hook that
    // never asks anything. This is the positive sibling, and it also holds the plainest
    // form of the rule: one request, for one page, from the server.
    const api = await standInApi();
    api.answer(page([{ id: "a" }, { id: "b" }]));

    const { result } = renderHook(() => api.useServerPage<Row>("/clients"));
    await waitFor(() => expect(result.current.busy).toBe(false));

    expect(api.urls).toHaveLength(1);
    expect(api.urls[0]).toContain("/api/v1/clients?");
    expect(queryOf(api.urls[0] as string).get(LIMIT_PARAMETER)).toBe("25");
    expect(result.current.rows).toEqual([{ id: "a" }, { id: "b" }]);
  });

  test("the next page is asked for with the cursor the API sent", async () => {
    // What breaks if this is deleted: the console starts paging by itself. Anything it
    // computed would be an offset into an ordering it does not know, over a result set
    // filtered by grants it cannot see, and the symptom is a row appearing on two pages or
    // on none. What goes back is exactly what came out.
    const api = await standInApi();
    api.answer(
      page([{ id: "a" }], { next_cursor: "OPAQUE-1" }),
      page([{ id: "b" }], { next_cursor: null }),
    );

    const { result } = renderHook(() => api.useServerPage<Row>("/clients"));
    await waitFor(() => expect(result.current.hasNext).toBe(true));

    act(() => {
      result.current.showNext();
    });
    await waitFor(() => expect(result.current.rows).toEqual([{ id: "b" }]));

    expect(api.urls).toHaveLength(2);
    expect(queryOf(api.urls[1] as string).get(CURSOR_PARAMETER)).toBe("OPAQUE-1");
    expect(result.current.hasNext).toBe(false);
    expect(result.current.canGoBack).toBe(true);
  });

  test("asking for the page before the first one asks the API for nothing", async () => {
    // What breaks if this is deleted: a console that refetches the page it is already on.
    // Harmless once, and it is the shape that turns into a loop when somebody later makes
    // `showPrevious` reset something. This is also the measurement behind a surviving
    // mutation: `back` returns its argument unchanged at the start of a list, and removing
    // that guard produces a structurally identical position, so the only difference is
    // object identity, and this shows the difference reaches neither the API nor the screen.
    const api = await standInApi();
    api.answer(page([{ id: "a" }]));

    const { result } = renderHook(() => api.useServerPage<Row>("/clients"));
    await waitFor(() => expect(result.current.busy).toBe(false));
    expect(result.current.canGoBack).toBe(false);

    act(() => {
      result.current.showPrevious();
    });

    expect(api.urls).toHaveLength(1);
    expect(result.current.rows).toEqual([{ id: "a" }]);
    expect(result.current.canGoBack).toBe(false);
  });

  test("changing a filter asks the API again from the first page", async () => {
    // What breaks if this is deleted: two failures at once. The filter is applied in the
    // browser to the rows already fetched, which cannot reach a row the API withheld and
    // looks the same as a filter that did; and a cursor from the previous filter is carried
    // across, which is a position in an ordering of a result set that no longer exists. The
    // API would answer that plausibly rather than refusing.
    const api = await standInApi();
    api.answer(
      page([{ id: "a" }], { next_cursor: "OPAQUE-1" }),
      page([{ id: "b" }], { next_cursor: "OPAQUE-2" }),
      page([{ id: "c" }], { next_cursor: null }),
    );

    const { result } = renderHook(() => api.useServerPage<Row>("/clients"));
    await waitFor(() => expect(result.current.hasNext).toBe(true));

    act(() => {
      result.current.showNext();
    });
    await waitFor(() => expect(result.current.rows).toEqual([{ id: "b" }]));

    act(() => {
      result.current.setFilter("owner", "Ada");
    });
    await waitFor(() => expect(result.current.rows).toEqual([{ id: "c" }]));

    const third = queryOf(api.urls[2] as string);
    expect(termsOf(third)).toEqual([filterTerm("owner", "Ada")]);
    expect(third.has(CURSOR_PARAMETER)).toBe(false);
    expect(result.current.canGoBack).toBe(false);
  });

  test("the rows are the API's answer even when they do not match the filter", async () => {
    // What breaks if this is deleted: the hook starts checking the API's work. A console
    // that dropped rows which do not look like a match has applied its own filter on top of
    // the server's, and where the two disagree the browser's copy wins on the screen. What
    // the API returned for a question is the answer to that question.
    const api = await standInApi();
    api.answer(page([{ id: "a" }, { id: "b" }]));

    const { result } = renderHook(() => api.useServerPage<Row>("/clients"));
    await waitFor(() => expect(result.current.busy).toBe(false));

    act(() => {
      result.current.setFilter("owner", "nobody-by-that-name");
    });
    api.answer(page([{ id: "x" }, { id: "y" }]));
    await waitFor(() => expect(result.current.rows).toEqual([{ id: "x" }, { id: "y" }]));
  });

  test("a refusal arrives as a failure and not as an empty page", async () => {
    // What breaks if this is deleted: a 404 renders as a grid with nothing in it. That is
    // the console answering a question the API refused to answer, and the reader has no way
    // to tell it from a list they are entitled to see that happens to be empty. The API's
    // own sentence is what gets shown; see api/errors.ts for why nothing may be added to it.
    const api = await standInApi();
    api.answer(failure(404, "I could not find that."));

    const { result } = renderHook(() => api.useServerPage<Row>("/clients"));
    await waitFor(() => expect(result.current.busy).toBe(false));

    expect(result.current.failure?.status).toBe(404);
    expect(result.current.failure?.message).toBe("I could not find that.");
    expect(result.current.rows).toEqual([]);
  });

  test("a body that is not a page is reported without describing the request", async () => {
    // What breaks if this is deleted: an unreadable answer throws out of a render, or is
    // shown as an empty list. The sentence used is about the console rather than about the
    // data, deliberately: this is the one failure here that is nobody's permission problem,
    // and saying anything about what was asked for would be the console explaining an
    // outcome it did not observe.
    const api = await standInApi();
    api.answer(new Response("[]", { status: 200, headers: { "content-type": "text/plain" } }));

    const { result } = renderHook(() => api.useServerPage<Row>("/clients"));
    await waitFor(() => expect(result.current.busy).toBe(false));

    expect(result.current.failure).not.toBeNull();
    expect(result.current.rows).toEqual([]);
  });

  test("locked cells travel with the page", async () => {
    // What breaks if this is deleted: the locks are read from the answer and dropped before
    // anything can render them, so every withheld field shows as an empty cell. An empty
    // cell says the record has no value there, which is a different and false statement.
    const api = await standInApi();
    api.answer(
      page([{ id: "two" }], {
        locked: [{ entity: "client", record_id: "two", field: "contract_value" }],
      }),
    );

    const { result } = renderHook(() => api.useServerPage<Row>("/clients"));
    await waitFor(() => expect(result.current.busy).toBe(false));

    expect([...result.current.lockedCells]).toEqual([lockedCellKey("two", "contract_value")]);
  });
});
