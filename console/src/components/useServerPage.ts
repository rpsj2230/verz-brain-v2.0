/**
 * One page of rows, fetched from the API, with the paging and filtering left there.
 *
 * **The server does the work, and the reason is not performance.** A console that fetched
 * everything and sliced it would be holding, in a browser, every row the API was willing to
 * give it, and paging would then be a presentation choice over a result set that had
 * already crossed the wire. It would also be wrong the moment a result set is bigger than a
 * page: the first page would be the first page of what fitted, and nothing would say so.
 * Filtering has the sharper version of the same problem. A filter applied in the browser is
 * applied to the rows this caller already received, so it can only ever narrow a set the
 * API chose, and the narrowing looks identical to a filter the API applied. Somebody
 * reading the screen cannot tell which happened, and neither can anybody reading this code
 * six months later.
 *
 * **Nothing here decides what may be fetched.** The path and the filters go out as written
 * and the API answers from grants this browser never sees. A refusal comes back as a value
 * and is rendered in the API's own words: see `api/errors.ts` for why a 404 must not be
 * softened into a sentence about permissions.
 *
 * **A superseded request never overwrites a newer one.** Typing in a filter box starts a
 * request per keystroke, and they do not come back in order. Each is given a sequence
 * number and only the current one is allowed to set state, so the rows on the screen are
 * always the answer to the question currently on the screen. Without it the grid settles on
 * whichever answer was slowest, which looks exactly like a filter that does not work.
 *
 * **There is no endpoint to call yet.** Nothing is mounted under `/api/v1`, so this has
 * never spoken to a real route: the tests drive it against a stand-in that answers in the
 * shape `brain.api.Page` describes. What that checks is this console's half of the
 * conversation. The query parameter names in `paging.ts` are the part that has to be agreed
 * with whoever writes the first list endpoint.
 *
 * Task ids: M32.5.2.1
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { request } from "../api/client";
import type { ApiFailure } from "../api/errors";
import {
  DEFAULT_PAGE_SIZE,
  FIRST_PAGE,
  back,
  forward,
  lockedCellsFrom,
  pageQuery,
  readPage,
  UnreadablePage,
  type PagePosition,
} from "./paging";

/**
 * What a person is told when the API answered with something that was not a page.
 *
 * About the console, not about the data, and deliberately so: this is the one failure here
 * that is nobody's permission problem, and saying anything about the request would be the
 * console explaining an outcome it did not observe.
 */
const UNREADABLE_ANSWER = "The console could not read the answer to that.";

export interface ServerPage<T> {
  /** Exactly the rows the API returned for this page. Never a subset computed here. */
  readonly rows: readonly T[];
  /** Keys of cells the API withheld, from `lockedCellKey`. Rendered as locks. */
  readonly lockedCells: ReadonlySet<string>;
  /** The failure, in the API's own words, or null. */
  readonly failure: ApiFailure | null;
  /** A request is in flight. Not an error state and not an empty one. */
  readonly busy: boolean;
  /** Whether the API sent a cursor for a page after this one. */
  readonly hasNext: boolean;
  /** Whether this console has a page to go back to. Never rendered as a number. */
  readonly canGoBack: boolean;
  /** The filters currently being asked for, by column. */
  readonly filters: Readonly<Record<string, string>>;
  readonly showNext: () => void;
  readonly showPrevious: () => void;
  /** Set one column's filter. Always returns to the first page; see below. */
  readonly setFilter: (column: string, value: string) => void;
}

interface Answer<T> {
  readonly rows: readonly T[];
  readonly lockedCells: ReadonlySet<string>;
  readonly nextCursor: string | null;
  readonly failure: ApiFailure | null;
}

const NOTHING: Answer<never> = {
  rows: [],
  lockedCells: new Set(),
  nextCursor: null,
  failure: null,
};

function unreadable(): ApiFailure {
  return { status: 0, message: UNREADABLE_ANSWER, traceId: "", outcome: "failed" };
}

export function useServerPage<T>(
  path: string,
  options: { readonly pageSize?: number } = {},
): ServerPage<T> {
  const pageSize = options.pageSize ?? DEFAULT_PAGE_SIZE;
  const [position, setPosition] = useState<PagePosition>(FIRST_PAGE);
  const [filters, setFilters] = useState<Readonly<Record<string, string>>>({});
  const [answer, setAnswer] = useState<Answer<T>>(NOTHING);
  const [busy, setBusy] = useState(true);
  const sequence = useRef(0);

  const query = useMemo(
    () => pageQuery({ limit: pageSize, cursor: position.cursor, filters }),
    [pageSize, position.cursor, filters],
  );

  useEffect(() => {
    sequence.current += 1;
    const mine = sequence.current;
    const controller = new AbortController();
    setBusy(true);

    void (async () => {
      const result = await request<unknown>(`${path}${query}`, {
        signal: controller.signal,
      });
      if (mine !== sequence.current) {
        // A newer question is already being asked. This answer is to the old one, and
        // putting it on the screen would show rows that do not match the filter box.
        return;
      }
      if (!result.ok) {
        setAnswer({ ...NOTHING, failure: result.failure });
        setBusy(false);
        return;
      }
      try {
        const page = readPage<T>(result.data);
        const locked = (result.data as { locked?: unknown }).locked;
        setAnswer({
          rows: page.items,
          lockedCells: lockedCellsFrom(locked),
          nextCursor: page.nextCursor,
          failure: null,
        });
      } catch (error) {
        if (!(error instanceof UnreadablePage)) {
          throw error;
        }
        setAnswer({ ...NOTHING, failure: unreadable() });
      }
      setBusy(false);
    })();

    return () => {
      controller.abort();
    };
  }, [path, query]);

  const showNext = useCallback(() => {
    const cursor = answer.nextCursor;
    if (cursor === null) {
      return;
    }
    setPosition((current) => forward(current, cursor));
  }, [answer.nextCursor]);

  const showPrevious = useCallback(() => {
    setPosition(back);
  }, []);

  const setFilter = useCallback(
    (column: string, value: string) => {
      if (filters[column] === value) {
        // Setting a filter to what it already says is not a change, and treating it as one
        // would jump a reader back to the first page for pressing a key with no effect.
        return;
      }
      setFilters({ ...filters, [column]: value });
      // Back to the start, and this is the part that is easy to leave out. A cursor is a
      // position in one ordering of one filtered set; carrying it across a filter change
      // means paging into the middle of a result set that no longer exists, and the API
      // would answer something plausible rather than refusing.
      setPosition(FIRST_PAGE);
    },
    [filters],
  );

  return {
    rows: answer.rows,
    lockedCells: answer.lockedCells,
    failure: answer.failure,
    busy,
    hasNext: answer.nextCursor !== null,
    canGoBack: position.trail.length > 0,
    filters,
    showNext,
    showPrevious,
    setFilter,
  };
}
