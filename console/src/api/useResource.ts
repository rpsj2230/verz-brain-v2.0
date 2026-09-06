/**
 * One GET, once, for a page that renders a single object rather than a list.
 *
 * **It is the sibling of `useServerPage.ts` and deliberately not the same function.** A
 * page of rows has a position, a cursor and a set of filters, and every one of those is a
 * thing a caller can be wrong about. An object at a fixed address has none of them: there
 * is one request, it either answered or it did not, and the whole state is three values. A
 * hook that served both would carry the paging vocabulary into a screen that has no pages,
 * and the first person to reach for `showNext` on it would find it there.
 *
 * **A failure is a value, in the API's own words.** The same rule `api/client.ts` states
 * and for the same reason: the commonest failure in this system is a 404 that is a correct
 * answer to a correct question, and a hook that threw would push it into a component-level
 * catch that renders the word "error". Nothing here interprets a status, chooses a
 * sentence, or softens a refusal. See `A_404_IS_NOT_AN_EXPLANATION`.
 *
 * **Nothing is cached.** A second mount asks again. That is one wasted request and it is
 * cheaper than the alternative it prevents: a cached answer is an answer computed for
 * whoever was signed in when it was cached, and this console has exactly one place where a
 * fact about a person could be held past the moment they stopped being the person asking.
 * A cache here is that place. `brain.gate.resolve` caches entitlements behind a version
 * that changes when a grant changes; a browser has no such version, so it does not cache.
 *
 * **A request that outlives its component sets nothing.** The cleanup aborts the fetch and
 * marks the run dead, so an answer arriving after the page has gone finds nowhere to put
 * itself. Without it, navigating away during a slow request produces a state update on an
 * unmounted tree, which React reports as a warning and which in a test suite reads as a
 * flake rather than as the bug it is.
 *
 * Task ids: M32.5.1.1
 */

import { useEffect, useState } from "react";
import { request } from "./client";
import type { ApiFailure } from "./errors";

/** What one address answered with, or why it did not. */
export interface Resource<T> {
  /** The body, exactly as the API sent it, or null. Never a partial or defaulted one. */
  readonly data: T | null;
  /** The failure, in the API's own words, or null. */
  readonly failure: ApiFailure | null;
  /** A request is in flight. Not an error state, and not an empty one. */
  readonly busy: boolean;
}

/**
 * The state before anything has answered.
 *
 * Frozen and shared, so that the initial render of every page using this hook is the same
 * object. `data` and `failure` are both null at once here and never again: after a request
 * settles exactly one of them is set, which is what lets a caller branch on `failure` first
 * and treat the rest as an answer.
 */
const PENDING: Resource<never> = Object.freeze({ data: null, failure: null, busy: true });

export function useResource<T>(path: string): Resource<T> {
  const [answer, setAnswer] = useState<Resource<T>>(PENDING);

  useEffect(() => {
    let live = true;
    const controller = new AbortController();
    setAnswer(PENDING);

    void (async () => {
      const result = await request<T>(path, { signal: controller.signal });
      if (!live) {
        return;
      }
      setAnswer(
        result.ok
          ? { data: result.data, failure: null, busy: false }
          : { data: null, failure: result.failure, busy: false },
      );
    })();

    return () => {
      live = false;
      controller.abort();
    };
  }, [path]);

  return answer;
}
