/**
 * Routing and the shell: what each address renders, and which addresses are outside the
 * session guard.
 *
 * The same route table the browser router mounts is mounted here on a memory router, so
 * these tests ask the real table what an address resolves to rather than asking a copy.
 *
 * **The two sign-in routes sit outside the guard and both would be bugs inside it.** The
 * callback is where a session comes from, so guarding it is a loop; the signed-out page
 * exists precisely because there is no session, so guarding it would sign the person
 * straight back in and undo what they just did.
 *
 * **A page may arrive after its address does.** The records route is code-split, so mounting
 * it renders the shell's suspense fallback first and the heading a moment later. Every
 * heading is therefore waited for rather than read once: a test that read it synchronously
 * would find the empty string, and "the empty string is not the not-found page" is a check
 * that passes for a route that never resolves at all.
 *
 * Task ids: M32.5.1.2
 */

import { RouterProvider, createMemoryRouter } from "react-router-dom";
import { render, waitFor } from "@testing-library/react";
import { beforeAll, describe, expect, test } from "vitest";
import { NOT_FOUND_MESSAGE } from "../src/api/errors";
import { loadConsole, signIn, type LoadedConsole } from "./support/auth";

/**
 * Transform the split route once, before anything is timed. See the same note in
 * `tests/records-page.test.tsx`: the first mount of that chunk is the first time the form
 * and table libraries are transformed, which is seconds and is not what is being measured.
 */
beforeAll(async () => {
  await import("../src/pages/Records");
}, 60_000);

/** Mount the application's own route table at one address. */
async function mountAt(path: string): Promise<HTMLElement> {
  const { routes } = await import("../src/App");
  const router = createMemoryRouter(routes, { initialEntries: [path] });
  return render(<RouterProvider router={router} />).container;
}

function headingOf(container: HTMLElement): string {
  return container.querySelector("h1")?.textContent ?? "";
}

/** The heading an address settles on, once a split route's code has arrived. */
async function headingAt(path: string): Promise<string> {
  const container = await mountAt(path);
  await waitFor(() => {
    if (!container.querySelector("h1")) {
      throw new Error(`${path} rendered no heading`);
    }
  });
  return headingOf(container);
}

async function signedIn(): Promise<LoadedConsole> {
  const loaded = await loadConsole();
  await signIn(loaded);
  return loaded;
}

describe("addresses behind the guard", () => {
  test("the root address renders the overview", async () => {
    // What breaks if this is deleted: the index route. A shell with no index child renders
    // the frame and an empty main region, which reads as a page that failed to load rather
    // than as a missing route.
    await signedIn();
    expect(await headingAt("/")).toBe("Overview");
  });

  test("a deep link resolves to its page", async () => {
    // What breaks if this is deleted: the console only works if you navigate to everything
    // from the front page. Deep links are how people share an address with a colleague and
    // how the callback returns somebody to where they were, so a table that only matches
    // from the root breaks both.
    await signedIn();
    expect(await headingAt("/records")).toBe("Records");
  });

  test("a deep link carrying a path parameter resolves to its page", async () => {
    // What breaks if this is deleted: the address stops being the state. The entity is a
    // path segment on this route, so this is the case a person actually shares with a
    // colleague, and a table that matched only the bare section would send them to a screen
    // with no question asked on it and no sign that anything was dropped.
    await signedIn();
    expect(await headingAt("/records/clients")).toBe("Records");
  });

  test("every section in the navigation resolves to a page", async () => {
    // What breaks if this is deleted: a link in the menu that lands on the not-found page,
    // which is indistinguishable from a section a person may not see and is therefore the
    // one broken link in this console that nobody would report as a bug.
    await signedIn();
    const shell = await mountAt("/");
    const hrefs = [...shell.querySelectorAll("nav a")].map((link) => link.getAttribute("href"));
    expect(hrefs.length).toBeGreaterThan(1);

    for (const href of hrefs) {
      const heading = await headingAt(href ?? "/");
      expect(heading, `${String(href)} does not resolve`).not.toBe("No such page");
      // And it resolved to something rather than to nothing: a split route that never
      // finished loading would leave the heading empty, which is not the not-found page
      // either and would satisfy the line above for ever.
      expect(heading, `${String(href)} renders no heading`).not.toBe("");
    }
  });

  test("an unknown address renders the console's own not-found page", async () => {
    // What breaks if this is deleted: an address with no page renders the shell and
    // nothing else, or throws. Either way the person is looking at a screen that says
    // nothing, and the deployment question of whether unknown paths return index.html
    // becomes impossible to distinguish from a routing bug.
    await signedIn();
    expect(headingOf(await mountAt("/nothing-here"))).toBe("No such page");
  });

  test("the not-found page does not borrow the API's words", async () => {
    // What breaks if this is deleted: the two kinds of "not found" get one wording. This
    // page is about the console's own routes, which are the same list in every deployment
    // and disclose nothing. The API's 404 means either that a record does not exist or that
    // nothing the asker holds reaches it, deliberately indistinguishable. Using one message
    // for both would eventually teach somebody that they mean the same thing, and then that
    // an address they cannot reach is an address that does not exist.
    await signedIn();
    const container = await mountAt("/nothing-here");
    expect(container.textContent).not.toContain(NOT_FOUND_MESSAGE);
  });

  test("a page behind the guard starts a sign-in when there is no session", async () => {
    // What breaks if this is deleted: a person with no session sits on an empty shell while
    // every request it makes is refused. The guard is not protecting anything, it is making
    // sure a token exists before a page tries to use one; without it the console shows
    // refusals instead of a sign-in prompt.
    const loaded = await loadConsole();
    const container = await mountAt("/records");

    await waitFor(() => {
      expect(loaded.location.assign).toHaveBeenCalled();
    });
    expect(container.textContent).toContain("Signing you in");
    // And the page's own code was never asked for. The guard sits outside the suspense
    // boundary, so a person with no session does not download a screen they are not going
    // to be shown.
    expect(container.querySelector("h1")).toBeNull();
    expect(loaded.location.lastAssigned().searchParams.get("response_type")).toBe("code");
  });
});

describe("addresses outside the guard", () => {
  test("the callback address is not behind the session guard", async () => {
    // What breaks if this is deleted: a loop. The callback is where a session comes from,
    // so a guard in front of it sends the browser back to the identity provider to get the
    // session it is holding in the query string, for ever, until the attempt counter stops
    // it with a message about a redirect URI that is perfectly correct.
    const loaded = await loadConsole({ path: "/auth/callback" });
    const container = await mountAt("/auth/callback?code=X&state=Y");

    await waitFor(() => {
      expect(container.textContent).toContain("Could not sign you in");
    });
    expect(loaded.location.assign).not.toHaveBeenCalled();
    expect(loaded.idp.urls).toEqual([]);
  });

  test("the signed-out address is not behind the session guard", async () => {
    // What breaks if this is deleted: signing out signs you back in. The page exists
    // precisely because there is no session, so a guard in front of it starts a fresh
    // sign-in and undoes what the person just did, which on a shared machine is the whole
    // point of having done it.
    const loaded = await loadConsole({ path: "/signed-out" });
    const container = await mountAt("/signed-out");

    expect(container.textContent).toContain("Signed out");
    expect(loaded.location.assign).not.toHaveBeenCalled();
    expect(loaded.idp.urls).toEqual([]);
  });

  test("the signed-out page offers a way back and does not take it", async () => {
    // What breaks if this is deleted: an automatic sign-in on the signed-out page. Somebody
    // who has just signed out on a shared machine and is immediately signed back in has not
    // signed out, and by then the SSO cookie is gone anyway, so the automatic version puts
    // them on a login form they did not ask for.
    await loadConsole({ path: "/signed-out" });
    const container = await mountAt("/signed-out");

    const link = container.querySelector("a");
    expect(link?.getAttribute("href")).toBe("/");
  });
});
