/**
 * The grid: what it renders, and the three things it must never be able to do.
 *
 * It must not slice, because a client-side page over a permission-filtered result set is a
 * first page of whatever arrived, presented as the first page of what exists. It must not
 * filter, because a filter applied in a browser can only narrow rows the caller already
 * received and looks identical on screen to one the API applied. And it must not put a
 * number of rows anywhere, because a count beside a list of the permitted rows discloses
 * the rest by subtraction, which is the leak `brain.core.redaction` spends a whole rule on.
 *
 * The first two are checked twice over: structurally, by reading which TanStack features
 * the table registers, and behaviourally, by handing it rows that a client-side page or
 * filter would have removed and finding all of them on the screen. The structural half is
 * read out of the installed library's own list of features rather than a copy of that list
 * kept here, so it stays honest as the library grows.
 *
 * Task ids: M32.5.2.1
 */

import { fireEvent, render } from "@testing-library/react";
import { describe, expect, test } from "vitest";
import { chipCell, statusCell } from "../src/components/cells";
import { DataTable, type GridColumn } from "../src/components/DataTable";
import { lockedCellKey } from "../src/components/paging";
import { Chip } from "../src/ui/Chip";
import { Lock } from "../src/ui/Lock";
import { Status } from "../src/ui/Status";
import { backendPublicMessages } from "./support/python";
import {
  callOptionsOf,
  interfaceMemberNames,
  namedImportsFrom,
  parseConsoleSource,
} from "./support/typescript";

const TABLE_MODULE = "src/components/DataTable.tsx";
const STOCK_FEATURES =
  "node_modules/@tanstack/table-core/dist/features/stockFeatures.d.ts";

/** The type-only slots on the features object. Neither registers any behaviour. */
const TYPE_ONLY_SLOTS = ["columnMeta", "tableMeta"];

interface Client extends Record<string, unknown> {
  readonly id: string;
  readonly name: string;
  readonly owner: string;
}

const COLUMNS: GridColumn<Client>[] = [
  { id: "name", accessorKey: "name", header: "Client" },
  {
    id: "owner",
    accessorKey: "owner",
    header: "Owner",
    meta: { filterLabel: "owner" },
  },
];

function client(id: string, name: string, owner: string): Client {
  return { id, name, owner };
}

const ROWS: Client[] = [
  client("one", "Acme", "Ada"),
  client("two", "Borden", "Bo"),
  client("three", "Corvid", "Cai"),
  client("four", "Delve", "Dee"),
  client("five", "Ember", "Eli"),
];

function grid(props: Partial<Parameters<typeof DataTable<Client>>[0]> = {}) {
  return render(
    <DataTable
      caption="Clients"
      columns={COLUMNS}
      rows={ROWS}
      rowId={(row) => row.id}
      {...props}
    />,
  ).container;
}

/** The first cell of every rendered row, which is the client's name. */
function namesOn(container: HTMLElement): string[] {
  return [...container.querySelectorAll("tbody tr")].map(
    (row) => row.querySelector("td")?.textContent ?? "",
  );
}

describe("what the grid is capable of", () => {
  test("the grid registers no feature that could remove or reorder a row", () => {
    // What breaks if this is deleted: the mechanism. Registering `rowPaginationFeature` or
    // `columnFilteringFeature` would put a second, client-side answer next to the API's,
    // and the two look the same on a screen. With neither registered, `pageCount`,
    // `rowCount` and `manualPagination` are not options this table has, so the mistake is a
    // type error rather than a behaviour. The feature names are read out of the installed
    // library's own interface, so a feature added by a future version is covered by this
    // without anybody updating a list here.
    const stock = interfaceMemberNames(parseConsoleSource(STOCK_FEATURES), "StockFeatures");
    expect(stock).toContain("rowPaginationFeature");
    expect(stock).toContain("columnFilteringFeature");

    const registered = Object.keys(
      callOptionsOf(parseConsoleSource(TABLE_MODULE), "tableFeatures"),
    );
    expect(registered.filter((name) => stock.includes(name))).toEqual([]);
    expect(registered.filter((name) => !TYPE_ONLY_SLOTS.includes(name))).toEqual([]);
  });

  test("the grid imports no row model that could reduce the rows", () => {
    // What breaks if this is deleted: the other way in. A row model factory registered in a
    // feature slot reduces rows without the feature name appearing in the object above, and
    // `createPaginatedRowModel` reads as plumbing rather than as a decision. The positive
    // half is asserted too, because a test that only forbids things passes for a file that
    // imports nothing and renders no table at all.
    const imported = namedImportsFrom(parseConsoleSource(TABLE_MODULE), "@tanstack/react-table");
    expect(imported).toContain("useTable");
    expect(imported).toContain("tableFeatures");
    expect(imported.filter((name) => /RowModel/.test(name))).toEqual([]);
  });

  test("the grid renders every row it was given, in the order it was given them", () => {
    // What breaks if this is deleted: everything else here is satisfied by a table that
    // renders nothing. This is the positive sibling, and it is also the plainest statement
    // of the rule: what is on the screen is the page the API sent, not a view of it.
    expect(namesOn(grid())).toEqual(["Acme", "Borden", "Corvid", "Delve", "Ember"]);
  });

  test("a filter that matches nothing on the screen removes nothing from it", () => {
    // What breaks if this is deleted: the grid starts filtering the rows it already has.
    // The result would look right, because narrowing a permitted set produces permitted
    // rows, and it would mean something different: "the records that match" rather than
    // "the records that match among the ones you were already sent". The filter here
    // matches none of the five names, so a client-side filter would leave an empty table
    // and this asserts all five are still there.
    const container = grid({ filters: { owner: "nobody-by-that-name" } });
    expect(namesOn(container)).toHaveLength(ROWS.length);
  });

  test("typing in a filter box asks the caller to refetch and changes no row", () => {
    // What breaks if this is deleted: the filter box stops being wired to anything, or
    // starts being wired to the rows in front of it. The console's job is to send the
    // filter to the API and render whatever comes back; the rows on screen must not move
    // until they do.
    const asked: [string, string][] = [];
    const container = render(
      <DataTable
        caption="Clients"
        columns={COLUMNS}
        rows={ROWS}
        rowId={(row) => row.id}
        filters={{}}
        onFilterChange={(column, value) => asked.push([column, value])}
      />,
    ).container;

    const box = container.querySelector(".grid__filter") as HTMLInputElement;
    fireEvent.change(box, { target: { value: "Ada" } });

    expect(asked).toEqual([["owner", "Ada"]]);
    expect(namesOn(container)).toHaveLength(ROWS.length);
  });

  test("a filter box appears only on a column that declares one", () => {
    // What breaks if this is deleted: every column grows a filter box, including the ones
    // with no server-side filter behind them. A box that sends a parameter the API ignores
    // is worse than no box: it silently returns the unfiltered page, and the reader
    // believes they are looking at a narrowed one.
    const boxes = [...grid().querySelectorAll(".grid__filter")];
    expect(boxes).toHaveLength(1);
    expect(boxes[0]?.getAttribute("aria-label")).toBe("Filter by owner");
  });
});

describe("what the grid says about rows it does not have", () => {
  test("nothing on the screen is a count of rows", () => {
    // What breaks if this is deleted: "showing 5 of 47", or its innocent cousin "5 rows".
    // A count beside a list filtered by what somebody may see discloses the difference by
    // subtraction, and the number that does it is one the API was entitled to compute. The
    // fixture carries no digits of its own, so any digit on the screen came from the table
    // rather than from the data.
    const text = grid({ hasNext: true, canGoBack: true }).textContent ?? "";
    expect(text).not.toMatch(/\d/);
  });

  test("the pager offers the next page and the previous one and no other destination", () => {
    // What breaks if this is deleted: page numbers, a last-page button, or a page-size
    // menu. All three need a total to be meaningful, none of them can have one, and each
    // would end up displaying a number about rows nobody has seen. Two buttons, each
    // disabled when there is nowhere to go, is the whole of what a cursor supports.
    const container = grid({ hasNext: true, canGoBack: false });
    const buttons = [...container.querySelectorAll("button")];
    expect(buttons.map((button) => button.textContent)).toEqual(["Previous", "Next"]);
    expect(buttons[0]?.disabled).toBe(true);
    expect(buttons[1]?.disabled).toBe(false);
  });

  test("an empty page says the same thing whether or not a filter is applied", () => {
    // What breaks if this is deleted: the console explains an empty result. "No records
    // match your filter" is the helpful version and it is the disclosure: it says the
    // filter was the reason, which says there was something there for it to exclude. Empty
    // because nothing exists and empty because nothing this caller holds reaches anything
    // are the same event, and `brain.app.handle_brain_error` gives them the same status and
    // the same body for exactly this reason.
    const unfiltered = grid({ rows: [] }).querySelector(".grid__empty");
    const filtered = grid({ rows: [], filters: { owner: "Ada" } }).querySelector(".grid__empty");

    expect(unfiltered?.outerHTML).toBeTruthy();
    expect(filtered?.outerHTML).toBe(unfiltered?.outerHTML);
  });

  test("a failure is shown in the API's own words with nothing added", () => {
    // What breaks if this is deleted: the console starts speaking for the API about a 404,
    // which is the outcome DENIED and ABSENT share. The sentence is read out of the Python
    // source, so this is not the console's copy of the message compared with itself, and
    // the assertion is that exactly that sentence and nothing else reaches the screen. A
    // grid that added "you may not have permission to view this" would turn one status code
    // back into two answers in the friendliest possible voice.
    const sentence = backendPublicMessages()["DENIED"];
    expect(sentence).toBeTruthy();

    const container = grid({
      rows: [],
      failure: { status: 404, message: sentence as string, traceId: "trace-abc", outcome: "" },
    });

    const notice = container.querySelector(".notice");
    expect(notice?.querySelector(".notice__body")?.textContent).toBe(sentence);
    // And the empty sentence is not shown as well: two answers to one question is the
    // beginning of a reader working out which one is the real one.
    expect(container.querySelector(".grid__empty")).toBeNull();
  });

  test("a page still loading is not reported as a page with nothing on it", () => {
    // What breaks if this is deleted: every grid flashes "Nothing to show" before its first
    // answer arrives. That is a statement about somebody's data made before anybody asked,
    // and on a slow connection it is the sentence they remember.
    const container = grid({ rows: [], busy: true });
    expect(container.querySelector(".grid__empty")).toBeNull();
    expect(container.querySelector(".grid__busy")).not.toBeNull();
  });
});

describe("what a cell is rendered with", () => {
  interface Connector extends Record<string, unknown> {
    readonly id: string;
    readonly team: string;
    readonly state: string;
  }

  const CONNECTOR_COLUMNS: GridColumn<Connector>[] = [
    { id: "team", accessorKey: "team", header: "Team", cell: chipCell },
    { id: "state", accessorKey: "state", header: "State", cell: statusCell },
  ];

  function connectors(): HTMLElement {
    return render(
      <DataTable
        caption="Connectors"
        columns={CONNECTOR_COLUMNS}
        rows={[{ id: "hubspot", team: "Sales", state: "down" }]}
        rowId={(row) => row.id}
      />,
    ).container;
  }

  test("a grid renders a state through the shared primitive and not a lookalike", () => {
    // What breaks if this is deleted: the grid grows its own labels. A status column is
    // exactly where somebody writes a coloured span, because the primitive is one import
    // away and a span is quicker. Two of those and the console has a second palette written
    // in TypeScript, which no stylesheet test can see, and a colour chosen per grid rather
    // than per state. Comparing the cell against the primitive rendered on its own is the
    // assertion, because a lookalike that happened to carry the same class would still
    // differ from this.
    const cell = connectors().querySelectorAll("tbody td")[1] as HTMLElement;
    expect(cell.innerHTML).toBe(render(<Status state="down" />).container.innerHTML);
  });

  test("a grid renders a plain value through the shared primitive too", () => {
    // What breaks if this is deleted: the neutral half of the same rule. A chip carries a
    // value out of a record and has exactly one appearance, and a grid that styled its own
    // would be free to make that appearance depend on the value, which is the console
    // forming an opinion about data it did not produce.
    const cell = connectors().querySelector("tbody td") as HTMLElement;
    expect(cell.innerHTML).toBe(render(<Chip label="Sales" />).container.innerHTML);
  });
});

describe("a withheld cell", () => {
  test("a locked cell renders the lock and not the value behind it", () => {
    // What breaks if this is deleted: the whole point of field-level redaction, in the one
    // place records are shown. The value must not be in the DOM at all, not hidden by CSS
    // and not in a title attribute: anything in the markup is one developer-tools panel
    // away from being read.
    const container = grid({
      lockedCells: new Set([lockedCellKey("two", "owner")]),
    });

    const locked = container.querySelectorAll("tbody tr")[1]?.querySelectorAll("td")[1];
    expect(locked?.textContent).not.toContain("Bo");
    expect(container.innerHTML).not.toContain("Bo<");
    expect(locked?.querySelector(".lock")).not.toBeNull();
  });

  test("a lock in a grid is the same lock as anywhere else", () => {
    // What breaks if this is deleted: the grid grows its own way of saying "withheld". A
    // greyed cell, a dash, a tinted row or a tooltip explaining why would each be the
    // lock's reason rendered in a new place, and a grid is where a designer reaches for one
    // first. `render_lock` takes no arguments and `Lock` takes no props; this asserts the
    // markup that reaches a table cell is byte for byte the markup that reaches anywhere.
    const standalone = render(<Lock />).container.innerHTML;
    const container = grid({ lockedCells: new Set([lockedCellKey("one", "name")]) });
    const cell = container.querySelector("tbody tr td") as HTMLElement;

    expect(cell.innerHTML).toBe(standalone);
  });

  test("only the cell that was withheld is locked", () => {
    // What breaks if this is deleted: a lock that spreads. Locking a whole row because one
    // field on it was withheld tells the reader which records carry a restricted value,
    // which is a fact about the policy and about the record, and it is the kind of change
    // that gets made to tidy up a ragged column.
    const container = grid({ lockedCells: new Set([lockedCellKey("two", "owner")]) });
    expect(container.querySelectorAll(".lock")).toHaveLength(1);
    expect(namesOn(container)).toEqual(["Acme", "Borden", "Corvid", "Delve", "Ember"]);
  });
});
