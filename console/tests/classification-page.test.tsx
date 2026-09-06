/**
 * The classification screen: the place in this console where a mistake widens what other
 * people may see.
 *
 * Five properties matter more than the rest, and each is a way this screen could be wrong
 * while looking right.
 *
 * **A guard in the browser must change nothing about what the server is asked.** `editable`
 * arrives on the response and decides whether an editor is drawn. A console that also
 * skipped the request, or asked a different one, would be enforcing a rule in the copy an
 * attacker edits, and the requests are compared in order and against the one address this
 * screen has.
 *
 * **The widening verdict is the API's and is never worked out here.** The closure that
 * decides it is the same one that withholds a column at request time, so a copy of it in a
 * browser would be a second answer to what a person may see. The screen is driven with a
 * review that says `widens` and one that does not, and what changes is what is rendered.
 *
 * **Nothing on this screen may read as a save.** There is no route that stores a
 * classification, so a receipt would describe a mechanism that does not exist. Asserted
 * twice: on the API's own document, which declares one GET and one review and nothing that
 * writes, and on the screen after a review comes back.
 *
 * **Nothing around the table may count anything.** The grid holds that rule for the table;
 * this holds it for the page, which is where a heading saying how many columns are
 * confidential would go, and for the widening notice, which is where a number would feel
 * most helpful.
 *
 * **It must ask and send only what the route declares.** This route declares no query
 * parameter at all, and FastAPI discards an undeclared one without a word and answers 200,
 * so both halves are read out of the API's own document rather than agreed here.
 *
 * Task ids: M7.5.3
 */

import { createMemoryRouter, RouterProvider } from "react-router-dom";
import { act, fireEvent, render, waitFor } from "@testing-library/react";
import { beforeAll, describe, expect, test } from "vitest";
import {
  CAPABILITY_PATTERN,
  CLASSIFICATION_COLUMNS,
  CLASSIFICATION_WORDS,
  EDIT_COLUMN,
  MAX_DERIVED_FROM,
  classificationAddress,
  classificationApiPath,
  classificationColumns,
  columnAddress,
  columnEditSchema,
  derivationOptions,
  editableDefaults,
  readClassification,
  readReview,
  reviewApiPath,
  submittedEdit,
  submittedEntity,
  type ColumnRow,
} from "../src/pages/classificationQuery";
import {
  IT_DOES_NOT_WIDEN,
  IT_WIDENS,
  IT_WOULD_NOT_LOAD,
  NOTHING_ASKED_FOR,
  NOTHING_WOULD_CHANGE,
  NO_SUCH_COLUMN,
  THERE_IS_NO_SAVE,
  WHAT_A_WIDENING_MEANS,
} from "../src/pages/Classification";
import { fakeIdentityProvider, loadConsole, signIn, type FakeIdp } from "./support/auth";
import {
  declaredParameterNames,
  declaredPropertySchema,
  declaredRequestBodySchema,
} from "./support/openapi";
import {
  backendClassificationWords,
  backendColumnEditFields,
  backendColumnViewFields,
  backendReviewViewFields,
} from "./support/python";

const READ_OPERATION = "/api/v1/classifications/{entity}";
const REVIEW_OPERATION = "/api/v1/classifications/{entity}/columns/{column}/review";
const CONSOLE_ORIGIN = "https://console.test";
const ENTITY = "price_list";

/**
 * Transform the split route once, before anything is timed.
 *
 * This route is code-split, so the first mount of it in this file is the first time Vite
 * transforms `@rjsf/core`, ajv and the table library, which takes several seconds and has
 * nothing to do with the property under test. Without this the first test in the file fails
 * on a timeout and every later one passes, which reads as a flake rather than as a cold
 * cache. The records and matrix suites warm the same libraries for the same reason.
 */
beforeAll(async () => {
  await import("../src/pages/Classification");
}, 60_000);

/** One classified column, in the shape `brain.classification_routes.ColumnView` serialises. */
function column(overrides: Partial<ColumnRow> = {}): ColumnRow {
  return {
    column: "cost",
    required_capability: "read:price_list.cost",
    classification: "confidential",
    derived_from: ["margin", "sell_price"],
    ...overrides,
  };
}

/** The price list's five columns, as the API answers them. */
function priceList(): ColumnRow[] {
  return [
    column({
      column: "margin",
      required_capability: "read:price_list.margin",
      derived_from: ["cost", "sell_price"],
    }),
    column({
      column: "name",
      required_capability: "read:price_list.name",
      classification: "internal",
      derived_from: [],
    }),
    column(),
    column({
      column: "sell_price",
      required_capability: "read:price_list.sell_price",
      classification: "internal",
      derived_from: [],
    }),
    column({
      column: "sku",
      required_capability: "read:price_list.sku",
      classification: "internal",
      derived_from: [],
    }),
  ];
}

/** One classification, in the shape `ClassificationView` serialises, epoch included. */
function classification(
  columns: ColumnRow[],
  extra: { editable?: boolean; epoch?: string } = {},
): unknown {
  return {
    entity: ENTITY,
    columns,
    epoch: extra.epoch ?? "EPOCH-SENTINEL-NOW",
    editable: extra.editable ?? false,
  };
}

/** One review, in the shape `ReviewView` serialises, both epochs included. */
function reviewed(extra: Record<string, unknown> = {}): unknown {
  return {
    entity: ENTITY,
    column: "cost",
    would_not_load: "",
    changes: [],
    widens: false,
    exposed: [],
    epoch_now: "EPOCH-SENTINEL-NOW",
    epoch_after: "EPOCH-SENTINEL-AFTER",
    ...extra,
  };
}

interface Answers {
  /** The body for the GET, or null to answer nothing at all. */
  readonly classification: unknown;
  /** The status and body for the POST. */
  readonly review?: { status?: number; body?: unknown; traceId?: string };
}

/**
 * Mount the application's own route table at one address, against a stand-in API.
 *
 * The real table rather than a copy of it, for the reason `routing.test.tsx` gives: a test
 * that declared its own route for this page would be testing the copy, and the route is half
 * of what is being checked here because the entity and the column are path segments.
 */
async function consoleAt(
  path: string,
  answers: Answers,
): Promise<{ container: HTMLElement; idp: FakeIdp }> {
  const idp = fakeIdentityProvider({
    api(url, init) {
      if (!url.includes("/api/v1/classifications")) {
        return null;
      }
      if ((init?.method ?? "GET") === "POST") {
        const answer = answers.review ?? { status: 200, body: reviewed() };
        return new Response(JSON.stringify(answer.body ?? reviewed()), {
          status: answer.status ?? 200,
          headers: {
            "content-type": "application/json",
            ...(answer.traceId ? { "x-trace-id": answer.traceId } : {}),
          },
        });
      }
      if (answers.classification === null) {
        return null;
      }
      return new Response(JSON.stringify(answers.classification), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    },
  });
  const loaded = await loadConsole({ idp });
  await signIn(loaded);
  const { routes } = await import("../src/App");
  const router = createMemoryRouter(routes, { initialEntries: [path] });
  const { container } = render(<RouterProvider router={router} />);
  await waitFor(() => {
    if (!container.querySelector(".grid") || container.querySelector(".grid__busy")) {
      throw new Error("the grid has no answer yet");
    }
  });
  return { container, idp };
}

/** Every request this console made about a classification, as URLs. */
function requests(idp: FakeIdp): URL[] {
  return idp.urls
    .filter((url) => url.includes("/api/v1/classifications"))
    .map((url) => new URL(url, CONSOLE_ORIGIN));
}

/** Every review this console asked for, with the body it sent. */
function proposals(idp: FakeIdp): { url: string; body: unknown }[] {
  return idp.calls
    .filter((call) => (call.init?.method ?? "GET") === "POST")
    .filter((call) => call.url.includes("/api/v1/classifications"))
    .map((call) => ({ url: call.url, body: JSON.parse(String(call.init?.body ?? "null")) }));
}

/** The column headings on the screen, in the order they are rendered. */
function headings(container: HTMLElement): string[] {
  return [...container.querySelectorAll(".grid__table th")].map((cell) => cell.textContent ?? "");
}

/** Press the editor's submit button and wait for the review to arrive. */
async function submitTheEditor(container: HTMLElement, idp: FakeIdp): Promise<void> {
  const forms = [...container.querySelectorAll(".form button[type=submit]")];
  const submit = forms[forms.length - 1];
  expect(submit, "the editor has no submit control").toBeTruthy();
  await act(async () => {
    fireEvent.click(submit as HTMLElement);
  });
  await waitFor(() => expect(proposals(idp)).toHaveLength(1));
}

describe("what the console asks for", () => {
  test("the console sends no query parameter, because the route declares none", async () => {
    // What breaks if this is deleted: a parameter the API ignores. FastAPI drops an
    // undeclared query parameter without a word and answers 200, so a console that grew a
    // `columns=` or a `limit=` here would get the whole classification back and read it as a
    // narrower answer. Both halves are asserted, because a claim about an empty set is
    // otherwise satisfied by a parser that stopped reading: the path parameters are checked
    // as well, so the operation really was found in the document.
    expect(declaredParameterNames(READ_OPERATION, "get", "path")).toContain("entity");
    expect(declaredParameterNames(READ_OPERATION, "get", "query")).toEqual([]);

    const { idp } = await consoleAt(`/classification/${ENTITY}`, {
      classification: classification(priceList()),
    });

    expect(requests(idp)).toHaveLength(1);
    expect(requests(idp)[0]?.search).toBe("");
    expect(`${requests(idp)[0]?.pathname ?? ""}`).toBe(`/api/v1${classificationApiPath(ENTITY)}`);
  });

  test("the review is asked for at the address the route declares, by both segments", async () => {
    // What breaks if this is deleted: a proposal reaching the wrong column, which on this
    // screen is a verdict about the wrong rule. The document is read for both path
    // parameters rather than one, because a path built with the entity and without the
    // column would still be a plausible-looking URL.
    const declared = declaredParameterNames(REVIEW_OPERATION, "post", "path");

    expect(declared.sort()).toEqual(["column", "entity"]);
    expect(reviewApiPath(ENTITY, "cost")).toBe(
      "/classifications/price_list/columns/cost/review",
    );
  });

  test("an address this screen builds always stays inside the console", async () => {
    // What breaks if this is deleted: an open redirect, and there is a live advisory for
    // exactly this. GHSA-wrjc-x8rr-h8h6 is an open redirect via a backslash reaching `<Link>`
    // and `useNavigate`, it covers every react-router from 6.0.0 to 7.17.0, and the only fix
    // is a major version. Both an entity a person typed and a column name from a payload
    // reach `<Link>` and `navigate` on this screen, so both halves of the address are driven.
    const hostile = ["\\\\evil.example", "//evil.example", "/\\evil.example", "http://evil.example", ".."];
    for (const nasty of hostile) {
      for (const address of [
        classificationAddress(nasty),
        columnAddress(nasty, "cost"),
        columnAddress(ENTITY, nasty),
      ]) {
        expect(address.startsWith("/classification/"), address).toBe(true);
        expect(new URL(address, CONSOLE_ORIGIN).origin, address).toBe(CONSOLE_ORIGIN);
      }
      expect(
        reviewApiPath(nasty, nasty).startsWith("/classifications/"),
        reviewApiPath(nasty, nasty),
      ).toBe(true);
    }
  });

  test("no column offers a filter, because the route declares no filter to offer", async () => {
    // What breaks if this is deleted: a filter box that does nothing and looks like one that
    // works. FastAPI discards a query parameter no signature names and answers 200, so a box
    // here would send a term, the whole classification would come back, and a person would
    // read it as the matching columns. `paging.ts` calls that the worst failure available on
    // a grid, and on this screen the rows are the rules that decide disclosure.
    const { container } = await consoleAt(`/classification/${ENTITY}`, {
      classification: classification(priceList(), { editable: true }),
    });

    expect(declaredParameterNames(READ_OPERATION, "get", "query")).toEqual([]);
    expect(container.querySelectorAll("input.grid__filter")).toHaveLength(0);
    expect(container.querySelector(".grid__filters")).toBeNull();
  });
});

describe("the guard in the browser", () => {
  test("hiding the editor changes nothing about what the API is asked", async () => {
    // What breaks if this is deleted: the flag becomes enforcement. The natural wrong
    // implementations are both invisible on screen and both fail here: skipping the request
    // when the flag is false, and asking a different question to get an editable answer.
    // Every URL is compared, in order, because a guard that added one request at the end
    // would pass a check on the first one alone. Then each is compared with the one address
    // this screen has, because two consoles agreeing with each other prove only that they are
    // wrong in the same way: a request varying by anything at all, the open column included,
    // is a client-side condition reaching the server.
    const hidden = await consoleAt(`/classification/${ENTITY}`, {
      classification: classification(priceList(), { editable: false }),
    });
    const shown = await consoleAt(`/classification/${ENTITY}`, {
      classification: classification(priceList(), { editable: true }),
    });
    const opened = await consoleAt(`/classification/${ENTITY}/cost`, {
      classification: classification(priceList(), { editable: true }),
    });

    expect(requests(hidden.idp).map(String)).toEqual(requests(shown.idp).map(String));
    expect(requests(hidden.idp)).toHaveLength(1);
    for (const asked of [
      ...requests(hidden.idp),
      ...requests(shown.idp),
      ...requests(opened.idp),
    ]) {
      expect(`${asked.pathname}${asked.search}`).toBe(`/api/v1${classificationApiPath(ENTITY)}`);
    }
  });

  test("an editor appears only when the API said this caller may propose something", async () => {
    // What breaks if this is deleted: one of the two halves, and each fails differently. A
    // control that is always drawn puts a form in front of somebody every one of whose
    // reviews is refused; one that is never drawn makes the screen read as broken for the
    // person who may use it. Both halves are asserted for that reason.
    const hidden = await consoleAt(`/classification/${ENTITY}/cost`, {
      classification: classification(priceList(), { editable: false }),
    });
    const shown = await consoleAt(`/classification/${ENTITY}/cost`, {
      classification: classification(priceList(), { editable: true }),
    });

    expect(headings(hidden.container)).not.toContain(EDIT_COLUMN);
    expect(headings(shown.container)).toContain(EDIT_COLUMN);
    // The screen carries the entity form as well, so an editor is a second form rather than
    // the only one. Counting them is what distinguishes the two states.
    expect(hidden.container.querySelectorAll(".form")).toHaveLength(1);
    expect(shown.container.querySelectorAll(".form")).toHaveLength(2);
  });

  test("a caller who may not propose is told nothing about why the editor is absent", async () => {
    // What breaks if this is deleted: the console explains a refusal the API never made. A
    // sentence saying "you cannot change this" is a permission model rendered in words, and
    // it distinguishes a caller who may not propose from one whose column is simply not
    // classified, which the API answers identically. Compared against the same screen with no
    // column named, so what is asserted is that opening a column adds nothing at all.
    const opened = await consoleAt(`/classification/${ENTITY}/cost`, {
      classification: classification(priceList(), { editable: false }),
    });
    const closed = await consoleAt(`/classification/${ENTITY}`, {
      classification: classification(priceList(), { editable: false }),
    });

    expect(opened.container.innerHTML).toBe(closed.container.innerHTML);
  });
});

describe("what the screen shows", () => {
  test("every field the API sends about a classified column reaches the screen", async () => {
    // What breaks if this is deleted: a field arrives and is dropped in silence. No route
    // sends a schema for this screen, so the columns are a list somebody wrote, and the only
    // thing keeping it in step with the response model is this. The field it protects is
    // `derived_from`: a derivation reads as bookkeeping and is what decides whether a caller
    // short of one column can work out another. Asserted in both directions, because a
    // column naming a field the model does not have would render an empty column headed with
    // a word from nowhere.
    const declared = backendColumnViewFields();
    const rendered = CLASSIFICATION_COLUMNS.map((entry) => String(entry.name));

    expect([...rendered].sort()).toEqual([...declared].sort());
  });

  test("the grid adds one column that is not a field, and only for an editor", async () => {
    // What breaks if this is deleted: the test above stops being able to mean anything,
    // because any extra column could be waved through as the edit control. The edit column is
    // named once and is the only id in the grid that is not a field of the response model.
    const fields = CLASSIFICATION_COLUMNS.map((entry) => String(entry.name));
    const withoutEditor = classificationColumns(false, () => null).map((c) => String(c.id));
    const withEditor = classificationColumns(true, () => null).map((c) => String(c.id));

    expect(withoutEditor).toEqual(fields);
    expect(withEditor).toEqual([...fields, EDIT_COLUMN]);
  });

  test("the page says the same thing whatever number of columns arrived", async () => {
    // What breaks if this is deleted: a count. "3 confidential columns" beside a
    // classification is one line, and the rule this console keeps is one rule about counts
    // rather than one per endpoint, because the person who writes the count here is the
    // person who copies it onto a screen where the collection is filtered. Everything outside
    // the table body is compared byte for byte between a classification of five columns and
    // one of two, so a number anywhere on the screen fails this.
    const chrome = (markup: string) => markup.replace(/<tbody>[\s\S]*?<\/tbody>/, "<tbody/>");
    const five = await consoleAt(`/classification/${ENTITY}`, {
      classification: classification(priceList()),
    });
    const two = await consoleAt(`/classification/${ENTITY}`, {
      classification: classification([column(), column({ column: "sku", derived_from: [] })]),
    });

    expect(chrome(two.container.innerHTML)).toBe(chrome(five.container.innerHTML));
  });

  test("a column's derivation reaches its cell", async () => {
    // What breaks if this is deleted: the derivation column renders nothing for every row,
    // which looks exactly like a classification in which nothing is reconstructable from
    // anything. That is the one reading of this screen that would make every widening
    // invisible, because the whole point of the derivation is that withholding `cost` is
    // pointless while `sell_price` and `margin` are both shown.
    const { container } = await consoleAt(`/classification/${ENTITY}`, {
      classification: classification([column()]),
    });

    const body = container.querySelector(".grid__table tbody")?.textContent ?? "";
    expect(body).toContain("margin");
    expect(body).toContain("sell_price");
  });

  test("naming no document shows the form and asks the API for nothing", async () => {
    // What breaks if this is deleted: a request for the classification of the empty string
    // every time somebody opens the screen from the menu, and a sentence naming an example
    // document. There is no route that lists what is classified, so an example here would be
    // this console publishing a guess at what the company keeps to everybody who can open
    // the page.
    const idp = fakeIdentityProvider({ api: () => null });
    const loaded = await loadConsole({ idp });
    await signIn(loaded);
    const { routes } = await import("../src/App");
    const router = createMemoryRouter(routes, { initialEntries: ["/classification"] });
    const { container } = render(<RouterProvider router={router} />);
    await waitFor(() => {
      if (!container.querySelector(".form")) {
        throw new Error("the form has not arrived");
      }
    });

    expect(container.textContent).toContain(NOTHING_ASKED_FOR);
    expect(container.querySelector(".grid")).toBeNull();
    expect(requests(idp)).toHaveLength(0);
    expect(NOTHING_ASKED_FOR).not.toContain(ENTITY);
  });

  test("an address naming a column the classification does not carry says so and asks for nothing else", async () => {
    // What breaks if this is deleted: a bad link renders a screen with no editor and no
    // reason, or worse, a second request for one column. There is no per-column read route,
    // and inventing one would be the console asking a question the API has not been given.
    const { container, idp } = await consoleAt(`/classification/${ENTITY}/nothing_like_that`, {
      classification: classification(priceList(), { editable: true }),
    });

    expect(container.textContent).toContain(NO_SUCH_COLUMN);
    expect(container.querySelectorAll(".form")).toHaveLength(1);
    expect(requests(idp)).toHaveLength(1);
  });

  test("a refusal is the API's sentence and the page adds no reading of it", async () => {
    // What breaks if this is deleted: the console explains a 404 it must not explain. The
    // route answers a caller who may not read a classification, a caller who may not review
    // one and anybody asking about an entity nothing classifies with the same body, so any
    // wording distinguishing them, including a sympathetic one about permissions, rebuilds
    // the difference the taxonomy spent itself removing.
    const idp = fakeIdentityProvider({
      api(url) {
        if (!url.includes("/api/v1/classifications")) {
          return null;
        }
        return new Response(JSON.stringify({ message: "I could not find that.", trace_id: "" }), {
          status: 404,
          headers: { "content-type": "application/json", "x-trace-id": "trace-classification" },
        });
      },
    });
    const loaded = await loadConsole({ idp });
    await signIn(loaded);
    const { routes } = await import("../src/App");
    const router = createMemoryRouter(routes, {
      initialEntries: [`/classification/${ENTITY}`],
    });
    const { container } = render(<RouterProvider router={router} />);
    await waitFor(() => {
      if (!container.querySelector(".notice__body")) {
        throw new Error("the failure has not arrived");
      }
    });

    expect(container.querySelector(".notice__body")?.textContent).toBe("I could not find that.");
    expect(container.querySelector(".notice__trace")?.textContent).toContain(
      "trace-classification",
    );
    expect(container.querySelector(".grid__empty")).toBeNull();
  });
});

describe("the form a rule is proposed through", () => {
  test("the form's fields are the route's fields and its bounds are the route's bounds", async () => {
    // What breaks if this is deleted: the console's copy of the capability grammar drifts
    // from the route's, and the form starts accepting a value that comes back 422 carrying
    // `HTTPValidationError`, which is not `ErrorBody` and reads as "Something went wrong."
    // The declared schema is read out of the API's own document, which carries the pattern
    // `brain.core.entitlement.CAPABILITY_RE` itself, so this is not two console constants
    // compared with each other.
    const declared = declaredRequestBodySchema(REVIEW_OPERATION, "post");
    const declaredProperties = declared["properties"] as Record<string, Record<string, unknown>>;
    const mine = (columnEditSchema(["sell_price"]).properties ?? {}) as Record<
      string,
      Record<string, unknown>
    >;

    expect(Object.keys(mine).sort()).toEqual(Object.keys(declaredProperties).sort());
    expect(Object.keys(mine).sort()).toEqual([...backendColumnEditFields()].sort());
    expect(mine["required_capability"]?.["pattern"]).toBe(
      declaredProperties["required_capability"]?.["pattern"],
    );
    expect(CAPABILITY_PATTERN).toBe(declaredProperties["required_capability"]?.["pattern"]);

    let compared = 0;
    for (const [name, schema] of Object.entries(declaredProperties)) {
      for (const bound of ["minLength", "maxLength", "maxItems", "pattern"]) {
        if (!(bound in schema)) {
          continue;
        }
        compared += 1;
        expect(mine[name]?.[bound], `${name}.${bound}`).toBe(schema[bound]);
      }
    }
    // A loop over an empty document would pass every assertion inside it.
    expect(compared).toBeGreaterThan(3);
    expect(MAX_DERIVED_FROM).toBe(declaredProperties["derived_from"]?.["maxItems"]);
  });

  test("the sensitivity levels the form offers are the ones the policy defines", async () => {
    // What breaks if this is deleted: a select offering a word the policy layer does not
    // have, which comes back as a 422 about an enum, or worse, a select missing one, which
    // silently makes a level unreachable from the console. Checked against both originals,
    // because they go stale differently: the document is a generation nobody ran, and the
    // enum is a vocabulary this console would keep offering the old shape of.
    const declared = declaredPropertySchema(REVIEW_OPERATION, "post", "classification");

    expect(declared["enum"]).toEqual([...CLASSIFICATION_WORDS]);
    expect([...CLASSIFICATION_WORDS].sort()).toEqual([...backendClassificationWords()].sort());
  });

  test("the route refuses a key the form does not carry, so the two refusals agree", async () => {
    // What breaks if this is deleted: the console's discipline becomes the only thing
    // stopping a proposal naming a column the address does not. The route's model forbids
    // extra keys and the document says so; if it ever stopped saying so, a console bug would
    // become a policy bug, and this is where that change is noticed.
    const declared = declaredRequestBodySchema(REVIEW_OPERATION, "post");

    expect(declared["additionalProperties"]).toBe(false);
    expect(declared["required"]).toEqual(backendColumnEditFields());
    expect(backendColumnEditFields()).not.toContain("entity");
    expect(backendColumnEditFields()).not.toContain("column");
  });

  test("a form for one column offers no control for anything but the route's three fields", async () => {
    // What breaks if this is deleted: the schema stays honest and the rendered form does not.
    // `formShape` and the library between them decide what becomes a control, so the property
    // has to be asserted on the mounted markup as well: a `ui:field` or a default that put a
    // fourth input on the screen would pass every check made against the schema alone.
    //
    // Matched by prefix rather than by equality, because an array field renders one control
    // per option and each carries an index on its id. What is asserted is that every control
    // belongs to one of the route's fields and that each field has at least one.
    const { container } = await consoleAt(`/classification/${ENTITY}/cost`, {
      classification: classification(priceList(), { editable: true }),
    });

    const fields = backendColumnEditFields();
    const editor = [...container.querySelectorAll(".form")].at(-1);
    const ids = [...(editor?.querySelectorAll("input, select, textarea") ?? [])]
      .map((control) => control.getAttribute("id") ?? "")
      .filter((id) => id.startsWith("root_"))
      .map((id) => id.slice("root_".length));

    expect(ids.length).toBeGreaterThan(0);
    for (const id of ids) {
      expect(fields.some((field) => id === field || id.startsWith(field)), id).toBe(true);
    }
    for (const field of fields) {
      expect(ids.some((id) => id === field || id.startsWith(field)), field).toBe(true);
    }
    expect(editor?.textContent).not.toContain("entity");
  });

  test("a column can never be offered as an input to itself", async () => {
    // What breaks if this is deleted: the form offers the one option the API can only refuse.
    // `ColumnRule` rejects a column declared as derived from itself, because such a rule
    // would make the closure withhold the column in order to protect it, and a person who
    // ticked the box would get a sentence about a rule that would not load rather than a
    // verdict about the change they meant to make.
    const options = derivationOptions(priceList(), "cost");

    expect(options).not.toContain("cost");
    expect(options).toEqual(["margin", "name", "sell_price", "sku"]);
  });

  test("a value the form never showed cannot be sent", async () => {
    // What breaks if this is deleted: the body becomes whatever the form library handed
    // back. An `entity` or a `column` in the form's state, from a schema change or a merge,
    // would then travel, and the route would refuse it with a 422 naming a field the person
    // never filled in. The three keys are built by name here, which is why an extra one has
    // nowhere to go.
    const built = submittedEdit({
      required_capability: "read:price_list.cost",
      classification: "confidential",
      derived_from: ["margin"],
      column: "margin",
      entity: "finance_ledger",
    });

    expect(built).not.toBeNull();
    expect(Object.keys(built ?? {}).sort()).toEqual([...backendColumnEditFields()].sort());
  });

  test("a submission that is not a rule sends nothing", async () => {
    // What breaks if this is deleted: a proposal assembled out of values nobody read, whose
    // answer would be a verdict about who may see what. A shape the screen does not recognise
    // has to produce no request rather than a defaulted one.
    expect(submittedEdit(null)).toBeNull();
    expect(submittedEdit({ required_capability: "read:a.b" })).toBeNull();
    expect(
      submittedEdit({ required_capability: 1, classification: "internal", derived_from: [] }),
    ).toBeNull();
    expect(
      submittedEdit({
        required_capability: "read:a.b",
        classification: "internal",
        derived_from: "margin",
      }),
    ).toBeNull();
    expect(
      submittedEdit({
        required_capability: "read:a.b",
        classification: "internal",
        derived_from: [1],
      }),
    ).toBeNull();
    expect(submittedEntity({ entity: "" })).toBeNull();
    expect(submittedEntity({ nothing: "here" })).toBeNull();
  });

  test("the form starts from the rule the API answered", async () => {
    // What breaks if this is deleted: a form that opens on defaults. Reviewing from it would
    // then ask about a rule nobody proposed, and the verdict would be about a change nobody
    // meant to make, on the one screen where the verdict is about disclosure.
    const values = editableDefaults(column());

    expect(values).toEqual({
      required_capability: "read:price_list.cost",
      classification: "confidential",
      derived_from: ["margin", "sell_price"],
    });
  });
});

describe("what a review says", () => {
  test("a review is a POST to the column in the address, carrying the three fields", async () => {
    // What breaks if this is deleted: the request. Both halves are checked because either
    // alone passes for the wrong reason: a request to the right address proves nothing about
    // what was in it, and a body with three keys proves nothing about which column it reached.
    // The key set is compared with the route's own model rather than with a list here.
    const { container, idp } = await consoleAt(`/classification/${ENTITY}/cost`, {
      classification: classification(priceList(), { editable: true }),
    });

    await submitTheEditor(container, idp);

    const sent = proposals(idp)[0];
    expect(sent?.url).toContain(`/api/v1${reviewApiPath(ENTITY, "cost")}`);
    expect(Object.keys(sent?.body as Record<string, unknown>).sort()).toEqual(
      [...backendColumnEditFields()].sort(),
    );
  });

  test("a widening is named and the columns it would expose are listed", async () => {
    // What breaks if this is deleted: the one outcome this screen exists to make impossible
    // to miss. The API says `widens` and names the columns; a console that rendered the
    // change words and dropped the exposed list would show `derivation_dropped` about `cost`
    // and leave the reader to work out that the consequence lands on `margin`, which is the
    // whole difficulty of a derivation.
    const { container, idp } = await consoleAt(`/classification/${ENTITY}/cost`, {
      classification: classification(priceList(), { editable: true }),
      review: {
        body: reviewed({
          widens: true,
          changes: ["derivation_dropped"],
          exposed: ["margin"],
        }),
      },
    });

    await submitTheEditor(container, idp);
    await waitFor(() => {
      if (!container.textContent?.includes(IT_WIDENS)) {
        throw new Error("the verdict has not arrived");
      }
    });

    expect(container.textContent).toContain(WHAT_A_WIDENING_MEANS);
    expect([...container.querySelectorAll(".review__exposed li")].map((n) => n.textContent)).toEqual(
      ["margin"],
    );
    expect(container.textContent).toContain("derivation_dropped");
    expect(container.textContent).not.toContain(IT_DOES_NOT_WIDEN);
  });

  test("a change the API did not call a widening is not dressed up as one", async () => {
    // What breaks if this is deleted: every review reads as an alarm, which trains the person
    // using this screen to ignore the one that matters. This is the sibling of the test
    // above, and without it a component that always rendered the loud branch would pass.
    const { container, idp } = await consoleAt(`/classification/${ENTITY}/cost`, {
      classification: classification(priceList(), { editable: true }),
      review: { body: reviewed({ widens: false, changes: ["more_sensitive"] }) },
    });

    await submitTheEditor(container, idp);
    await waitFor(() => {
      if (!container.textContent?.includes(IT_DOES_NOT_WIDEN)) {
        throw new Error("the verdict has not arrived");
      }
    });

    expect(container.textContent).toContain("more_sensitive");
    expect(container.textContent).not.toContain(IT_WIDENS);
    expect(container.querySelector(".review__exposed")).toBeNull();
  });

  test("a proposal the API said would not load is shown in the API's own words", async () => {
    // What breaks if this is deleted: the worst outcome on this surface becomes silent. A
    // classification that raises on construction leaves the previous rules in place while a
    // person believes they changed something, and the API answers 200 with a sentence saying
    // so. A console that only branched on `widens` would render "this would not widen
    // anything", which is true and is the opposite of useful.
    const refusal = "price_list.cost is declared as derived from ['nope'], which is not classified here";
    const { container, idp } = await consoleAt(`/classification/${ENTITY}/cost`, {
      classification: classification(priceList(), { editable: true }),
      review: { body: reviewed({ would_not_load: refusal }) },
    });

    await submitTheEditor(container, idp);
    await waitFor(() => {
      if (!container.textContent?.includes(IT_WOULD_NOT_LOAD)) {
        throw new Error("the verdict has not arrived");
      }
    });

    expect(container.textContent).toContain(refusal);
    expect(container.textContent).not.toContain(IT_DOES_NOT_WIDEN);
  });

  test("a review of the rule that already stands says so", async () => {
    // What breaks if this is deleted: an empty verdict, which reads as a screen that failed
    // rather than as an answer. It is also the zero of the whole comparison: a screen that
    // rendered something for every review would pass every other test in this block.
    const { container, idp } = await consoleAt(`/classification/${ENTITY}/cost`, {
      classification: classification(priceList(), { editable: true }),
      review: { body: reviewed() },
    });

    await submitTheEditor(container, idp);
    await waitFor(() => {
      if (!container.textContent?.includes(NOTHING_WOULD_CHANGE)) {
        throw new Error("the verdict has not arrived");
      }
    });

    expect(container.textContent).toContain(IT_DOES_NOT_WIDEN);
  });

  test("a refused review shows the API's sentence and the editor stays on the screen", async () => {
    // What breaks if this is deleted: a refused proposal is silent, or it takes the form
    // away. The refusal here is the same 404 a caller who may not read the classification
    // gets, so the console has nothing to add to it, and the form has to stay so the person
    // can see what they asked about.
    const { container, idp } = await consoleAt(`/classification/${ENTITY}/cost`, {
      classification: classification(priceList(), { editable: true }),
      review: {
        status: 404,
        body: { message: "I could not find that.", trace_id: "" },
        traceId: "t-1",
      },
    });

    await submitTheEditor(container, idp);
    await waitFor(() => {
      if (!container.querySelector(".form .notice__body")) {
        throw new Error("the failure has not arrived");
      }
    });

    expect(container.querySelector(".form .notice__body")?.textContent).toBe(
      "I could not find that.",
    );
    expect(container.querySelectorAll(".form")).toHaveLength(2);
    expect(container.textContent).not.toContain(IT_WIDENS);
    expect(container.textContent).not.toContain(IT_DOES_NOT_WIDEN);
  });

  test("the screen says a review is not a save, and never says anything was", async () => {
    // What breaks if this is deleted: the console starts describing a mechanism that does not
    // exist. There is no route that stores a classification, so a person who read a verdict
    // as a receipt would stop checking that the change had actually been made, which is the
    // one failure on this screen that nothing downstream would catch.
    const { container, idp } = await consoleAt(`/classification/${ENTITY}/cost`, {
      classification: classification(priceList(), { editable: true }),
      review: { body: reviewed({ widens: true, changes: ["added"], exposed: ["discount"] }) },
    });

    await submitTheEditor(container, idp);
    await waitFor(() => {
      if (!container.textContent?.includes(IT_WIDENS)) {
        throw new Error("the verdict has not arrived");
      }
    });

    expect(container.textContent).toContain(THERE_IS_NO_SAVE);
    expect(container.textContent ?? "").not.toMatch(/\bsaved\b/i);
    expect(container.textContent ?? "").not.toMatch(/\bstored\b/i);
  });

  test("no sentence this screen adds carries a number", async () => {
    // What breaks if this is deleted: a count arrives in prose rather than in a heading,
    // which is where the rest of this console is watching for it. "2 columns would be
    // exposed" is the natural way to write the widening sentence and it is the one number
    // this screen must not have: the exposed columns are named, and naming is what the reader
    // has to act on.
    for (const sentence of [
      NOTHING_ASKED_FOR,
      NOTHING_WOULD_CHANGE,
      NO_SUCH_COLUMN,
      THERE_IS_NO_SAVE,
      WHAT_A_WIDENING_MEANS,
      IT_WIDENS,
      IT_DOES_NOT_WIDEN,
      IT_WOULD_NOT_LOAD,
    ]) {
      expect(sentence, sentence).not.toMatch(/\d/);
    }
  });
});

describe("reading a classification and a review", () => {
  test("every epoch the API sends stops at the reader", async () => {
    // What breaks if this is deleted: a digest reaches a screen, and two of them mislead.
    // `FieldPolicy.epoch` does not digest a rule's `derived_from`, which the route's own
    // docstring records, so `epoch_now` and `epoch_after` are identical for the one edit this
    // screen exists to catch, and showing them beside a widening would hand a person an
    // authoritative-looking reason to believe nothing had changed. The model is read for the
    // field names, so this is not the console's reader compared with itself.
    const fields = backendReviewViewFields();
    const read = readReview(reviewed({ widens: true }));
    const page = readClassification(classification(priceList(), { epoch: "EPOCH-SENTINEL-NOW" }));

    expect(fields).toContain("epoch_now");
    expect(fields).toContain("epoch_after");
    expect(Object.keys(read).sort()).toEqual(["changes", "exposed", "widens", "wouldNotLoad"]);
    expect(Object.keys(page).sort()).toEqual(["columns", "editable", "entity"]);
    expect(JSON.stringify([read, page])).not.toContain("EPOCH-SENTINEL");
  });

  test("a body that is not a classification is an empty one rather than a crash", async () => {
    // What breaks if this is deleted: a console built against a different API renders a blank
    // screen with a stack trace behind it instead of an empty grid. The shape is fixed by a
    // response model in this repository, so there is no sentence worth composing about a body
    // that does not match it.
    expect(readClassification(null).columns).toEqual([]);
    expect(readClassification({ entity: ENTITY, columns: "no" }).columns).toEqual([]);
    expect(readClassification({ columns: [] }).columns).toEqual([]);
    expect(readClassification({ entity: ENTITY, columns: [], editable: "yes" }).editable).toBe(
      false,
    );

    // Written because a mutation survived: the empty page the reader falls back to had
    // `editable` on it, and flipping that to true passed every test here. A page nobody
    // could read is not a page somebody may edit, and an edit control drawn over a body
    // that failed to parse invites a write against a screen showing nothing. The API would
    // still refuse it, which is what makes this a misleading control rather than a
    // disclosure, and a control that cannot work is the thing this console refuses to draw.
    for (const unreadable of [null, "not an object", 7, { columns: "no" }]) {
      expect(readClassification(unreadable).editable).toBe(false);
    }
  });

  test("a review that is not a review is never read as a permitted one", async () => {
    // What breaks if this is deleted: an unreadable body defaults to something, and the safe
    // default is not obvious. `widens: false` is what an absent field reads as, which is the
    // permissive direction, so the caller has to be the one that never renders a verdict it
    // did not receive. Both halves are asserted: the reader is strict about the flag, and a
    // word that is not a string is dropped rather than rendered.
    expect(readReview(null).widens).toBe(false);
    expect(readReview({ widens: "yes" }).widens).toBe(false);
    expect(readReview({ widens: true }).widens).toBe(true);
    expect(readReview({ changes: [1, "added", null] }).changes).toEqual(["added"]);
    expect(readReview({ exposed: "margin" }).exposed).toEqual([]);
    expect(readReview({ would_not_load: 7 }).wouldNotLoad).toBe("");
  });
});
