/**
 * The routing matrix screen: the first place in this console that writes anything.
 *
 * Four properties matter more than the rest, and each is a way this screen could be wrong
 * while looking right.
 *
 * **The role must have a column and no input.** `brain.models.routing.RungRole` says a label
 * a person types drifts from the position and provider it describes, and then the console
 * shows a primary sitting third in the chain. M5.3.2 derives the column in a trigger. The
 * console's half of that is asserted from three directions here, because a form is exactly
 * where a derived field grows an input: somebody sees a wrong-looking role and the obvious
 * repair is a box.
 *
 * **A guard in the browser must change nothing about what the server is asked.** `editable`
 * arrives on the response and decides whether an edit control is drawn. A console that also
 * skipped the request, or asked a different one, would be enforcing a rule in the copy an
 * attacker edits, and the request is compared byte for byte between the two answers.
 *
 * **Nothing around the table may count anything.** The grid holds that rule for the table;
 * this holds it for the page, which is where a heading saying how many rungs there are would
 * go, and for the sentence about a full page, which is where a number would be helpful.
 *
 * **It must ask and send only what the route declares.** FastAPI discards an undeclared query
 * parameter without a word, and a body key it does not declare is refused rather than
 * ignored. Both are read out of the API's own document rather than agreed here.
 *
 * Task ids: M5.3.3
 */

import { createMemoryRouter, RouterProvider } from "react-router-dom";
import { act, fireEvent, render, waitFor } from "@testing-library/react";
import { beforeAll, describe, expect, test } from "vitest";
import {
  clauseText,
  EDIT_COLUMN,
  editableDefaults,
  MATRIX_COLUMNS,
  MATRIX_PAGE_SIZE,
  matrixApiPath,
  matrixColumns,
  readMatrixPage,
  RUNG_EDIT_SCHEMA,
  rungAddress,
  rungApiPath,
  rungById,
  scopeLines,
  submittedEdit,
  type RungRow,
} from "../src/pages/matrixQuery";
import { NO_SUCH_RUNG, THERE_IS_MORE } from "../src/pages/Matrix";
import { fakeIdentityProvider, loadConsole, signIn, type FakeIdp } from "./support/auth";
import { declaredParameterSchema, declaredQueryParameters, declaredRequestBodySchema } from "./support/openapi";
import { backendRoutingRungColumns, backendRungEditFields, backendRungViewFields } from "./support/python";

const RUNGS_OPERATION = "/api/v1/routing/rungs";
const EDIT_OPERATION = "/api/v1/routing/rungs/{rung_id}";
const CONSOLE_ORIGIN = "https://console.test";

/**
 * Transform the split route once, before anything is timed.
 *
 * This route is code-split, so the first mount of it in this file is the first time Vite
 * transforms `@rjsf/core`, ajv and the table library, which takes several seconds and has
 * nothing to do with the property under test. Without this the first test in the file fails
 * on a timeout and every later one passes, which reads as a flake rather than as a cold
 * cache. The records suite warms the same libraries for the same reason.
 */
beforeAll(async () => {
  await import("../src/pages/Matrix");
}, 60_000);

/** One rung in the shape `brain.routing_routes.RungView` serialises. */
function rung(overrides: Partial<RungRow> = {}): RungRow {
  return {
    id: "11111111-1111-4111-8111-111111111111",
    tier: "main",
    position: 0,
    role: "primary",
    scope: {},
    deployment_id: "anthropic-sonnet-global",
    provider: "anthropic",
    model: "claude-sonnet-5",
    attempts: 1,
    timeout_seconds: 12,
    max_concurrency: 40,
    enabled: true,
    ...overrides,
  };
}

/** A page of the matrix in the shape `RungPage` serialises, `total` included and unset. */
function page(
  items: RungRow[],
  extra: { editable?: boolean; truncated?: boolean } = {},
): unknown {
  return {
    items,
    next_cursor: null,
    total: null,
    truncated: extra.truncated ?? false,
    editable: extra.editable ?? false,
  };
}

interface Answers {
  /** The body for the GET, or null to answer nothing at all. */
  readonly matrix: unknown;
  /** The status and body for the PATCH. */
  readonly saved?: { status?: number; body?: unknown; traceId?: string };
}

/**
 * Mount the application's own route table at one address, against a stand-in API.
 *
 * The real table rather than a copy of it, for the reason `routing.test.tsx` gives: a test
 * that declared its own route for this page would be testing the copy, and the route is half
 * of what is being checked here because the rung is a path segment.
 */
async function consoleAt(
  path: string,
  answers: Answers,
): Promise<{ container: HTMLElement; idp: FakeIdp }> {
  const idp = fakeIdentityProvider({
    api(url, init) {
      if (!url.includes("/api/v1/routing/rungs")) {
        return null;
      }
      if ((init?.method ?? "GET") === "PATCH") {
        const saved = answers.saved ?? { status: 200, body: rung() };
        return new Response(JSON.stringify(saved.body ?? rung()), {
          status: saved.status ?? 200,
          headers: {
            "content-type": "application/json",
            ...(saved.traceId ? { "x-trace-id": saved.traceId } : {}),
          },
        });
      }
      if (answers.matrix === null) {
        return null;
      }
      return new Response(JSON.stringify(answers.matrix), {
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

/** Every request this console made to the matrix, as URLs. */
function matrixRequests(idp: FakeIdp): URL[] {
  return idp.urls
    .filter((url) => url.includes("/api/v1/routing/rungs"))
    .map((url) => new URL(url, CONSOLE_ORIGIN));
}

/** Every write this console made, with the body it sent. */
function writes(idp: FakeIdp): { url: string; body: unknown }[] {
  return idp.calls
    .filter((call) => (call.init?.method ?? "GET") === "PATCH")
    .map((call) => ({ url: call.url, body: JSON.parse(String(call.init?.body ?? "null")) }));
}

/** The column headings on the screen, in the order they are rendered. */
function headings(container: HTMLElement): string[] {
  return [...container.querySelectorAll(".grid__table th")].map((cell) => cell.textContent ?? "");
}

describe("what the console asks for", () => {
  test("the console sends only query parameters the route declares", async () => {
    // What breaks if this is deleted: a parameter the API ignores. FastAPI drops an
    // undeclared query parameter without a word, so a console that sent `rows` instead of
    // `limit` would get the route's default page and read it as the page it asked for. The
    // declared names are read out of the API's own document, so this is not the console's
    // spelling compared with itself.
    const declared = new Set(declaredQueryParameters(RUNGS_OPERATION, "get"));
    const { idp } = await consoleAt("/routing", { matrix: page([rung()]) });

    const sent = [...(matrixRequests(idp)[0]?.searchParams.keys() ?? [])];
    expect(sent.length).toBeGreaterThan(0);
    expect(sent.filter((name) => !declared.has(name))).toEqual([]);
  });

  test("the page size the console asks for is one the route will answer", async () => {
    // What breaks if this is deleted: every load of this screen becomes a 422. The route
    // bounds `limit`, the console names a number, and a number outside the bound comes back
    // as `HTTPValidationError`, which is not `ErrorBody` and therefore reaches a person as
    // "Something went wrong." The bound is read off the route's own parameter rather than
    // compared with another constant here.
    const declared = declaredParameterSchema(RUNGS_OPERATION, "get", "limit");

    expect(typeof declared["maximum"]).toBe("number");
    expect(MATRIX_PAGE_SIZE).toBeLessThanOrEqual(declared["maximum"] as number);
    expect(MATRIX_PAGE_SIZE).toBeGreaterThanOrEqual(declared["minimum"] as number);
    expect(matrixApiPath()).toBe(`/routing/rungs?limit=${String(MATRIX_PAGE_SIZE)}`);
  });

  test("an address this screen builds always stays inside the console", async () => {
    // What breaks if this is deleted: an open redirect, and there is a live advisory for
    // exactly this. GHSA-wrjc-x8rr-h8h6 is an open redirect via a backslash reaching `<Link>`
    // and `useNavigate`, it covers every react-router from 6.0.0 to 7.17.0, and the only fix
    // is a major version. A rung id reaches `<Link>` on this screen, so the defence has to be
    // here, and what holds it up is the constant `/routing/` prefix rather than the encoding.
    const hostile = ["\\\\evil.example", "//evil.example", "/\\evil.example", "http://evil.example", ".."];
    for (const id of hostile) {
      const address = rungAddress(id);
      expect(address.startsWith("/routing/"), address).toBe(true);
      expect(new URL(address, CONSOLE_ORIGIN).origin, address).toBe(CONSOLE_ORIGIN);
      expect(rungApiPath(id).startsWith("/routing/rungs/"), rungApiPath(id)).toBe(true);
    }
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
    // wrong in the same way: a request varying by anything at all, the open rung included, is
    // a client-side condition reaching the server.
    const hidden = await consoleAt("/routing", { matrix: page([rung()], { editable: false }) });
    const shown = await consoleAt("/routing", { matrix: page([rung()], { editable: true }) });
    const opened = await consoleAt("/routing/11111111-1111-4111-8111-111111111111", {
      matrix: page([rung()], { editable: true }),
    });

    expect(matrixRequests(hidden.idp).map(String)).toEqual(
      matrixRequests(shown.idp).map(String),
    );
    expect(matrixRequests(hidden.idp)).toHaveLength(1);
    for (const asked of [
      ...matrixRequests(hidden.idp),
      ...matrixRequests(shown.idp),
      ...matrixRequests(opened.idp),
    ]) {
      expect(`${asked.pathname}${asked.search}`).toBe(`/api/v1${matrixApiPath()}`);
    }
  });

  test("no column offers a filter, because the route declares no filter to offer", async () => {
    // What breaks if this is deleted: a filter box that does nothing and looks like one that
    // works. FastAPI discards a query parameter no signature names and answers 200, so a box
    // here would send a term, the whole matrix would come back, and a person would read it as
    // the matching rungs. `paging.ts` calls that the worst failure available on a grid. The
    // declared parameters are read from the document, so this is the route's silence rather
    // than a decision restated here.
    const declared = declaredQueryParameters(RUNGS_OPERATION, "get");
    const { container } = await consoleAt("/routing", {
      matrix: page([rung()], { editable: true }),
    });

    expect(declared).not.toContain("filter");
    expect(container.querySelectorAll("input.grid__filter")).toHaveLength(0);
    expect(container.querySelector(".grid__filters")).toBeNull();
  });

  test("an edit control appears only when the API said this caller may change something", async () => {
    // What breaks if this is deleted: one of the two halves, and each fails differently. A
    // control that is always drawn puts a form in front of somebody every one of whose saves
    // is refused; one that is never drawn makes the screen read as broken for the person who
    // may use it. Both halves are asserted for that reason.
    const hidden = await consoleAt("/routing/11111111-1111-4111-8111-111111111111", {
      matrix: page([rung()], { editable: false }),
    });
    const shown = await consoleAt("/routing/11111111-1111-4111-8111-111111111111", {
      matrix: page([rung()], { editable: true }),
    });

    expect(hidden.container.querySelector(".form")).toBeNull();
    expect(headings(hidden.container)).not.toContain(EDIT_COLUMN);
    expect(shown.container.querySelector(".form")).not.toBeNull();
    expect(headings(shown.container)).toContain(EDIT_COLUMN);
  });

  test("a caller who may not edit is told nothing about why the editor is absent", async () => {
    // What breaks if this is deleted: the console explains a refusal the API never made. A
    // sentence saying "you cannot change this" is a permission model rendered in words, and
    // it distinguishes a caller who may not edit from one whose rung is simply not on the
    // page, which the API answers identically. Compared against the same screen with no rung
    // named, so what is asserted is that opening a rung adds nothing at all.
    const opened = await consoleAt("/routing/11111111-1111-4111-8111-111111111111", {
      matrix: page([rung()], { editable: false }),
    });
    const closed = await consoleAt("/routing", {
      matrix: page([rung()], { editable: false }),
    });

    expect(opened.container.innerHTML).toBe(closed.container.innerHTML);
  });
});

describe("the role a trigger derives", () => {
  test("the role is a column of the matrix and never a field of an edit", async () => {
    // What breaks if this is deleted: an input for a derived label. Both originals are read:
    // the table that declares the column and the request model that declares what may be
    // sent. Comparing the console's own two lists would be green for every value they could
    // hold, and the value they would agree on is the wrong one, because a console that grew a
    // role input would grow a role column beside it.
    expect(backendRoutingRungColumns()).toContain("role");
    expect(backendRungEditFields()).not.toContain("role");

    expect(MATRIX_COLUMNS.map((column) => column.name)).toContain("role");
    expect(Object.keys(RUNG_EDIT_SCHEMA.properties ?? {})).not.toContain("role");
  });

  test("a form for one rung offers no control for anything but the four dials", async () => {
    // What breaks if this is deleted: the schema stays honest and the rendered form does not.
    // `formShape` and the library between them decide what becomes a control, so the property
    // has to be asserted on the mounted markup as well: a `ui:field` or a default that put a
    // fifth input on the screen would pass every check made against the schema alone.
    const { container } = await consoleAt("/routing/11111111-1111-4111-8111-111111111111", {
      matrix: page([rung()], { editable: true }),
    });

    // Read off the controls rather than off the labels. A required field's label carries a
    // marker the library adds and a checkbox puts its title somewhere else entirely, so a
    // label comparison would be a comparison with the library's own rendering conventions;
    // an input's id is `root_<property>` for every widget it has, which is the property.
    const controls = [...container.querySelectorAll(".form input, .form select, .form textarea")]
      .map((control) => control.getAttribute("id") ?? "")
      .filter((id) => id.startsWith("root_"))
      .map((id) => id.slice("root_".length));

    expect(controls.sort()).toEqual([...backendRungEditFields()].sort());
    expect(container.querySelector(".form")?.textContent).not.toContain("role");
  });

  test("a value the form never showed cannot be sent", async () => {
    // What breaks if this is deleted: the body becomes whatever the form library handed
    // back. A role in the form's state, from a schema change or a merge, would then travel,
    // and the route would refuse it with a 422 naming a field the person never filled in.
    // The four keys are built by name here, which is why an extra one has nowhere to go.
    const built = submittedEdit({
      attempts: 2,
      timeout_seconds: 15,
      max_concurrency: 8,
      enabled: true,
      role: "primary",
      tier: "heavy",
    });

    expect(built).not.toBeNull();
    expect(Object.keys(built ?? {}).sort()).toEqual([...backendRungEditFields()].sort());
  });

  test("a submission that is not an edit sends nothing", async () => {
    // What breaks if this is deleted: a PATCH assembled out of values nobody read. This is
    // the one screen in this console where a wrong request changes something, so a shape the
    // screen does not recognise has to produce no write rather than a defaulted one.
    expect(submittedEdit(null)).toBeNull();
    expect(submittedEdit({ attempts: 1 })).toBeNull();
    expect(submittedEdit({ attempts: "2", timeout_seconds: 1, max_concurrency: 1, enabled: true }))
      .toBeNull();
    expect(
      submittedEdit({ attempts: 1, timeout_seconds: 1, max_concurrency: 1, enabled: "yes" }),
    ).toBeNull();
  });
});

describe("what the screen shows", () => {
  test("every field the API sends about a rung reaches the screen", async () => {
    // What breaks if this is deleted: a field arrives and is dropped in silence. No route
    // sends a schema for this screen, so the columns are a list somebody wrote, and the only
    // thing keeping it in step with the response model is this. Asserted in both directions:
    // a column naming a field the model does not have would render an empty column headed
    // with a word from nowhere.
    const declared = backendRungViewFields();
    const rendered = MATRIX_COLUMNS.map((column) => String(column.name));

    expect([...rendered].sort()).toEqual([...declared].sort());
  });

  test("the grid adds one column that is not a field, and only for an editor", async () => {
    // What breaks if this is deleted: the test above stops being able to mean anything,
    // because any extra column could be waved through as the edit control. The edit column is
    // named once and is the only id in the grid that is not a field of the response model.
    const fields = MATRIX_COLUMNS.map((column) => String(column.name));
    const withoutEditor = matrixColumns(false, () => null).map((column) => String(column.id));
    const withEditor = matrixColumns(true, () => null).map((column) => String(column.id));

    expect(withoutEditor).toEqual(fields);
    expect(withEditor).toEqual([...fields, EDIT_COLUMN]);
  });

  test("the page says the same thing whatever number of rungs arrived", async () => {
    // What breaks if this is deleted: a count. "4 rungs" beside a matrix is one line, and the
    // rule this console keeps is one rule about counts rather than one per endpoint, because
    // the person who writes the count here is the person who copies it onto a screen where
    // the collection is filtered. Everything outside the table body is compared byte for byte
    // between a page of one rung and a page of three, so a number anywhere on the screen
    // fails this.
    const chrome = (markup: string) => markup.replace(/<tbody>[\s\S]*?<\/tbody>/, "<tbody/>");
    const one = await consoleAt("/routing", { matrix: page([rung()]) });
    const three = await consoleAt("/routing", {
      matrix: page([
        rung(),
        rung({ id: "22222222-2222-4222-8222-222222222222", position: 1 }),
        rung({ id: "33333333-3333-4333-8333-333333333333", position: 2 }),
      ]),
    });

    expect(chrome(three.container.innerHTML)).toBe(chrome(one.container.innerHTML));
  });

  test("a full page says there is more without saying how much", async () => {
    // What breaks if this is deleted: either the sentence or its emptiness. Without the
    // sentence a person reading a truncated matrix has no sign of it, which is the gap the
    // records screen still has; with a number in it the sentence becomes the count this whole
    // console refuses. Both are asserted, and the number check is a digit search over the
    // sentence rather than a comparison with an expected wording.
    const full = await consoleAt("/routing", { matrix: page([rung()], { truncated: true }) });
    const short = await consoleAt("/routing", { matrix: page([rung()], { truncated: false }) });

    expect(full.container.textContent).toContain(THERE_IS_MORE);
    expect(THERE_IS_MORE).not.toMatch(/\d/);
    expect(short.container.textContent).not.toContain(THERE_IS_MORE);
  });

  test("an address naming a rung the page does not carry says so and asks for nothing else", async () => {
    // What breaks if this is deleted: a bad link renders a screen with no form and no reason,
    // or worse, a second request for one rung. There is no per-rung route, and inventing one
    // would be the console asking a question the API has not been given.
    const { container, idp } = await consoleAt("/routing/44444444-4444-4444-8444-444444444444", {
      matrix: page([rung()], { editable: true }),
    });

    expect(container.textContent).toContain(NO_SUCH_RUNG);
    expect(container.querySelector(".form")).toBeNull();
    expect(matrixRequests(idp)).toHaveLength(1);
  });

  test("a refusal is the API's sentence and the page adds no reading of it", async () => {
    // What breaks if this is deleted: the console explains a 404 it must not explain. The
    // matrix route answers a caller who may not read it and one who may not change it with
    // the same body, so any wording distinguishing them, including a sympathetic one about
    // permissions, rebuilds the difference the taxonomy spent itself removing.
    const idp = fakeIdentityProvider({
      api(url) {
        if (!url.includes("/api/v1/routing/rungs")) {
          return null;
        }
        return new Response(JSON.stringify({ message: "I could not find that.", trace_id: "" }), {
          status: 404,
          headers: { "content-type": "application/json", "x-trace-id": "trace-matrix" },
        });
      },
    });
    const loaded = await loadConsole({ idp });
    await signIn(loaded);
    const { routes } = await import("../src/App");
    const router = createMemoryRouter(routes, { initialEntries: ["/routing"] });
    const { container } = render(<RouterProvider router={router} />);
    await waitFor(() => {
      if (!container.querySelector(".notice__body")) {
        throw new Error("the failure has not arrived");
      }
    });

    expect(container.querySelector(".notice__body")?.textContent).toBe("I could not find that.");
    expect(container.querySelector(".notice__trace")?.textContent).toContain("trace-matrix");
    expect(container.querySelector(".grid__empty")).toBeNull();
  });
});

describe("saving one rung", () => {
  test("a save is a PATCH to the rung in the address, carrying the four dials", async () => {
    // What breaks if this is deleted: the write. Both halves are checked because either alone
    // passes for the wrong reason: a request to the right address proves nothing about what
    // was in it, and a body with four keys proves nothing about which rung it reached. The
    // key set is compared with the route's own model rather than with a list here.
    const { container, idp } = await consoleAt("/routing/11111111-1111-4111-8111-111111111111", {
      matrix: page([rung()], { editable: true }),
    });

    const submit = container.querySelector(".form button[type=submit]");
    expect(submit).not.toBeNull();
    await act(async () => {
      fireEvent.click(submit as HTMLElement);
    });
    await waitFor(() => expect(writes(idp)).toHaveLength(1));

    const sent = writes(idp)[0];
    expect(sent?.url).toContain("/api/v1/routing/rungs/11111111-1111-4111-8111-111111111111");
    expect(Object.keys(sent?.body as Record<string, unknown>).sort()).toEqual(
      [...backendRungEditFields()].sort(),
    );
  });

  test("a saved rung is read back from the API rather than assumed", async () => {
    // What breaks if this is deleted: the grid shows what the console sent. `role` is derived
    // on write, so the value written and the value stored are about to stop being the same
    // thing, and a screen that patched its own row in place would report a label the database
    // does not hold. The second GET is the whole property.
    const { container, idp } = await consoleAt("/routing/11111111-1111-4111-8111-111111111111", {
      matrix: page([rung()], { editable: true }),
    });

    expect(matrixRequests(idp).filter((url) => !url.pathname.endsWith("111"))).toHaveLength(1);
    await act(async () => {
      fireEvent.click(container.querySelector(".form button[type=submit]") as HTMLElement);
    });

    await waitFor(() =>
      expect(
        idp.urls.filter((url) => url.endsWith(`/api/v1${matrixApiPath()}`)),
      ).toHaveLength(2),
    );
  });

  test("a refused save shows the API's sentence and the rung stays on the screen", async () => {
    // What breaks if this is deleted: a refused write is silent, or it takes the form away.
    // The refusal here is the same 404 a caller who may not read the matrix gets, so the
    // console has nothing to add to it, and the form has to stay so the person can see what
    // they tried to save.
    const { container, idp } = await consoleAt("/routing/11111111-1111-4111-8111-111111111111", {
      matrix: page([rung()], { editable: true }),
      saved: { status: 404, body: { message: "I could not find that.", trace_id: "" }, traceId: "t-1" },
    });

    await act(async () => {
      fireEvent.click(container.querySelector(".form button[type=submit]") as HTMLElement);
    });
    await waitFor(() => expect(writes(idp)).toHaveLength(1));
    await waitFor(() => {
      if (!container.querySelector(".form .notice__body")) {
        throw new Error("the failure has not arrived");
      }
    });

    expect(container.querySelector(".form .notice__body")?.textContent).toBe(
      "I could not find that.",
    );
    expect(container.querySelector(".form")).not.toBeNull();
  });
});

describe("the form the rung is edited through", () => {
  test("the form's fields are the route's fields and its bounds are the route's bounds", async () => {
    // What breaks if this is deleted: the console's copy of four numbers drifts from the
    // route's, and the form starts offering a value that comes back 422 carrying
    // `HTTPValidationError`, which is not `ErrorBody` and reads as "Something went wrong."
    // The declared schema is read out of the API's own document, which carries bounds pydantic
    // derived from the column's `Numeric(6, 2)` and from the table's check constraints, so
    // this is not two console constants compared with each other.
    const declared = declaredRequestBodySchema(EDIT_OPERATION, "patch");
    const declaredProperties = declared["properties"] as Record<string, Record<string, unknown>>;
    const mine = (RUNG_EDIT_SCHEMA.properties ?? {}) as Record<string, Record<string, unknown>>;

    expect(Object.keys(mine).sort()).toEqual(Object.keys(declaredProperties).sort());
    expect(Object.keys(mine).sort()).toEqual([...backendRungEditFields()].sort());

    let compared = 0;
    for (const [name, schema] of Object.entries(declaredProperties)) {
      for (const bound of ["type", "minimum", "maximum", "exclusiveMinimum"]) {
        if (!(bound in schema)) {
          continue;
        }
        compared += 1;
        expect(mine[name]?.[bound], `${name}.${bound}`).toBe(schema[bound]);
      }
    }
    // A loop over an empty document would pass every assertion inside it.
    expect(compared).toBeGreaterThan(8);
  });

  test("the route refuses a key the form does not carry, so the two refusals agree", async () => {
    // What breaks if this is deleted: the console's discipline becomes the only thing
    // stopping a role being written. The route's model forbids extra keys and the document
    // says so; if it ever stopped saying so, a console bug would become a data bug, and this
    // is where that change is noticed.
    const declared = declaredRequestBodySchema(EDIT_OPERATION, "patch");

    expect(declared["additionalProperties"]).toBe(false);
    expect(declared["required"]).toEqual(backendRungEditFields());
  });

  test("the form starts from the values the API answered", async () => {
    // What breaks if this is deleted: a form that opens on defaults. Saving from it would
    // then write a number nobody chose over a number somebody did, and every field is
    // required, so a person pressing save without editing anything would retune the rung.
    const values = editableDefaults(rung({ attempts: 3, timeout_seconds: 90, max_concurrency: 4 }));

    expect(values).toEqual({
      attempts: 3,
      timeout_seconds: 90,
      max_concurrency: 4,
      enabled: true,
    });
  });
});

describe("reading a page and a scope", () => {
  test("a page's count stops at the reader and never reaches a screen", async () => {
    // What breaks if this is deleted: `total` becomes a value a renderer can reach. The API
    // sends the field and never populates it, and a screen holding the whole body is one line
    // away from showing whatever it does hold. This keeps the reader's output to three fields
    // by asserting on its shape rather than on its values.
    const read = readMatrixPage({ items: [rung()], total: 47, truncated: true, editable: true });

    expect(Object.keys(read).sort()).toEqual(["editable", "rungs", "truncated"]);
    expect(JSON.stringify(read)).not.toContain("47");
  });

  test("a body that is not a page is an empty page rather than a crash", async () => {
    // What breaks if this is deleted: a console built against a different API renders a blank
    // screen with a stack trace behind it instead of an empty grid. The shape is fixed by a
    // response model in this repository, so there is no sentence worth composing about a body
    // that does not match it.
    expect(readMatrixPage(null).rungs).toEqual([]);
    expect(readMatrixPage({ items: "no" }).rungs).toEqual([]);
    expect(readMatrixPage({ items: [], editable: "yes" }).editable).toBe(false);
  });

  test("a rung is found by the id in the address and by nothing else", async () => {
    // What breaks if this is deleted: the editor opens on the wrong rung, most likely the
    // first one, and a save then retunes a rung nobody was looking at.
    const rows = [rung(), rung({ id: "22222222-2222-4222-8222-222222222222", position: 1 })];

    expect(rungById(rows, "22222222-2222-4222-8222-222222222222")?.position).toBe(1);
    expect(rungById(rows, "nothing-like-that")).toBeNull();
  });

  test("a scope is shown in the API's own words and an empty one shows nothing", async () => {
    // What breaks if this is deleted: the scope column starts explaining itself. "starts
    // with" for `prefix` would be a fourth vocabulary after the grant tables, the query
    // compiler and every support conversation, and a word like "all" for an unrestricted
    // scope would be this console naming a state the payload does not carry.
    expect(clauseText({ field: "region", op: "prefix", value: "eu-" })).toBe("region prefix eu-");
    expect(clauseText({ field: "region", op: "any", value: null })).toBe("region any");
    expect(clauseText({ field: "region", op: "in", value: ["eu-west-1", "ap-southeast-1"] })).toBe(
      'region in ["eu-west-1","ap-southeast-1"]',
    );
    expect(scopeLines({ clauses: [] })).toEqual([]);
    expect(scopeLines({})).toEqual([]);
  });

  test("a rung's scope reaches its cell", async () => {
    // What breaks if this is deleted: the scope column renders nothing for every rung, which
    // looks exactly like a matrix whose rungs are all unrestricted. M5.5.1's residency
    // constraint reaches the matrix through these clauses, so a column that silently shows
    // none of them hides the one thing an operator checks before a compliance conversation.
    const { container } = await consoleAt("/routing", {
      matrix: page([rung({ scope: { clauses: [{ field: "region", op: "prefix", value: "eu-" }] } })]),
    });

    expect(container.querySelector(".grid__table tbody")?.textContent).toContain(
      "region prefix eu-",
    );
  });
});
