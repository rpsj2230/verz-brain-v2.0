/**
 * The only place this console talks to the API.
 *
 * One function, so that the token is attached in one place, failures are shaped in one
 * place, and a reviewer asking "what can this console fetch, and what does it do with a
 * refusal" reads one file. `scripts/check-boundaries.mjs` refuses a `fetch` anywhere else
 * in `src` outside the sign-in flow, because the second call site is always the one that
 * forgets something.
 *
 * **A failure is a value, not an exception.** `request` resolves to a result, and callers
 * branch on it. Throwing invites a component-level catch that renders a red banner saying
 * "error", and the most common failure in this system is a 404 that is a legitimate answer
 * to a legitimate question. A 404 is not an exception; it is what "nothing you hold
 * reaches that" looks like from outside.
 *
 * **Nothing here decides what may be fetched.** There is no allow-list of paths, no check
 * of anything about the caller, no branch that asks whether this person should be asking.
 * The API decides, per request, from grants this browser cannot see. A console that
 * filtered its own requests would be a second permission model, and the copy in the
 * browser is the copy an attacker edits. See `THE_CONSOLE_IS_NOT_A_TRUST_BOUNDARY`.
 *
 * **The response type is supplied by the caller from the generated schema**, rather than
 * derived from the path by conditional types. It is the smaller claim: the shapes come
 * from the API's own document, and the link between a path and its shape is written at the
 * call site where a reviewer can see it. Rejected: a generated runtime client. Those bring
 * their own base URL handling, their own retries and their own opinion about what a status
 * code means, and the opinion this system needs about 404 is not one any generator holds.
 * If the call sites ever outgrow this, `openapi-fetch` types the pair together and is the
 * next step, not a rewrite.
 */

import { config } from "../config";
import { accessToken, forgetSession } from "../auth/session";
import { failureFrom, transportFailure, type ApiFailure } from "./errors";

export type ApiResult<T> =
  | { readonly ok: true; readonly data: T }
  | { readonly ok: false; readonly failure: ApiFailure };

export interface RequestOptions {
  /**
   * A closed set rather than `string`, so a call site cannot invent a verb. PATCH is here
   * because `PATCH /api/v1/routing/rungs/{rung_id}` is the first write this console makes;
   * DELETE is deliberately absent, because nothing in this system hard-deletes and a verb
   * with no route behind it is a verb somebody eventually points at one.
   */
  readonly method?: "GET" | "POST" | "PATCH";
  readonly body?: unknown;
  readonly signal?: AbortSignal;
}

/**
 * A path under the API base, always beginning with a slash, for example `/health/ready`.
 * Joined rather than concatenated blindly so that a base of `/api/v1` and a base of
 * `https://api.example.com/api/v1` behave the same way.
 */
function urlFor(path: string): string {
  const base = config.apiBaseUrl.endsWith("/")
    ? config.apiBaseUrl.slice(0, -1)
    : config.apiBaseUrl;
  return `${base}${path}`;
}

export async function request<T>(
  path: string,
  options: RequestOptions = {},
): Promise<ApiResult<T>> {
  const token = await accessToken();
  const headers: Record<string, string> = { accept: "application/json" };
  if (token) {
    headers["authorization"] = `Bearer ${token}`;
  }
  if (options.body !== undefined) {
    headers["content-type"] = "application/json";
  }

  let response: Response;
  try {
    response = await fetch(urlFor(path), {
      method: options.method ?? "GET",
      headers,
      // The console authenticates with a bearer token and nothing else. Omitting ambient
      // credentials means a request cannot be made meaningful by a cookie that happened to
      // be in the browser, which is the whole shape of a cross-site request forgery.
      credentials: "omit",
      ...(options.body === undefined ? {} : { body: JSON.stringify(options.body) }),
      ...(options.signal ? { signal: options.signal } : {}),
    });
  } catch (error) {
    return { ok: false, failure: transportFailure(error) };
  }

  if (response.status === 401) {
    // The API refused a token this console believed was current. The console does not
    // guess why: the session may have ended elsewhere, the token may be minted for an
    // audience the API does not accept, the clock may be wrong. Forgetting it lets the
    // guard start a fresh sign-in, and the attempt counter turns a permanent cause into a
    // readable message rather than a redirect loop.
    forgetSession();
  }

  const payload: unknown = await response.json().catch(() => null);
  if (!response.ok) {
    return { ok: false, failure: failureFrom(response, payload) };
  }
  // The one unchecked step, and it is the boundary this function exists to be: the shape
  // was described by the API's own document and the caller named the type from it. Nothing
  // here can prove the two agree, and a runtime validator would be a third description of
  // the same schema. A body that is not what the caller expected, including the `null` from
  // a response with no content, arrives as a wrong-shaped value rather than as an error.
  return { ok: true, data: payload as T };
}
