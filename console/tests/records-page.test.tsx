/**
 * The records screen: the first place in this console where somebody's data reaches a page.
 *
 * Three properties matter more than the rest, and each of them is a way this screen could be
 * wrong while looking right.
 *
 * **It must ask the route only what the route declares.** `GET /api/v1/records/{entity}` has
 * one query parameter. A filter box would send `filter.owner`, FastAPI would ignore it, and
 * the grid would answer with every row it was already showing while a person read it as the
 * matching ones. `paging.ts` calls that the worst failure available because the grid still
 * returns rows, and the parameter names are read out of the API's own document rather than
 * listed here.
 *
 * **A withheld field must still get a column.** `brain.core.redaction` deletes the key from
 * the record and reports the field in `locked`, so a column list derived from the rows alone
 * has nowhere to render the lock and the one thing the screen exists to show disappears
 * quietly.
 *
 * **Nothing around the table may count anything.** The grid holds that rule for the table;
 * this holds it for the page, which is where a heading saying how many records there are
 * would go.
 *
 * Task ids: M32.5.1.2, M32.5.2.1, M32.5.2.2
 */

import { createMemoryRouter, RouterProvider } from "react-router-dom";
import { render, waitFor } from "@testing-library/react";
import { beforeAll, describe, expect, test } from "vitest";
import { fieldOfLockedCell, lockedCellKey, LOCKED_CELL_SEPARATOR } from "../src/components/paging";
import { valueCell } from "../src/components/cells";
import {
  columnNamesFor,
  ENTITY_FIELD,
  LIMIT_FIELD,
  MAX_LIMIT,
  MIN_LIMIT,
  readLimit,
  RECORDS_QUERY_SCHEMA,
  recordsAddress,
  recordsApiPath,
  rowIdentity,
  submittedQuery,
} from "../src/pages/recordsQuery";
import { Lock } from "../src/ui/Lock";
import { fakeIdentityProvider, loadConsole, signIn, type FakeIdp } from "./support/auth";
import { declaredParameterSchema, declaredQueryParameters } from "./support/openapi";
import { backendRowLimits } from "./support/python";
import { readRepoFile } from "./support/repo";

const RECORDS_OPERATION = "/api/v1/records/{entity}";
const CONSOLE_ORIGIN = "https://console.test";

interface Answer {
  readonly status?: number;
  readonly body: unknown;
  readonly traceId?: string;
}

/**
 * Transform the split route once, before anything is timed.
 *
 * This route is code-split, so the first mount of it in this file is the first time Vite
 * transforms `@rjsf/core`, ajv and the table library, which takes several seconds and has
 * nothing to do with the property under test. Without this the first test in the file fails
 * on a timeout and every later one passes, which reads as a flake rather than as a cold
 * cache. `vi.resetModules()` still re-evaluates the module for each test; only the transform
 * is being warmed here.
 */
beforeAll(async () => {
  await import("../src/pages/Records");
}, 60_000);

/** A page of records in the shape `brain.api_routes.RecordPage` serialises. */
function page(items: unknown[], locked: unknown[] = []): unknown {
  return {
    items,
    next_cursor: null,
    locked,
    source: "a-system-of-record",
    fetched_at: "2026-09-06T00:00:00Z",
    truncated: false,
  };
}

/**
 * Mount the application's own route table at one address, against a stand-in API.
 *
 * The real table rather than a copy of it, for the reason `routing.test.tsx` gives: a test
 * that declared its own route for this page would be testing the copy, and the route is half
 * of what is being checked here because the entity is a path segment.
 */
async function consoleAt(
  path: string,
  answer: Answer | null,
): Promise<{ container: HTMLElement; idp: FakeIdp }> {
  const idp = fakeIdentityProvider({
    api(url) {
      if (answer === null || !url.includes("/api/v1/records/")) {
        return null;
      }
      return new Response(JSON.stringify(answer.body), {
        status: answer.status ?? 200,
        headers: {
          "content-type": "application/json",
          ...(answer.traceId ? { "x-trace-id": answer.traceId } : {}),
        },
      });
    },
  });
  const loaded = await loadConsole({ idp });
  await signIn(loaded);
  const { routes } = await import("../src/App");
  const router = createMemoryRouter(routes, { initialEntries: [path] });
  const { container } = render(<RouterProvider router={router} />);
  await waitFor(() => {
    if (!container.querySelector("h1")) {
      throw new Error("the page has not arrived");
    }
  });
  return { container, idp };
}

/** The same, waiting until the grid has an answer rather than a spinner. */
async function gridAt(
  path: string,
  answer: Answer,
): Promise<{ container: HTMLElement; idp: FakeIdp }> {
  const mounted = await consoleAt(path, answer);
  await waitFor(() => {
    if (!mounted.container.querySelector(".grid") || mounted.container.querySelector(".grid__busy")) {
      throw new Error("the grid has no answer yet");
    }
  });
  return mounted;
}

/** Every request this console made to the records route, as URLs. */
function recordRequests(idp: FakeIdp): URL[] {
  return idp.urls
    .filter((url) => url.includes("/api/v1/records/"))
    .map((url) => new URL(url, CONSOLE_ORIGIN));
}

/** The column headings on the screen, in the order they are rendered. */
function headings(container: HTMLElement): string[] {
  return [...container.querySelectorAll(".grid__table th")].map(
    (cell) => cell.textContent ?? "",
  );
}

describe("what the console asks for", () => {
  test("the entity in the address is the entity asked for", async () => {
    // What breaks if this is deleted: the screen stops being linkable. The address is the
    // whole of the state here, so a page that asked for something other than what the
    // address named would send two people who shared a link to two different questions,
    // each of which would look like the other person's permissions.
    const { idp } = await gridAt("/records/clients", { body: page([]) });
    const asked = recordRequests(idp);
    expect(asked).toHaveLength(1);
    expect(asked[0]?.pathname).toBe("/api/v1/records/clients");
  });

  test("the console sends only query parameters the route declares", async () => {
    // What breaks if this is deleted: a parameter the API ignores. FastAPI drops an
    // undeclared query parameter without a word, so a console that sent `filter.owner` would
    // get back unfiltered rows and show them as filtered ones. The declared names are read
    // out of the API's own document, so this is not the console's spelling compared with
    // itself.
    const declared = new Set(declaredQueryParameters(RECORDS_OPERATION, "get"));
    const { idp } = await gridAt("/records/clients", { body: page([]) });

    const sent = [...(recordRequests(idp)[0]?.searchParams.keys() ?? [])];
    expect(sent.length).toBeGreaterThan(0);
    expect(sent.filter((name) => !declared.has(name))).toEqual([]);
  });

  test("the grid offers no filter the route would ignore", async () => {
    // What breaks if this is deleted: a filter box appears the moment somebody gives a
    // column a `filterLabel`, and it does nothing. A control that does nothing is worse than
    // an absent one here, because a person cannot tell it from a filter that matched
    // everything, or from one the API refused.
    const { container } = await gridAt("/records/clients", {
      body: page([{ entity: "client", id: "one", name: "Ada" }]),
    });
    expect(container.querySelectorAll(".grid__filter")).toHaveLength(0);
    expect(container.querySelectorAll(".grid__filters")).toHaveLength(0);
  });

  test("the limit in the address is the limit asked for", async () => {
    // What breaks if this is deleted: the one control a person has over how much they see.
    // There is no cursor on this route, so raising the limit is the only way to find out
    // whether a page was cut short, and an address whose limit was ignored would answer a
    // different question from the one it names.
    const { idp } = await gridAt("/records/clients?limit=100", { body: page([]) });
    expect(recordRequests(idp)[0]?.searchParams.get(LIMIT_FIELD)).toBe("100");
  });

  test("a limit the route would refuse is treated as unstated", async () => {
    // What breaks if this is deleted: a hand-edited address spends a round trip being
    // refused with `HTTPValidationError`, which is not `ErrorBody`, so it reaches a person
    // as "Something went wrong." That reads as a broken console rather than as a number
    // outside the range, and it is avoidable without correcting anybody: the bounds are the
    // route's own and are checked against it below.
    expect(readLimit(String(MAX_LIMIT + 1))).not.toBe(MAX_LIMIT + 1);
    expect(readLimit("0")).not.toBe(0);
    expect(readLimit("abc")).toBeGreaterThanOrEqual(MIN_LIMIT);
    expect(readLimit("abc")).toBeLessThanOrEqual(MAX_LIMIT);
    expect(readLimit("100")).toBe(100);
  });

  test("an address this screen builds always stays inside the console", async () => {
    // What breaks if this is deleted: an open redirect, and there is a live advisory for
    // exactly this. GHSA-wrjc-x8rr-h8h6 is an open redirect via a backslash reaching
    // `<Link>` and `useNavigate`, it covers every react-router from 6.0.0 to 7.17.0, and the
    // only fix is a major version. This screen is the first place in the console where a
    // value somebody typed reaches `navigate`, so the defence has to be here.
    //
    // What holds it up is the constant `/records/` prefix rather than the encoding, and that
    // was checked rather than assumed: removing `encodeURIComponent` leaves every address
    // below inside this origin, because the URL parser folds a backslash into a slash under
    // a path that already starts with one. The encoding is what keeps the address on the
    // right route, and its sibling test below is what fails when it goes. The mutation this
    // one catches is the tempting refactor: returning where the person asked to go.
    const hostile = ["\\\\evil.example", "//evil.example", "/\\evil.example", "http://evil.example", ".."];
    for (const entity of hostile) {
      const address = recordsAddress(entity, 25);
      expect(address.startsWith("/records/"), address).toBe(true);
      expect(new URL(address, CONSOLE_ORIGIN).origin, address).toBe(CONSOLE_ORIGIN);
    }
  });

  test("the entity is encoded on the way into the API path", async () => {
    // What breaks if this is deleted: a name with a space or a slash in it becomes a
    // different request. The person types the name, so anything they can type has to arrive
    // at the route as the one segment they meant, or the 404 they get back is about an
    // address this console invented.
    expect(recordsApiPath("two words")).toBe("/records/two%20words");
    expect(recordsApiPath("a/b")).toBe("/records/a%2Fb");
    expect(recordsAddress("two words", 25)).toBe("/records/two%20words?limit=25");
  });
});

describe("the form the screen is asked through", () => {
  test("the form cannot offer a page size the route refuses", async () => {
    // What breaks if this is deleted: the console's copy of the bounds drifts from the
    // route's, and the form starts offering a number that comes back 422. Both originals are
    // read here: the Python module that enforces the bound, and the OpenAPI document the
    // route publishes. Comparing the console's two constants with each other would be green
    // for every value they could hold.
    const declared = declaredParameterSchema(RECORDS_OPERATION, "get", LIMIT_FIELD);
    const enforced = backendRowLimits();

    expect(declared["maximum"]).toBe(enforced.max);
    expect(MAX_LIMIT).toBe(enforced.max);
    expect(MIN_LIMIT).toBe(declared["minimum"]);

    const limit = (RECORDS_QUERY_SCHEMA.properties ?? {})[LIMIT_FIELD] as Record<string, unknown>;
    expect(limit["maximum"]).toBe(enforced.max);
    expect(limit["minimum"]).toBe(MIN_LIMIT);
  });

  test("the entity is typed and never chosen from a list this console holds", async () => {
    // What breaks if this is deleted: a dropdown of entity names. Whether a company runs a
    // price list or a finance ledger is a fact about that company, and the route answers an
    // unknown entity exactly as it answers a forbidden one so that the difference cannot be
    // read off by trying names. An `enum` on this property would publish the guess it was
    // built from, to everybody, before anybody asked.
    const entity = (RECORDS_QUERY_SCHEMA.properties ?? {})[ENTITY_FIELD] as Record<
      string,
      unknown
    >;
    expect(entity["type"]).toBe("string");
    expect(Object.keys(entity)).not.toContain("enum");
    expect(Object.keys(entity)).not.toContain("examples");
  });

  test("a submission that is not a query navigates nowhere", async () => {
    // What breaks if this is deleted: an address assembled out of a value nobody read. The
    // form hands back an object the library types loosely, and a missing entity turned into
    // a path would ask the API about the empty string, which is a request nobody meant to
    // make and a 404 nobody can interpret.
    expect(submittedQuery(null)).toBeNull();
    expect(submittedQuery({ [LIMIT_FIELD]: 25 })).toBeNull();
    expect(submittedQuery({ [ENTITY_FIELD]: "   " })).toBeNull();
    expect(submittedQuery({ [ENTITY_FIELD]: "clients", [LIMIT_FIELD]: 100 })).toEqual({
      entity: "clients",
      limit: 100,
    });
  });

  test("the screen with no entity asks the API nothing and names no entity", async () => {
    // What breaks if this is deleted: two things at once. The console asks for the rows of
    // the empty string every time somebody opens the section from the menu, and the empty
    // state grows an example: "try clients or invoices" is this console publishing a guess
    // at what the company runs.
    const { container, idp } = await consoleAt("/records", null);
    expect(recordRequests(idp)).toEqual([]);
    expect(container.querySelector(".grid")).toBeNull();
    expect(container.querySelector(".form")).not.toBeNull();
  });
});

describe("what the screen shows", () => {
  test("a field the API withheld from every row still has a column and renders the lock", async () => {
    // What breaks if this is deleted: the lock stops rendering at all, quietly. The redactor
    // deletes a withheld key from the record rather than blanking it, so a column list built
    // from the keys that arrived has no column for it, and the one thing this screen exists
    // to show is missing with nothing on the screen looking wrong. The markup is compared
    // against a standalone lock byte for byte, because a lock that varied by where it was
    // rendered would be a lock that varies.
    const { container } = await gridAt("/records/clients", {
      body: page(
        [
          { entity: "client", id: "one", name: "Ada" },
          { entity: "client", id: "two", name: "Bo" },
        ],
        [
          { entity: "client", record_id: "one", field: "contract_value" },
          { entity: "client", record_id: "two", field: "contract_value" },
        ],
      ),
    });

    expect(headings(container)).toContain("contract_value");
    const locks = container.querySelectorAll(".grid__table tbody .lock");
    expect(locks).toHaveLength(2);
    const standalone = render(<Lock />).container.innerHTML;
    for (const lock of locks) {
      expect(lock.outerHTML).toBe(standalone);
    }
  });

  test("a value that is not a string still reaches its cell", async () => {
    // What breaks if this is deleted: numbers and booleans vanish. A row from the row plane
    // is whatever the projection admitted, so most of a real record is not a string, and a
    // cell renderer that only handled strings would show an empty cell where a figure is.
    // An empty cell is how this console renders an absent value, so the failure would read
    // as "your grants do not reach that" for every number in the table.
    const { container } = await gridAt("/records/clients", {
      body: page([{ entity: "client", id: "one", amount: 1200, active: true, note: "" }]),
    });
    const body = container.querySelector(".grid__table tbody")?.textContent ?? "";
    expect(body).toContain("1200");
    expect(body).toContain("true");
  });

  test("the page says the same thing whatever number of rows arrived", async () => {
    // What breaks if this is deleted: a count. "Showing 3 records" beside a list filtered by
    // what somebody may see discloses the rest by subtraction, and a page heading is where a
    // console adds one because the grid refused to. Everything outside the table body is
    // compared byte for byte between a page of one row and a page of three, so a number
    // anywhere on the screen fails this.
    const row = (id: string) => ({ entity: "client", id, name: "Ada" });
    const chrome = (markup: string) => markup.replace(/<tbody>[\s\S]*?<\/tbody>/, "<tbody/>");

    const one = await gridAt("/records/clients", { body: page([row("one")]) });
    const three = await gridAt("/records/clients", {
      body: page([row("one"), row("two"), row("three")]),
    });

    expect(chrome(three.container.innerHTML)).toBe(chrome(one.container.innerHTML));
  });

  test("an empty answer reads the same whatever was asked for", async () => {
    // What breaks if this is deleted: the sentence starts naming the question. "Nothing
    // called clients" or "no records match" says the request was the reason, which says
    // there was something for it to exclude. Empty because nothing is there and empty
    // because nothing this caller holds reaches anything are one event with one appearance,
    // and `brain.app.handle_brain_error` gives them one status and one body to keep it so.
    const alpha = await gridAt("/records/alpha", { body: page([]) });
    const beta = await gridAt("/records/beta", { body: page([]) });

    const sentence = (container: HTMLElement) =>
      container.querySelector(".grid__empty")?.outerHTML;
    expect(sentence(alpha.container)).toBeTruthy();
    expect(sentence(beta.container)).toBe(sentence(alpha.container));
  });

  test("a refusal is the API's sentence and the page adds no reading of it", async () => {
    // What breaks if this is deleted: the console explains a 404 that it must not explain.
    // The route answers an unclassified entity, an unregistered one, an ambiguous one and
    // one this caller reaches no column of with the same body, so any wording that
    // distinguishes them, including a sympathetic one about permissions or a helpful one
    // about spelling, rebuilds the difference the taxonomy spent itself removing.
    const { container } = await gridAt("/records/clients", {
      status: 404,
      body: { message: "I could not find that.", trace_id: "" },
      traceId: "trace-records",
    });

    expect(container.querySelector(".notice__body")?.textContent).toBe("I could not find that.");
    expect(container.querySelector(".notice__trace")?.textContent).toContain("trace-records");
    // Not the empty sentence as well: two answers to one question is the beginning of a
    // reader deciding which one is the real one.
    expect(container.querySelector(".grid__empty")).toBeNull();
  });
});

describe("how a column list is built", () => {
  test("the columns are the keys that arrived and the fields that were locked", () => {
    // What breaks if this is deleted: one of the two halves. Without the keys there are no
    // columns for the data; without the locked names there is no column for a withheld
    // field, which is the half that fails silently because the screen still looks complete.
    const names = columnNamesFor(
      [{ id: "one", name: "Ada" }],
      new Set([lockedCellKey("one", "contract_value")]),
    );
    expect(names).toEqual(["contract_value", "id", "name"]);
  });

  test("the column order is a function of the names and not of the payload's order", () => {
    // What breaks if this is deleted: the order a source returned its columns in becomes
    // readable off the screen. `brain.core.redaction` sorts its own redaction list for that
    // reason, and a grid laying columns out in payload order would hand the same fact back
    // one level up.
    const first = columnNamesFor([{ zeta: 1, alpha: 2 }], new Set());
    const second = columnNamesFor([{ alpha: 2, zeta: 1 }], new Set());
    expect(second).toEqual(first);
    expect(first).toEqual(["alpha", "zeta"]);
  });

  test("two callers who were sent different fields each get a packed grid", () => {
    // What breaks if this is deleted: a hole. The sharpest placeholder is not an element, it
    // is a space, and a column reserved for a field that did not arrive would be a gap in
    // exactly the shape of what was withheld. `graph.ts` packs each row of a canvas for the
    // same reason and states the argument at length.
    const wide = columnNamesFor([{ id: "one", name: "Ada", amount: 1 }], new Set());
    const narrow = columnNamesFor([{ id: "one", name: "Ada" }], new Set());
    expect(wide).toEqual(["amount", "id", "name"]);
    expect(narrow).toEqual(["id", "name"]);
  });

  test("a row keeps the API's own id, and a row without one is still its own row", () => {
    // What breaks if this is deleted: locks stop matching. A lock arrives as a record id and
    // a field name and is looked up against exactly this identity, so an invented id makes
    // every lock miss. The fallback exists for a body the redactor did not produce, and it
    // cannot collide with a real id because it contains a space, which `_RECORD_ID_RE` does
    // not admit.
    const rows = [{ id: "one" }, {}, {}];
    const identity = rowIdentity(rows);
    expect(identity.get(rows[0])).toBe("one");
    expect(new Set([...identity.values()]).size).toBe(3);
  });
});

describe("taking a locked-cell key apart", () => {
  test("the separator is a character neither half of a key can contain", () => {
    // What breaks if this is deleted: an ambiguous key. A grid whose columns are not known
    // in advance has to read the field name back out of the key, and a separator that a
    // record id or a field name could hold would make one lock land on the wrong cell. The
    // two patterns are read out of the Python source, so this is the console's separator
    // checked against the API's vocabulary rather than against itself.
    const redaction = readRepoFile("src/brain/core/redaction.py");
    const recordIds = /^_RECORD_ID_RE = re\.compile\(r"([^"]+)"\)$/m.exec(redaction);
    const names = /^_NAME_RE = re\.compile\(r"([^"]+)"\)$/m.exec(redaction);
    expect(recordIds?.[1]).toBeTruthy();
    expect(names?.[1]).toBeTruthy();

    const sample = `a${LOCKED_CELL_SEPARATOR}b`;
    expect(new RegExp(recordIds?.[1] ?? "").test(sample)).toBe(false);
    expect(new RegExp(names?.[1] ?? "").test(sample)).toBe(false);
  });

  test("a key made by one function is read back by the other", () => {
    // What breaks if this is deleted: the pair drifts. The two are each other's inverse and
    // nothing else checks that, so a change to the separator in one of them would show up
    // as locks that never match any cell, which looks exactly like an API that sent none.
    expect(fieldOfLockedCell(lockedCellKey("one", "contract_value"))).toBe("contract_value");
    expect(fieldOfLockedCell("nothing-to-split")).toBe("");
  });
});

describe("what a cell may render", () => {
  test("a structured value is not flattened into one cell", () => {
    // What breaks if this is deleted: a nested record's fields get printed and its own locks
    // do not. A lock is keyed to the record it belongs to, and a child object has its own id,
    // so stringifying one into a parent's cell would show the fields that survived and drop
    // every lock that belonged to them, in the one place nobody would look for them.
    expect(valueCell({ getValue: () => ({ a: 1 }) })).toBeNull();
    expect(valueCell({ getValue: () => [1, 2] })).toBeNull();
    expect(valueCell({ getValue: () => null })).toBeNull();
    expect(valueCell({ getValue: () => "" })).toBeNull();
  });

  test("a primitive is rendered in the API's own spelling", () => {
    // What breaks if this is deleted: the refusal above is satisfied by a cell that renders
    // nothing at all. This is the sibling that proves the renderer still shows a value, and
    // that it shows the value rather than a formatted version of it: a thousands separator
    // or a rounded figure would be the console editing a number out of a system of record.
    expect(valueCell({ getValue: () => "Ada" })).toBe("Ada");
    expect(valueCell({ getValue: () => 1200 })).toBe("1200");
    expect(valueCell({ getValue: () => 0 })).toBe("0");
    expect(valueCell({ getValue: () => false })).toBe("false");
  });
});
