/**
 * The navigation, which is the same for everybody.
 *
 * **This is the positive statement of the rule that the console does not decide what
 * exists.** The obvious alternative is to read the roles out of the token and show each
 * person only the sections they can use. That is one line, it works, and it puts a
 * permission model in the browser: the console would be deciding what exists, from a copy
 * of the rules nobody keeps in step, computed from a token this code has no business
 * reading. And a menu that shrinks is itself a disclosure: a person who can see six
 * sections and a person who can see four have learned something about each other.
 *
 * A section somebody cannot use answers with the API's own refusal, which is the same
 * answer they would get for a section that does not exist.
 *
 * Task ids: M32.5.1.2
 */

import { MemoryRouter } from "react-router-dom";
import { render } from "@testing-library/react";
import { describe, expect, test } from "vitest";
import { loadConsole, signIn, type LoadedConsole } from "./support/auth";

/** The navigation landmark's markup, from a shell rendered at one address. */
async function navigationAt(path: string): Promise<string> {
  const { Shell } = await import("../src/layout/Shell");
  const { container } = render(
    <MemoryRouter initialEntries={[path]}>
      <Shell />
    </MemoryRouter>,
  );
  const nav = container.querySelector("nav");
  if (!nav) {
    throw new Error("The shell rendered no navigation landmark, so there is nothing to compare.");
  }
  return nav.outerHTML;
}

/** Where the navigation points, ignoring which entry is marked as current. */
function targets(navMarkup: string): { href: string; label: string }[] {
  const holder = document.createElement("div");
  holder.innerHTML = navMarkup;
  return [...holder.querySelectorAll("a")].map((link) => ({
    href: link.getAttribute("href") ?? "",
    label: link.textContent ?? "",
  }));
}

describe("the navigation", () => {
  test("the navigation is identical for every session", async () => {
    // What breaks if this is deleted: the rule that this console does not decide what
    // exists. A filter here would need a token to be read, would be a second permission
    // model computed in the one place an attacker can edit, and would leak by omission:
    // the shape of the menu would tell each person what they are not allowed to see.
    const seen = new Set<string>();

    const noSession = await loadConsole();
    seen.add(await navigationAt("/"));
    expect(noSession.session.isSignedIn()).toBe(false);

    const first = await loadConsole();
    await signIn(first, { accessToken: "TOKEN-FOR-ONE-PERSON", idToken: "ID-ONE" });
    seen.add(await navigationAt("/"));

    const second = await loadConsole();
    await signIn(second, {
      accessToken: "A-DIFFERENT-TOKEN-ENTIRELY",
      idToken: "ID-TWO",
      expiresIn: 900,
    });
    seen.add(await navigationAt("/"));

    expect([...seen]).toHaveLength(1);
  });

  test("the navigation lists the same sections on every page", async () => {
    // What breaks if this is deleted: a section that appears only once you are already in
    // it, which is the same disclosure by a slower route. Where the links point is
    // compared rather than the whole markup, because the current entry is legitimately
    // marked and that mark is the one thing that should differ.
    await loadConsole();
    const onOverview = targets(await navigationAt("/"));
    const onActivity = targets(await navigationAt("/activity"));

    expect(onOverview.length).toBeGreaterThan(1);
    expect(onActivity).toEqual(onOverview);
  });

  test("the current section is marked by more than a colour", async () => {
    // What breaks if this is deleted: the only signal of where you are becomes a colour,
    // which is invisible to a screen reader and to anyone who cannot distinguish the two
    // shades. This is the accessibility half of the same list.
    await loadConsole();
    const holder = document.createElement("div");
    holder.innerHTML = await navigationAt("/activity");
    const current = [...holder.querySelectorAll("a")].filter(
      (link) => link.getAttribute("aria-current") === "page",
    );

    expect(current.map((link) => link.textContent)).toEqual(["Activity"]);
  });

  test("a section stays current at an address inside it", async () => {
    // What breaks if this is deleted: `end` on the section links. With exact matching
    // everywhere, opening anything nested under a section unmarks that section, so the
    // person is somewhere the menu says they are not. The root entry is the one that needs
    // exact matching, because a prefix match on "/" would otherwise mark it everywhere.
    await loadConsole();
    const holder = document.createElement("div");
    holder.innerHTML = await navigationAt("/activity/something-nested");
    const current = [...holder.querySelectorAll("a")].filter(
      (link) => link.getAttribute("aria-current") === "page",
    );

    expect(current.map((link) => link.textContent)).toEqual(["Activity"]);
  });

  test("the header says nothing about who is signed in", async () => {
    // What breaks if this is deleted: a name in the corner. The only way to know who is
    // signed in without asking the API is to read the token, which is the one thing this
    // console must never do. When an endpoint exists that says who the caller is, that is
    // where a name comes from.
    const loaded: LoadedConsole = await loadConsole();
    await signIn(loaded, {
      accessToken: "PERSON-SENTINEL-ACCESS",
      idToken: "PERSON-SENTINEL-ID",
      refreshToken: "PERSON-SENTINEL-REFRESH",
    });

    const { Shell } = await import("../src/layout/Shell");
    const { container } = render(
      <MemoryRouter initialEntries={["/"]}>
        <Shell />
      </MemoryRouter>,
    );

    expect(container.textContent).not.toContain("PERSON-SENTINEL");
    expect(container.innerHTML).not.toContain("PERSON-SENTINEL");
  });

  test("the shell offers a way past the navigation from the keyboard", async () => {
    // What breaks if this is deleted: reaching the page content from the keyboard means
    // tabbing through every navigation item on every page. The skip link is first in the
    // DOM on purpose, and being first is the part that a later reordering breaks.
    await loadConsole();
    const { Shell } = await import("../src/layout/Shell");
    const { container } = render(
      <MemoryRouter initialEntries={["/"]}>
        <Shell />
      </MemoryRouter>,
    );

    const firstFocusable = container.querySelector("a, button, input");
    expect(firstFocusable?.getAttribute("href")).toBe("#main");
    expect(container.querySelector("#main")?.tagName).toBe("MAIN");
  });
});
