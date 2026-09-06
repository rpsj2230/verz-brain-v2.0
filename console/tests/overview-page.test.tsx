/**
 * The overview, which is the console's only claim about who is signed in.
 *
 * **Every fact on it came from `GET /api/v1/me`.** The console holds an opaque token it
 * never decodes, so the alternative to asking is reading a claim, which is the one thing
 * `scripts/check-boundaries.mjs` refuses by name. The first test here is therefore about the
 * list of fields being the API's list and not a list somebody here remembered: it reads the
 * names off `brain.api_routes.CallerView` in the Python source, so a field added there and
 * not here fails rather than arriving and being dropped.
 *
 * **The page must add nothing.** Not a sentence about assurance, not a lock over a null, not
 * an explanation of a 404. The route's 404 means the token authenticated and its subject maps
 * to no principal this company wrote down, and saying so is the console explaining a refusal
 * it did not observe.
 *
 * Task ids: M32.5.1.1, M32.5.1.2
 */

import { render, waitFor } from "@testing-library/react";
import { describe, expect, test } from "vitest";
import { CALLER_FIELDS, ME_PATH } from "../src/pages/Overview";
import { fakeIdentityProvider, loadConsole, signIn } from "./support/auth";
import { backendCallerViewFields, backendPublicMessages } from "./support/python";

/** A caller whose every field is its own sentinel, so a dropped one cannot hide. */
const A_CALLER = {
  principal_id: "PRINCIPAL-SENTINEL",
  display_name: "DISPLAY-SENTINEL",
  primary_department: "DEPARTMENT-SENTINEL",
  employment: "EMPLOYMENT-SENTINEL",
  assurance: "ASSURANCE-SENTINEL",
  channel: "CHANNEL-SENTINEL",
  ent_hash: "ENTHASH-SENTINEL",
};

interface Answer {
  readonly status?: number;
  readonly body: unknown;
  readonly traceId?: string;
}

/** Mount the overview against a stand-in `/api/v1/me`, and wait for it to settle. */
async function overviewAnswering(answer: Answer): Promise<HTMLElement> {
  const idp = fakeIdentityProvider({
    api(url) {
      if (!url.endsWith(`/api/v1${ME_PATH}`)) {
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
  const { Overview } = await import("../src/pages/Overview");
  const { container } = render(<Overview />);
  await waitFor(() => {
    if (container.querySelector(".note[role='status']")) {
      throw new Error("still loading");
    }
  });
  return container;
}

/** The value shown beside one label, or null when the row is not on the page. */
function valueBeside(container: HTMLElement, label: string): string | null {
  for (const row of container.querySelectorAll(".fields__row")) {
    if (row.querySelector("dt")?.textContent === label) {
      return row.querySelector("dd")?.textContent ?? "";
    }
  }
  return null;
}

describe("what the overview shows", () => {
  test("the fields it renders are the fields the API declares", async () => {
    // What breaks if this is deleted: a fact the API sends about the caller arrives and is
    // silently dropped. The names are read out of the Python model rather than listed here,
    // so this is not the console's list compared with itself: adding a field to CallerView
    // fails this test, which is the only moment anybody will decide what to do with it.
    expect(CALLER_FIELDS.map((field) => field.name).sort()).toEqual(
      backendCallerViewFields().sort(),
    );
  });

  test("every fact the API sent about the caller reaches the screen", async () => {
    // What breaks if this is deleted: the list above becomes decoration. A page could
    // declare seven fields and render three, and the structural test would still pass. Each
    // value is its own sentinel so that a dropped one cannot be covered by another.
    const container = await overviewAnswering({ body: A_CALLER });
    for (const value of Object.values(A_CALLER)) {
      expect(container.textContent, `${value} is not on the page`).toContain(value);
    }
  });

  test("a value is rendered as the API spelled it and gains no sentence", async () => {
    // What breaks if this is deleted: the console starts interpreting. `assurance` is on the
    // response because it is the one fact a person can act on, which makes it exactly the
    // value somebody will wrap in "sign in again with your second factor". That sentence is
    // a mapping from a value to a meaning, written in a browser, out of step with the API
    // within a release. The cell is compared as a whole, so an addition beside the word
    // fails as loudly as a replacement of it.
    const container = await overviewAnswering({ body: A_CALLER });
    expect(valueBeside(container, "Assurance")).toBe(A_CALLER.assurance);
    expect(valueBeside(container, "Channel")).toBe(A_CALLER.channel);
  });

  test("a caller with no department has no row where one would be", async () => {
    // What breaks if this is deleted: an empty row, which is a shape where a fact would be.
    // `primary_department` is nullable and a caller can legitimately have none, so absence
    // has to contribute nothing at all: no label, no gap, no dash. Two people comparing
    // screens can read a shape as easily as they can read a value.
    const container = await overviewAnswering({
      body: { ...A_CALLER, primary_department: null },
    });
    expect(valueBeside(container, "Department")).toBeNull();
    expect(valueBeside(container, "Employment")).toBe(A_CALLER.employment);
  });

  test("nothing on this page renders a lock", async () => {
    // What breaks if this is deleted: an invented refusal. A lock says the API told us a
    // field exists and withheld it, and `/me` sends no `locked` at all, so a lock here could
    // only have been derived from a null. That would be the console asserting a refusal
    // nobody made, in the one appearance that is supposed to mean something exact.
    const container = await overviewAnswering({
      body: { ...A_CALLER, primary_department: null },
    });
    expect(container.querySelector(".lock")).toBeNull();
  });
});

describe("when the API does not answer", () => {
  test("a failure is shown in the API's own words with nothing added", async () => {
    // What breaks if this is deleted: the console starts speaking for the API about a 404,
    // which on this route means the token was accepted and its subject maps to no principal
    // this company wrote down. The sentence is read out of the Python source, so this is not
    // the console's copy compared with itself, and the assertion is that exactly that
    // sentence and nothing else reaches the screen.
    const sentence = backendPublicMessages()["DENIED"];
    expect(sentence).toBeTruthy();

    const container = await overviewAnswering({
      status: 404,
      body: { message: sentence, trace_id: "" },
      traceId: "trace-overview",
    });

    expect(container.querySelector(".notice__body")?.textContent).toBe(sentence);
    expect(container.querySelector(".notice__trace")?.textContent).toContain("trace-overview");
    // And no half-page of fields beside it: two answers to one question is the beginning of
    // a reader working out which one is the real one.
    expect(container.querySelector(".fields")).toBeNull();
  });

  test("a page still waiting is not reported as a caller with no facts", async () => {
    // What breaks if this is deleted: the overview flashes an empty field list before its
    // first answer arrives. That is a statement about somebody's identity made before
    // anybody asked, and on a slow connection it is the screen they remember.
    const idp = fakeIdentityProvider({
      api() {
        return new Response(JSON.stringify(A_CALLER), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      },
    });
    const loaded = await loadConsole({ idp });
    await signIn(loaded);
    const { Overview } = await import("../src/pages/Overview");
    const { container } = render(<Overview />);

    expect(container.querySelector(".fields")).toBeNull();
    expect(container.querySelector(".notice")).toBeNull();
    expect(container.querySelector(".note[role='status']")).not.toBeNull();

    await waitFor(() => {
      expect(container.querySelector(".fields")).not.toBeNull();
    });
  });
});
