/**
 * What the console is allowed to say when a request does not succeed.
 *
 * **A 404 from this API is not "not found".** `brain.app.handle_brain_error` maps DENIED
 * and ABSENT to the same status with the same body, deliberately, because a 403 on a hidden
 * record confirms that the record exists. The way a console breaks that is never by writing
 * "access denied"; it is by being helpful. "You may not have permission to view this" turns
 * one status code back into two answers, in the friendliest possible voice, and it is the
 * most natural mistake anybody will make in this directory.
 *
 * The fallback sentences are read out of the Python source rather than compared with
 * themselves, so a drift in either direction fails here.
 */

import { render } from "@testing-library/react";
import { describe, expect, test } from "vitest";
import {
  NOT_FOUND_MESSAGE,
  failureFrom,
  transportFailure,
  type ApiFailure,
} from "../src/api/errors";
import { Notice } from "../src/ui/Notice";
import { backendOutcomeStatuses, backendPublicMessages } from "./support/python";

function response(status: number, headers: Record<string, string> = {}): Response {
  return new Response(null, { status, headers });
}

/** The rendered text of a failure, as a person would read it off the screen. */
function screenText(failure: ApiFailure, title = "That did not work"): string {
  const { container } = render(
    <Notice title={title} traceId={failure.traceId}>
      <p>{failure.message}</p>
    </Notice>,
  );
  return container.textContent ?? "";
}

describe("the fallback messages", () => {
  test("every fallback is the API's own sentence for that status", () => {
    // What breaks if this is deleted: the console starts speaking for the API. These
    // sentences exist for a response that carried no body at all, which is what a proxy
    // returns when it never reached the application, and a console that invented its own
    // wording there would be describing an outcome it did not observe. Both the status
    // table and the sentences are read out of the Python source, so this is not a constant
    // compared with itself.
    const statuses = backendOutcomeStatuses();
    const messages = backendPublicMessages();
    expect(Object.keys(statuses).length).toBeGreaterThan(0);

    for (const [outcome, status] of Object.entries(statuses)) {
      const expected = messages[outcome];
      expect(expected, `no public message parsed for ${outcome}`).toBeDefined();
      expect(failureFrom(response(status), null).message).toBe(expected);
    }
  });

  test("denied and absent share one sentence", () => {
    // What breaks if this is deleted: the property everything else here depends on. If the
    // two outcomes ever stopped sharing a message, the console's single fallback for 404
    // would be picking one of two answers, and a reader could tell which.
    const statuses = backendOutcomeStatuses();
    const messages = backendPublicMessages();

    expect(statuses["DENIED"]).toBe(statuses["ABSENT"]);
    expect(messages["DENIED"]).toBe(messages["ABSENT"]);
    expect(NOT_FOUND_MESSAGE).toBe(messages["DENIED"]);
  });
});

describe("a failed request", () => {
  test("the message the API sent is shown unchanged", () => {
    // What breaks if this is deleted: a prefix, a suffix or a rewrite. The API's message
    // has already been through `to_public`, which is the only function allowed to produce a
    // sentence for a person; anything wrapped around it here is a second voice describing
    // an outcome this console did not decide.
    const sent = "I could not find that.";
    expect(failureFrom(response(404), { message: sent }).message).toBe(sent);
  });

  test("nothing is added to a 404 on the way to the screen", () => {
    // What breaks if this is deleted: the sympathetic wording. This asserts the rendered
    // text is exactly the title and the message with nothing between or after them, so a
    // helpful sentence added anywhere in the path fails rather than reads well.
    const failure = failureFrom(response(404), { message: NOT_FOUND_MESSAGE });
    expect(screenText(failure, "No answer")).toBe(`No answer${NOT_FOUND_MESSAGE}`);
  });

  test("denied and absent are indistinguishable on the screen", () => {
    // What breaks if this is deleted: the taxonomy is rebuilt in the browser. The outcome
    // field exists for code that must branch, such as deciding whether a retry could help,
    // and the moment it chooses a word, a colour or an icon, two people comparing screens
    // can tell a refusal from an absence.
    const denied: ApiFailure = {
      status: 404,
      message: NOT_FOUND_MESSAGE,
      traceId: "t-1",
      outcome: "denied",
    };
    const absent: ApiFailure = { ...denied, outcome: "absent" };

    expect(screenText(absent)).toBe(screenText(denied));
    expect(screenText(denied)).not.toContain("denied");
    expect(screenText(absent)).not.toContain("absent");
  });

  test("a failure carries no wording of its own about permissions", () => {
    // What breaks if this is deleted: the exact sentence this whole design exists to keep
    // out. "You may not have permission to view this" is a correct-sounding, kind, and
    // completely disclosing thing for a console to say, and it is one commit away at all
    // times.
    const rendered = screenText(failureFrom(response(404), null)).toLowerCase();
    for (const word of ["permission", "denied", "forbidden", "not allowed", "access", "authoris"]) {
      expect(rendered).not.toContain(word);
    }
  });

  test("a notice waits its turn rather than interrupting", () => {
    // What breaks if this is deleted: `role="alert"` interrupts a screen reader mid
    // sentence, which is right for a fire alarm and wrong for "I could not find that". It
    // is a one-word change that nothing else in this suite would notice.
    const { container } = render(<Notice title="No answer">A message.</Notice>);
    expect(container.firstElementChild?.getAttribute("role")).toBe("status");
  });

  test("a notice has one appearance and no severity variants", () => {
    // What breaks if this is deleted: the moment a notice can look different, something
    // has to decide which look a given message gets, and the only fact available to decide
    // with is the API's outcome. A 404 that looked different from a 503 would be harmless;
    // a 404 that looked different depending on which kind of 404 it was would not, and the
    // two changes are one line apart.
    const { container } = render(<Notice title="No answer">A message.</Notice>);
    expect(container.firstElementChild?.getAttribute("class")).toBe("notice");
  });

  test("the trace id comes from the response header", () => {
    // What breaks if this is deleted: the one useful thing a person can quote in a support
    // conversation. It identifies a request rather than a record, which is why it is safe
    // to show at all, and it is minted before the application knows who is asking.
    const failure = failureFrom(response(503, { "x-trace-id": "trace-abc" }), null);
    expect(failure.traceId).toBe("trace-abc");
    expect(screenText(failure)).toContain("trace-abc");
  });

  test("a trace id in the body is used when the header carried none", () => {
    // What breaks if this is deleted: a response shaped by the application's own error body
    // rather than by the middleware loses its reference, and the support conversation
    // becomes "roughly when did this happen".
    expect(failureFrom(response(500), { trace_id: "from-body" }).traceId).toBe("from-body");
  });

  test("a request that never reached the API says so and nothing more", () => {
    // What breaks if this is deleted: a network failure starts being reported as an answer.
    // "I could not find that" for a dropped connection is a statement about the company's
    // data that nothing observed.
    const failure = transportFailure(new TypeError("Failed to fetch"));
    expect(failure.status).toBe(0);
    expect(failure.traceId).toBe("");
    expect(failure.message).not.toBe(NOT_FOUND_MESSAGE);
  });
});
