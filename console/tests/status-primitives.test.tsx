/**
 * The shared chip, badge and status primitives, and the leak they are the natural place for.
 *
 * `ui/Lock.tsx` takes no props, and `brain.core.redaction.render_lock` takes no arguments,
 * so that a withheld field cannot look different for different people, fields,
 * classifications or reasons: two people comparing screens must not be able to read off
 * which of them was refused and why. A set of coloured labels is where that rule gets
 * rebuilt by somebody who is not trying to break anything. `<Badge tone="critical"
 * label="Out of scope" />` discloses exactly what `.lock--out-of-scope` would have
 * disclosed, and it arrives in a pull request about making the console clearer.
 *
 * These tests hold two mechanisms rather than one convention. The vocabulary a tone can be
 * chosen from is closed, and it is checked against the words the API refuses to explain,
 * read out of the Python source rather than listed here. And a tone is never computed from
 * data outside the single module that owns the lookup table, which is checked by parsing
 * every component in `src` rather than by searching the files for text: `Badge.tsx` and
 * `Status.tsx` both write out the forbidden line in prose in order to forbid it, so a
 * substring search would find it in the explanation and pass with the real thing written
 * below it.
 *
 * Task ids: M32.5.2.4
 */

import { render } from "@testing-library/react";
import { describe, expect, test } from "vitest";
import { Badge } from "../src/ui/Badge";
import { Chip } from "../src/ui/Chip";
import { STATE_TONES, Status, toneFor } from "../src/ui/Status";
import { TONES, toneClass, type Tone } from "../src/ui/tone";
import { customProperties, parseCss, type CssRule } from "./support/css";
import {
  backendCollapsedOutcomes,
  backendHealthStates,
  backendRedactionReasons,
} from "./support/python";
import { readConsoleFile } from "./support/repo";
import {
  colourLiteralsIn,
  consoleSourcePaths,
  jsxAttributeUses,
  parseConsoleSource,
  propNamesOf,
} from "./support/typescript";

/** The one module allowed to turn a value into a colour. Everywhere else writes a literal. */
const TONE_LOOKUP_LIVES_HERE = "src/ui/Status.tsx";

function appRules(): CssRule[] {
  return parseCss(readConsoleFile("src/styles/app.css"));
}

function tokenRules(): CssRule[] {
  return parseCss(readConsoleFile("src/theme/tokens.css"));
}

/** Selectors in the application stylesheet that mention a class, whatever else they carry. */
function selectorsMentioning(className: string): string[] {
  return appRules()
    .map((rule) => rule.selector)
    .filter((selector) => selector.includes(className));
}

/** How loud a tone is, as its position in the declared order. */
function loudness(tone: Tone): number {
  return TONES.indexOf(tone);
}

describe("what a primitive may be told", () => {
  test("no primitive accepts a reason, a classification or a viewer", () => {
    // What breaks if this is deleted: the mechanism that keeps these components from
    // becoming the lock with a colour. `Lock` holds its property by having an empty
    // signature, which cannot vary by anything; these three do take props, so the property
    // has to be the exact list. Asserting the whole list rather than the absence of a few
    // bad names is deliberate: `reason` is only the spelling somebody would use today, and
    // `withheldBecause`, `policy` or `sensitivity` would each be the same disclosure. Any
    // addition fails here and has to be argued for in this file, which is the review the
    // rule needs.
    expect(propNamesOf(parseConsoleSource("src/ui/Chip.tsx"), "Chip")).toEqual(["label"]);
    expect(propNamesOf(parseConsoleSource("src/ui/Badge.tsx"), "Badge")).toEqual([
      "label",
      "tone",
    ]);
    expect(propNamesOf(parseConsoleSource(TONE_LOOKUP_LIVES_HERE), "Status")).toEqual([
      "state",
    ]);
  });

  test("a tone is never computed from data outside the module that owns the table", () => {
    // What breaks if this is deleted: the leak arrives at a call site rather than in a
    // component. `tone={row.classification === "restricted" ? "critical" : "neutral"}` is
    // one line, it reads as a nice touch, and it turns a fact about what a person may see
    // into a colour on their screen that somebody else's screen does not have. Concentrating
    // every lookup in one module means a reviewer asking "what can make something red" reads
    // one table. The check parses each file, so the example written out in `Badge.tsx`'s own
    // docstring in order to forbid it does not satisfy it.
    const computed: string[] = [];
    for (const path of consoleSourcePaths("src")) {
      if (path === TONE_LOOKUP_LIVES_HERE) {
        continue;
      }
      for (const use of jsxAttributeUses(parseConsoleSource(path), "tone")) {
        if (!use.isStringLiteral) {
          computed.push(`${path}: tone=${use.text}`);
        }
      }
    }
    expect(computed).toEqual([]);
  });

  test("the module that owns the table is the one that computes a tone", () => {
    // What breaks if this is deleted: the rule above is satisfied by a console with no
    // tones in it at all. A guard tested only by its refusals is satisfied by a function
    // that refuses everything, so this is the sibling: the allowed module really does
    // choose a tone from a value, and the allow-list is load-bearing rather than a leftover.
    const uses = jsxAttributeUses(parseConsoleSource(TONE_LOOKUP_LIVES_HERE), "tone");
    expect(uses).toHaveLength(1);
    expect(uses[0]?.isStringLiteral).toBe(false);
  });
});

describe("the state vocabulary", () => {
  test("no state the console colours is a word the API refuses to explain", () => {
    // What breaks if this is deleted: `denied: "critical"` in the tone table. It is a
    // one-line change, it looks like completeness, and it hands back the distinction the
    // whole error taxonomy exists to remove: a refusal and an absence are the same status
    // and the same body, so a colour that told them apart would be the side channel written
    // in CSS. The forbidden words are read out of the Python enums, and the collapsed pair
    // is derived from the handler's own status table rather than listed here, so this
    // follows the API if the taxonomy ever changes.
    const forbidden = new Set([...backendRedactionReasons(), ...backendCollapsedOutcomes()]);
    expect(forbidden.size).toBeGreaterThan(0);
    expect([...forbidden]).toContain("denied");

    const offending = Object.keys(STATE_TONES).filter((state) => forbidden.has(state));
    expect(offending).toEqual([]);

    // And the tone names themselves, which are the other half of the same door: a tone
    // called "denied" would need no state at all to leak, because a call site could write
    // it as a literal and satisfy every other rule here.
    expect(TONES.filter((tone) => forbidden.has(tone))).toEqual([]);
  });

  test("every state the API can report has a tone of its own", () => {
    // What breaks if this is deleted: a state added to `HealthState` in Python renders in
    // the fallback tone for ever, silently. The fallback is safe, which is exactly why
    // nobody would notice: a new state meaning "this connector is leaking" would show as a
    // quiet grey label and look deliberate. Reading the enum out of the Python source is
    // what makes this fail on the day the API grows a word, rather than on the day somebody
    // remembers to look.
    const states = backendHealthStates();
    expect(states.length).toBeGreaterThan(0);

    const untoned = states.filter((state) => !(state in STATE_TONES));
    expect(untoned).toEqual([]);
  });

  test("the states the API keeps apart are kept apart on the screen", () => {
    // What breaks if this is deleted: two states collapse into one appearance. The Python
    // enum argues this itself: `unconfigured` is separate from `down` because they go to
    // different people, a connector nobody finished installing is a task and one that was
    // working this morning is an incident, and collapsing them produces a dashboard that is
    // permanently amber during a rollout and therefore permanently ignored. A tone table
    // that painted them the same would undo that decision in the one place a person looks.
    const tones = backendHealthStates().map((state) => toneFor(state));
    expect(new Set(tones).size).toBe(backendHealthStates().length);
  });

  test("an unfinished connector is quieter than a failing one", () => {
    // What breaks if this is deleted: the ordering that makes the tones mean anything.
    // Asserting `toneFor("down") === "critical"` would pin a name; asserting that down is
    // louder than degraded and that unconfigured is quieter than both states the property
    // the choice was made for, and it survives a rename of the tones. Without it the table
    // could be shuffled and every other test here would still pass.
    expect(loudness(toneFor("down"))).toBeGreaterThan(loudness(toneFor("degraded")));
    expect(loudness(toneFor("unconfigured"))).toBeLessThan(loudness(toneFor("degraded")));
  });

  test("an unrecognised state keeps its own spelling and gets the quietest tone", () => {
    // What breaks if this is deleted: the console starts asserting a severity nobody told
    // it. A word the table has never heard of must render as the word, in the quietest
    // tone: falling back to anything louder invents an alarm, and falling back to positive
    // would be worse, because an API that grew a state meaning "this is leaking" would show
    // as healthy until somebody noticed.
    const { container } = render(<Status state="unheard-of" />);
    expect(container.textContent).toBe("unheard-of");
    // The class is written out rather than built with `toneClass`, which is part of what is
    // being checked: a helper that returned the same string for every tone would satisfy an
    // assertion phrased in terms of itself and would paint the whole console one colour.
    expect(container.querySelector(".badge")?.getAttribute("class")).toBe(
      "badge tone--neutral",
    );
  });

  test("a state the table knows is rendered with its tone and its own word", () => {
    // What breaks if this is deleted: every test above is satisfied by a component that
    // renders nothing and colours nothing. This is the positive sibling, and it also holds
    // the rule that the API's word reaches the DOM unchanged: prettifying `ok` into
    // "Healthy" here would mean a screenshot and a support conversation quote a word the
    // API never used. Capitalising is done in the stylesheet, where it changes no text.
    const { container } = render(<Status state="degraded" />);
    const badge = container.querySelector(".badge");
    expect(badge?.textContent).toBe("degraded");
    expect(badge?.getAttribute("class")).toBe("badge tone--caution");
  });
});

describe("how a chip and a badge look", () => {
  test("a chip renders the value it was given and carries no tone", () => {
    // What breaks if this is deleted: the split these three components exist for. A chip's
    // text is a value out of a record, so the moment its appearance could depend on that
    // text the console is holding an opinion about data it did not produce, and the first
    // opinion anybody writes is red for the restricted ones.
    const { container } = render(<Chip label="Finance" />);
    const chip = container.querySelector(".chip");
    expect(chip?.textContent).toBe("Finance");
    expect(chip?.getAttribute("class")).toBe("chip");
  });

  test("a badge is readable with every colour removed", () => {
    // What breaks if this is deleted: the meaning moves entirely into the colour. About one
    // man in twelve cannot rely on a hue, a printed or monochrome screen has none, and a
    // screen reader announces no colour at all. The word is the first channel here and the
    // tone is the second, which is the same decision the navigation makes with an underline.
    const { container } = render(<Badge label="down" tone="critical" />);
    const badge = container.querySelector(".badge") as HTMLElement;
    expect(badge.textContent).toBe("down");
    expect(badge.getAttributeNames()).toEqual(["class"]);
    expect(badge.children).toHaveLength(0);
  });

  test("a badge with no tone stated is the quiet one", () => {
    // What breaks if this is deleted: an unstated tone becomes whatever the first branch
    // returns. The default has to be the tone that asserts nothing, so that a call site
    // which forgot cannot end up shouting about a state nobody classified.
    const { container } = render(<Badge label="anything" />);
    expect(container.querySelector(".badge")?.getAttribute("class")).toBe(
      "badge tone--neutral",
    );
  });

  test("the stylesheet gives a badge exactly one rule per tone and no modifiers", () => {
    // What breaks if this is deleted: the leak written in CSS, one component along from the
    // lock. `.badge--denied` or `.chip--out-of-scope` would say what the reason says, and
    // the stylesheet names both spellings in a comment in order to forbid them, so this
    // parses the file into rules instead of searching it. Every declared tone must have a
    // rule and every rule must be a declared tone: a tone with no rule renders unpainted,
    // and a rule with no tone is a modifier that something can reach for.
    expect(selectorsMentioning(".badge").sort()).toEqual([".badge", ".chip, .badge"]);
    expect(selectorsMentioning(".chip").sort()).toEqual([".chip", ".chip, .badge"]);
    expect(selectorsMentioning(".tone--").sort()).toEqual(
      [...TONES].map((tone) => `.${toneClass(tone)}`).sort(),
    );
  });

  test("a tone takes every colour from a token and never from the accent", () => {
    // What breaks if this is deleted: the second palette, and the collision with the accent.
    // A literal in a tone rule is invisible in whichever theme it was written in and wrong
    // in the other. Reusing the accent hue is the subtler failure: the accent says "this is
    // the thing you can click", so a healthy connector painted with it looks like a link,
    // and the one clickable thing on the page stops being distinguishable from a label.
    for (const rule of appRules()) {
      if (!rule.selector.startsWith(".tone--")) {
        continue;
      }
      for (const [property, value] of Object.entries(rule.declarations)) {
        expect(value, `${rule.selector} { ${property} }`).toMatch(/^var\(--[a-z-]+\)$/);
      }
    }

    // And the values behind those tokens, in all three blocks of the token file: the media
    // query, the explicit dark choice and the unstamped root each declare their own copy.
    for (const rule of tokenRules()) {
      const declared = customProperties(rule);
      const accent = declared["--accent"];
      if (accent === undefined) {
        continue;
      }
      const clashes = Object.entries(declared)
        .filter(([property, value]) => property.startsWith("--tone-") && value === accent)
        .map(([property]) => `${rule.atRule} ${rule.selector} { ${property}: ${accent} }`);
      expect(clashes).toEqual([]);
    }
  });

  test("no component in the shared layers writes a colour", () => {
    // What breaks if this is deleted: the theme tests read stylesheets, so a colour written
    // in TypeScript is invisible to them. `style={{ color: "#b00020" }}` inside a cell
    // renderer would be a second palette in the one place that is hardest to find, and it
    // would be wrong in whichever theme its author was not using. Only string contents are
    // read, never comments, so the token names quoted in these files' explanations do not
    // count as colours appearing.
    const literals: string[] = [];
    for (const directory of ["src/ui", "src/components"]) {
      for (const path of consoleSourcePaths(directory)) {
        literals.push(...colourLiteralsIn(parseConsoleSource(path)));
      }
    }
    expect(literals).toEqual([]);
  });
});
