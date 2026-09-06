/**
 * What the console is allowed to say when a request does not succeed.
 *
 * **A 404 from this API is not "not found".** `brain.app.handle_brain_error` maps both
 * DENIED and ABSENT to 404 with the same body, deliberately, because a 403 on a hidden
 * record confirms that the record exists. The console has to keep that property, and the
 * way a console breaks it is never by writing "access denied": it is by being helpful.
 * "You may not have permission to view this" turns one status code back into two answers,
 * and it does it in the friendliest possible voice.
 *
 * So the rule here is negative and absolute: the console adds no interpretation to a
 * failure. It shows the message the API sent, and when there is none it shows the same
 * sentence the API would have sent, copied from `brain.core.errors`.
 *
 * **The trace id is the one useful thing to add.** Every response carries `x-trace-id`,
 * minted by the application before it knows who is asking, and quoting it is what makes a
 * support conversation short. It identifies a request, not a record, and it is safe to
 * show for the same reason the ledger can hold it and an answer cannot hold a count.
 */

/** Written down because "be helpful about a 404" is the most natural mistake here. */
export const A_404_IS_NOT_AN_EXPLANATION =
  "DENIED and ABSENT are both 404 with the same body. Any wording that distinguishes " +
  "them, including a sympathetic one about permissions, rebuilds the difference the API " +
  "spent a taxonomy removing. Show what the API said and nothing else.";

/**
 * The fallback for a 404 with no readable body. Copied verbatim from
 * `brain.core.errors.Denied.public_message`, which is identical to `Absent`'s, which is
 * the entire point of that pair.
 */
export const NOT_FOUND_MESSAGE = "I could not find that.";

/** Fallbacks for the other outcomes in the taxonomy, in the API's own words. */
const FALLBACK_MESSAGES: Readonly<Record<number, string>> = Object.freeze({
  404: NOT_FOUND_MESSAGE,
  409: "I found more than one match and could not tell which you meant.",
  503: "I could not reach one of the systems needed to answer that.",
});

const UNKNOWN_FAILURE = "Something went wrong.";

/** A request that did not succeed. A value, not an exception: see `client.ts`. */
export interface ApiFailure {
  /** The HTTP status, for code that must branch. Never rendered on its own. */
  readonly status: number;
  /** Safe to show a person, and already safe when it came from the API. */
  readonly message: string;
  /** From the `x-trace-id` response header, or the error body. May be empty. */
  readonly traceId: string;
  /**
   * The API's own vocabulary: denied, absent, unresolved, degraded, failed.
   *
   * For code that must branch, such as deciding whether retrying could help. **Never
   * rendered, and never used to choose wording.** The whole point of the taxonomy is that
   * a person cannot tell denied from absent, and a console that showed the word, or picked
   * a different sentence or a different colour from it, would hand back the distinction
   * the API removed. Today the application's own handler does not send this field at all;
   * the parsing is here because a middleware response does.
   */
  readonly outcome: string;
}

/** The shape `brain.api.ErrorBody` returns. Every failing response uses it. */
interface ErrorBody {
  message?: unknown;
  trace_id?: unknown;
  outcome?: unknown;
}

/**
 * Turn a failed response into something renderable, preferring what the API said.
 *
 * The API's message has already been through `to_public`, so it is the one string that is
 * known to be safe. The fallbacks exist only for a response that carried no body at all,
 * which is what a proxy returns when it never reached the application.
 */
export function failureFrom(response: Response, body: unknown): ApiFailure {
  // A cast at the boundary, where proving a structural match buys nothing: every field is
  // read back through a `typeof` check below, so the cast widens nothing that the reading
  // does not then narrow.
  const fields = (typeof body === "object" && body !== null ? body : {}) as ErrorBody;
  const message =
    typeof fields.message === "string" && fields.message.length > 0
      ? fields.message
      : (FALLBACK_MESSAGES[response.status] ?? UNKNOWN_FAILURE);
  const headerTrace = response.headers.get("x-trace-id") ?? "";
  return {
    status: response.status,
    message,
    traceId: headerTrace || (typeof fields.trace_id === "string" ? fields.trace_id : ""),
    outcome: typeof fields.outcome === "string" ? fields.outcome : "failed",
  };
}

/** A failure that never reached the API: no network, DNS, or a blocked request. */
export function transportFailure(error: unknown): ApiFailure {
  return {
    status: 0,
    message:
      error instanceof Error && error.name === "AbortError"
        ? "That request was cancelled."
        : "The console could not reach the Brain. Check your connection and try again.",
    traceId: "",
    outcome: "degraded",
  };
}
