/**
 * The entry point: what has to be true before anything renders.
 *
 * `src/main.tsx` is two lines of wiring and one contract with `index.html`, and neither is
 * exercised by any other test in this suite, because everything else mounts a component or
 * a route table directly. That makes it exactly the file where a deletion goes unnoticed.
 *
 * Task ids: M32.5.1.4
 */

import { afterEach, describe, expect, test, vi } from "vitest";
import { fakeIdentityProvider, ISSUER, stubLocation } from "./support/auth";

/**
 * Start the application the way the browser does: a `#root` element in the document, the
 * environment set, and `src/main.tsx` imported for its side effects.
 */
async function start(): Promise<void> {
  vi.resetModules();
  vi.stubEnv("VITE_KEYCLOAK_ISSUER", ISSUER);
  stubLocation("/");
  vi.stubGlobal("fetch", fakeIdentityProvider().fetch);
  await import("../src/main");
}

afterEach(() => {
  document.body.innerHTML = "";
});

describe("starting the console", () => {
  test("the stored theme is applied before anything renders", async () => {
    // What breaks if this is deleted: the module that owns the preference and the blocking
    // script in index.html end up with different ideas of what is selected. The script
    // exists to avoid the flash of the wrong theme and cannot import anything; this call is
    // what makes the module agree with it. When they already agree it costs nothing, which
    // is exactly why removing it looks safe.
    localStorage.setItem("brain.console.theme", "dark");
    document.documentElement.removeAttribute("data-theme");
    document.body.innerHTML = '<div id="root"></div>';

    await start();

    expect(document.documentElement.getAttribute("data-theme")).toBe("dark");
  });

  test("a missing root element fails with a sentence rather than a null reference", async () => {
    // What breaks if this is deleted: index.html and main.tsx are the two halves of one
    // contract, and when the element is gone React fails with a null reference from inside
    // a minified bundle. The person reading that has no reason to look at the HTML.
    document.body.innerHTML = "<p>no root here</p>";

    await expect(start()).rejects.toThrow(/index\.html/);
  });
});
