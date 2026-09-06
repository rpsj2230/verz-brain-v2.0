/**
 * What every test file starts from, and why it has to be reset rather than assumed.
 *
 * Three things in this console live outside React and therefore outside a component's
 * lifetime: the theme's attribute on the root element, the two browser stores, and the
 * modules that hold a session or a discovery promise in a module-level variable. The first
 * two are reset here. The third cannot be reset from outside, so the tests that touch it
 * call `vi.resetModules()` and import again; see `tests/support/auth.ts`.
 *
 * Automatic cleanup from `@testing-library/react` only registers itself when the test
 * globals are present, and this project runs with `globals: false` so that every helper a
 * test uses is imported where a reader can see it. The `cleanup` call below is the price
 * of that and is not optional: without it every render stays in the document, and a test
 * that asserts on "the navigation" finds three of them.
 */

import { afterEach } from "vitest";
import { cleanup } from "@testing-library/react";

afterEach(() => {
  cleanup();
  document.documentElement.removeAttribute("data-theme");
  try {
    localStorage.clear();
    sessionStorage.clear();
  } catch {
    // A store that refuses to be read is a state the console handles; it is not a state a
    // test needs to reproduce here.
  }
});
