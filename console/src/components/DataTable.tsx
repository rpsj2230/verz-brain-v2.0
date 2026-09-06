/**
 * Every grid in this console. One page of rows, rendered exactly as they arrived.
 *
 * **This table cannot page, filter or sort. That is the mechanism, not a limitation.**
 * TanStack Table v9 registers features explicitly, and this one registers none: the
 * `features` object below carries only type-only slots. The consequence is stronger than a
 * convention, and stronger than v8's `manualPagination: true` would have been, because a
 * boolean is a thing somebody can flip. With no feature registered, `pageCount`, `rowCount`
 * and `manualPagination` are not options this table has. Writing one is a type error, not a
 * behaviour change, and `table.getRowModel()` is the core model with nothing applied to it.
 *
 * Why that matters here more than in most consoles:
 *
 * - **A client-side page is a lie about a permission-filtered set.** The rows a caller gets
 *   are what the API decided they may see. Slicing them in a browser produces a first page
 *   of what happened to arrive, and nothing on the screen distinguishes that from the first
 *   page of what exists.
 * - **A client-side filter is worse, because it looks the same as a server one.** Narrowing
 *   rows the caller already received cannot reach a row the API withheld, so the result is
 *   correct and its meaning is wrong: the person reads it as "these are the matching
 *   records" when it is "these are the matching records among the ones already fetched".
 * - **A row count is the disclosure.** Not the count of hidden rows, which nobody would
 *   write: the count of all rows, beside a list of the permitted ones. "Showing 20 of 47"
 *   is 27 facts the reader did not have, and it arrives through a number the API was
 *   entitled to compute. `brain.core.redaction` withholds a count field whenever the
 *   collection it counts was filtered for that caller, and a table footer is where a
 *   console rebuilds it out of two harmless halves. So: no total, no page count, no page
 *   number, no "showing 1 to 20". The pager says whether there is more and nothing else.
 *
 * **A withheld cell renders through `ui/Lock.tsx` and through nothing else.** The lock takes
 * no props and has one appearance, because a lock that varied by field or reason would let
 * two people comparing screens learn which of them was refused and why. A table is the place
 * that rule gets broken by accident, because a grid is where a designer naturally reaches
 * for a greyed cell, a dash, a tooltip saying why, or a tinted row. There is one lock here
 * and the reason never reaches this file: `lockedCellsFrom` keeps two names per entry.
 *
 * **An empty page says one sentence.** Empty because there is nothing, and empty because
 * nothing this caller holds reaches anything, are the same event and must look the same.
 * The dangerous version is not "you may not see these": it is the helpful one, "no records
 * match your filter", which says the filter was the reason and therefore that there was
 * something to filter. See `A_404_IS_NOT_AN_EXPLANATION` in `api/errors.ts`.
 *
 * **Nothing here fetches.** `useServerPage.ts` asks the API and this renders the answer, in
 * the same split the Python side keeps between a module that holds a rule and a module that
 * holds a client. It also means a test can hand this table a result set and check what it
 * shows without a server, which is how the no-slicing property is checked at all.
 *
 * Task ids: M32.5.2.1
 */

import {
  flexRender,
  tableFeatures,
  useTable,
  type ColumnDef,
  type RowData,
} from "@tanstack/react-table";
import type { ApiFailure } from "../api/errors";
import { Lock } from "../ui/Lock";
import { Notice } from "../ui/Notice";
import { filterValueBudget, lockedCellKey } from "./paging";

/**
 * Written down because a footer count is the single most natural addition to a table and
 * the reviewer who waves it through will be right about everything except the subtraction.
 */
export const A_GRID_SHOWS_WHAT_IT_WAS_GIVEN =
  "This table renders the rows it was handed, in the order they arrived, and says nothing " +
  "about rows it was not handed. No total, no page count, no page number and no range. A " +
  "count beside a permission-filtered list discloses what was withheld by subtraction, and " +
  "a page number is the shortest route back to one.";

/** The one thing said about a page with no rows on it, whatever the reason. */
export const NOTHING_TO_SHOW = "Nothing to show.";

/** The one heading over any failure. The API's own sentence goes underneath it. */
export const SOMETHING_DID_NOT_WORK = "That did not work";

/** Per-column settings this console adds. A type-only slot; it registers no feature. */
export interface GridColumnMeta {
  /**
   * The name of the column in a filter box's label. Its presence is what gives the column
   * a filter box, so a column with no server-side filter behind it cannot grow one here by
   * accident.
   */
  readonly filterLabel?: string;
}

/**
 * The feature set: none.
 *
 * `columnMeta` is a type-only slot rather than a feature. Every optional feature is left
 * out, and the two that matter are `rowPaginationFeature` and `columnFilteringFeature`:
 * registering either would put a second, client-side answer to a question the API has
 * already answered, and the two answers look identical on screen.
 */
const features = tableFeatures({ columnMeta: {} as GridColumnMeta });

/** The feature set every column definition and every grid in this console is typed against. */
export type GridFeatures = typeof features;

/**
 * A column, typed against the feature set above so a feature-only option cannot be written.
 *
 * `RowData` is the library's own constraint on what a row may be, and it is repeated on
 * every generic here rather than relaxed: a row in this console is an object out of a
 * payload, and widening it would only move the failure to the first cell that tried to read
 * a field off something that has none.
 */
export type GridColumn<T extends RowData> = ColumnDef<GridFeatures, T>;

interface DataTableProps<T extends RowData> {
  /** What the table is, for a screen reader and for anybody reading it. */
  readonly caption: string;
  readonly columns: readonly GridColumn<T>[];
  /** Exactly one page, as the API sent it. Never a superset to be sliced. */
  readonly rows: readonly T[];
  /**
   * The record's own identifier, the one the API uses. It has to be the API's, because a
   * lock arrives as a record id and a field name and is matched against this.
   */
  readonly rowId: (row: T) => string;
  /** Keys from `lockedCellKey` for cells the API withheld. */
  readonly lockedCells?: ReadonlySet<string>;
  /** The failure, in the API's words, or null. Rendered instead of the empty sentence. */
  readonly failure?: ApiFailure | null;
  /** A request is in flight, so an empty page is not yet an answer. */
  readonly busy?: boolean;
  readonly hasNext?: boolean;
  readonly canGoBack?: boolean;
  readonly onNext?: () => void;
  readonly onPrevious?: () => void;
  /** The filter each column is currently asking for, by column id. */
  readonly filters?: Readonly<Record<string, string>>;
  /** Called as somebody types. The value goes to the API, never to these rows. */
  readonly onFilterChange?: (column: string, value: string) => void;
}

const NO_LOCKS: ReadonlySet<string> = new Set();
const NO_FILTERS: Readonly<Record<string, string>> = Object.freeze({});

export function DataTable<T extends RowData>({
  caption,
  columns,
  rows,
  rowId,
  lockedCells = NO_LOCKS,
  failure = null,
  busy = false,
  hasNext = false,
  canGoBack = false,
  onNext,
  onPrevious,
  filters = NO_FILTERS,
  onFilterChange,
}: DataTableProps<T>) {
  const table = useTable({
    features,
    columns: [...columns],
    data: rows,
    getRowId: (row: T) => rowId(row),
  });

  const filterable = table
    .getFlatHeaders()
    .some((header) => header.column.columnDef.meta?.filterLabel !== undefined);

  return (
    <div className="grid">
      <table className="grid__table">
        <caption className="grid__caption">{caption}</caption>
        <thead>
          {table.getHeaderGroups().map((group) => (
            <tr key={group.id}>
              {group.headers.map((header) => (
                <th key={header.id} scope="col">
                  {flexRender(header.column.columnDef.header, header.getContext())}
                </th>
              ))}
            </tr>
          ))}
          {filterable ? (
            <tr className="grid__filters">
              {table.getFlatHeaders().map((header) => {
                const label = header.column.columnDef.meta?.filterLabel;
                return (
                  <td key={header.id}>
                    {label === undefined ? null : (
                      <input
                        type="text"
                        className="grid__filter"
                        aria-label={`Filter by ${label}`}
                        // The term the route accepts, less what the column name and the
                        // separator take. Spent here rather than on the way out, because a
                        // term trimmed as it is sent asks a different question and answers it
                        // convincingly; one a person cannot finish typing is visible.
                        maxLength={filterValueBudget(header.column.id)}
                        value={filters[header.column.id] ?? ""}
                        onChange={(event) => {
                          onFilterChange?.(header.column.id, event.target.value);
                        }}
                      />
                    )}
                  </td>
                );
              })}
            </tr>
          ) : null}
        </thead>
        <tbody>
          {table.getRowModel().rows.map((row) => (
            <tr key={row.id}>
              {row.getAllCells().map((cell) => (
                <td key={cell.id}>
                  {lockedCells.has(lockedCellKey(row.id, cell.column.id)) ? (
                    <Lock />
                  ) : (
                    flexRender(cell.column.columnDef.cell, cell.getContext())
                  )}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>

      {failure ? (
        <Notice title={SOMETHING_DID_NOT_WORK} traceId={failure.traceId}>
          <p>{failure.message}</p>
        </Notice>
      ) : null}

      {/*
       * One sentence, and it is the same sentence whatever the filters say. "No records
       * match your filter" would name the filter as the reason, which says there was
       * something for it to exclude.
       */}
      {!failure && !busy && rows.length === 0 ? (
        <p className="grid__empty">{NOTHING_TO_SHOW}</p>
      ) : null}

      {busy ? (
        <p className="grid__busy" role="status">
          Loading.
        </p>
      ) : null}

      <div className="grid__pager">
        <button
          type="button"
          className="button"
          disabled={!canGoBack}
          onClick={() => {
            onPrevious?.();
          }}
        >
          Previous
        </button>
        <button
          type="button"
          className="button"
          disabled={!hasNext}
          onClick={() => {
            onNext?.();
          }}
        >
          Next
        </button>
      </div>
    </div>
  );
}
