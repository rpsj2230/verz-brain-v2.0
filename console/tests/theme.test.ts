/**
 * The theme, which has three states and not two.
 *
 * A person can choose light, choose dark, or choose nothing and follow the machine. The
 * third is the default, it is the one a two-position toggle has to lie about, and it is
 * expressed by the absence of an attribute rather than by a value: with nothing stamped on
 * the root element, only the `prefers-color-scheme` block applies and the page follows the
 * machine, including when the machine changes at sunset.
 *
 * The cascade itself cannot be exercised here. jsdom does not evaluate media queries and
 * does not compute a colour from a custom property, so "the page is dark" is not a thing
 * this suite can observe. What it can do is read the stylesheet's real rules and ask the
 * browser's own selector engine which of them a given root element matches, which is the
 * part of the cascade that the `:not([data-theme="light"])` guard decides.
 *
 * Task ids: M32.5.1.4
 */

import { describe, expect, test, vi } from "vitest";
import { customProperties, parseCss, type CssRule } from "./support/css";
import { readConsoleFile } from "./support/repo";

/** A copy of the theme module that has not yet read anything. */
async function freshTheme(): Promise<typeof import("../src/theme/theme")> {
  vi.resetModules();
  return await import("../src/theme/theme");
}

function tokenRules(): CssRule[] {
  return parseCss(readConsoleFile("src/theme/tokens.css"));
}

function onlyRule(rules: CssRule[], what: string): CssRule {
  if (rules.length !== 1) {
    throw new Error(`Expected exactly one ${what} in tokens.css, found ${rules.length}.`);
  }
  return rules[0] as CssRule;
}

function bareRoot(rules: CssRule[]): CssRule {
  return onlyRule(
    rules.filter((rule) => rule.atRule === "" && rule.selector === ":root"),
    "unconditional :root rule",
  );
}

function darkMediaRule(rules: CssRule[]): CssRule {
  return onlyRule(
    rules.filter((rule) => /prefers-color-scheme:\s*dark/.test(rule.atRule)),
    "rule inside the dark media query",
  );
}

function explicitDarkRule(rules: CssRule[]): CssRule {
  return onlyRule(
    rules.filter((rule) => rule.atRule === "" && rule.selector.includes('[data-theme="dark"]')),
    "unconditional rule for an explicit dark choice",
  );
}

describe("the theme preference", () => {
  test("the default state stamps no attribute on the root element", async () => {
    // What breaks if this is deleted: the third state. Stamping `data-theme="system"`
    // would match neither theme selector and leave the page in light values for ever on a
    // machine set to dark, which is the same bug with a friendlier attribute value. The
    // absence of the attribute is what "follow the machine" is made of.
    const theme = await freshTheme();
    theme.initialiseTheme();

    expect(theme.getTheme()).toBe("system");
    expect(document.documentElement.hasAttribute("data-theme")).toBe(false);
  });

  test("an explicit choice is stamped on the root element", async () => {
    // What breaks if this is deleted: the toggle. Without the attribute nothing in the
    // stylesheet can tell an explicit choice from the machine's, so choosing light on a
    // dark machine would do nothing at all.
    const theme = await freshTheme();

    theme.setTheme("dark");
    expect(document.documentElement.getAttribute("data-theme")).toBe("dark");

    theme.setTheme("light");
    expect(document.documentElement.getAttribute("data-theme")).toBe("light");
  });

  test("choosing to follow the machine again removes the stamp", async () => {
    // What breaks if this is deleted: the way back. A console that could be moved off
    // "system" but never back onto it has a one-way door in its settings, and the person
    // who wants their laptop's sunset switch again has to clear site data to get it.
    const theme = await freshTheme();

    theme.setTheme("dark");
    theme.setTheme("system");

    expect(theme.getTheme()).toBe("system");
    expect(document.documentElement.hasAttribute("data-theme")).toBe(false);
  });

  test("an unrecognised stored preference falls back to following the machine", async () => {
    // What breaks if this is deleted: a stale or tampered value in the store becomes an
    // attribute nothing matches, which is the permanent-light-mode bug again, arriving
    // through the one input to this module that a person can edit by hand.
    localStorage.setItem("brain.console.theme", "midnight");

    const theme = await freshTheme();
    theme.initialiseTheme();

    expect(theme.getTheme()).toBe("system");
    expect(document.documentElement.hasAttribute("data-theme")).toBe(false);
  });

  test("an explicit choice survives a reload", async () => {
    // What breaks if this is deleted: the preference stops being a preference. This is
    // also the path the blocking script in index.html reads before the first paint, so
    // losing it here is the flash of the wrong theme that script exists to prevent.
    const first = await freshTheme();
    first.setTheme("dark");

    const second = await freshTheme();
    second.initialiseTheme();

    expect(second.getTheme()).toBe("dark");
    expect(document.documentElement.getAttribute("data-theme")).toBe("dark");
  });

  test("the theme is the only thing this console writes to the local store", async () => {
    // What breaks if this is deleted: the one allowed use of the local store stops being
    // the only one. `scripts/check-boundaries.mjs` refuses the spelling `localStorage`
    // outside this module; this checks the observable half, which is that a whole theme
    // change leaves exactly one key behind and it is not a credential.
    const theme = await freshTheme();
    theme.setTheme("dark");

    const keys = Object.keys(localStorage);
    expect(keys).toEqual(["brain.console.theme"]);
    expect(localStorage.getItem("brain.console.theme")).toBe("dark");
  });
});

describe("the theme tokens", () => {
  test("every token is defined on the unstamped root", async () => {
    // What breaks if this is deleted: a colour whose only definition sits behind
    // `[data-theme="dark"]` or inside the dark media query. In the default state, where
    // nothing is stamped and the machine is light, that custom property resolves to
    // nothing and whatever uses it falls back to an inherited or initial value. The bug
    // shows as one unpainted element in the state most people are in, and it is invisible
    // to whoever added the token while testing in dark mode.
    const rules = tokenRules();
    const defined = new Set(Object.keys(customProperties(bareRoot(rules))));
    expect(defined.size).toBeGreaterThan(0);

    const missing: string[] = [];
    for (const rule of rules) {
      for (const property of Object.keys(customProperties(rule))) {
        if (!defined.has(property)) {
          missing.push(`${rule.atRule} ${rule.selector} { ${property} }`);
        }
      }
    }
    expect(missing).toEqual([]);
  });

  test("an explicit light choice wins on a machine set to dark", async () => {
    // What breaks if this is deleted: the `:not([data-theme="light"])` guard, which is one
    // token long and the easiest thing in the file to drop. Without it the dark media
    // query still applies to a root that has explicitly chosen light, so the control works
    // in one direction only: a person on a dark machine can select light and watch nothing
    // happen. The selector is read out of the stylesheet and handed to the browser's own
    // matcher, so this asserts on the rule rather than on the file containing the words.
    const root = document.documentElement;
    const selector = darkMediaRule(tokenRules()).selector;

    root.removeAttribute("data-theme");
    expect(root.matches(selector)).toBe(true);

    root.setAttribute("data-theme", "dark");
    expect(root.matches(selector)).toBe(true);

    root.setAttribute("data-theme", "light");
    expect(root.matches(selector)).toBe(false);
  });

  test("the explicit dark rule applies only to a root that chose dark", async () => {
    // What breaks if this is deleted: the other half of the toggle. A selector that
    // matched an unstamped root would make dark the default and take the machine's opinion
    // out of the design entirely, and a selector that matched a light choice would be the
    // mirror of the bug above.
    const root = document.documentElement;
    const selector = explicitDarkRule(tokenRules()).selector;

    root.setAttribute("data-theme", "dark");
    expect(root.matches(selector)).toBe(true);

    root.removeAttribute("data-theme");
    expect(root.matches(selector)).toBe(false);

    root.setAttribute("data-theme", "light");
    expect(root.matches(selector)).toBe(false);
  });

  test("each palette declares the colour scheme it actually is", async () => {
    // What breaks if this is deleted: `color-scheme` is what tells the browser to draw
    // form controls, scrollbars, the caret and the default canvas to match. It is not a
    // custom property, so nothing else in this file checks it, and a light palette that
    // declares `dark` gives a white page dark scrollbars and dark select menus. Every
    // colour would still be correct and the page would still look broken.
    const rules = tokenRules();
    expect(bareRoot(rules).declarations["color-scheme"]).toBe("light");
    expect(darkMediaRule(rules).declarations["color-scheme"]).toBe("dark");
    expect(explicitDarkRule(rules).declarations["color-scheme"]).toBe("dark");
  });

  test("the two dark blocks declare the same values", async () => {
    // What breaks if this is deleted: the duplication stops being safe. The dark values
    // are written twice on purpose, because `light-dark()` fails to no colour at all
    // rather than to a wrong one on a browser that does not know it. Two copies are only
    // acceptable while something notices when they drift, and a drift here means the theme
    // a person chose looks subtly different from the same theme their machine chose.
    const rules = tokenRules();
    expect(explicitDarkRule(rules).declarations).toEqual(darkMediaRule(rules).declarations);
  });

  test("no stylesheet outside the tokens file names a colour", async () => {
    // What breaks if this is deleted: the second palette. A component that reaches for a
    // literal instead of a token is invisible in whichever theme it was written in and
    // wrong in the other, and the lock is the worst place for that to happen because its
    // whole point is looking the same everywhere.
    const appCss = parseCss(readConsoleFile("src/styles/app.css"));
    const literals: string[] = [];
    for (const rule of appCss) {
      for (const [property, value] of Object.entries(rule.declarations)) {
        if (/#[0-9a-fA-F]{3,8}\b|\brgba?\(|\bhsla?\(|\bcolor-mix\(/.test(value)) {
          literals.push(`${rule.selector} { ${property}: ${value} }`);
        }
      }
    }
    expect(literals).toEqual([]);
  });
});
