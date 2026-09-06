/**
 * The four ways a state may be coloured, and the one thing colour may never mean here.
 *
 * **A tone says how urgent a state is, never who was allowed to see it.** That sentence is
 * the whole reason this module is separate from the components that use it. The console
 * already has one appearance that must not vary, `ui/Lock.tsx`, and it holds that property
 * by taking no props at all. A set of coloured labels is the obvious place for the same
 * leak to reappear in a new spelling: `<Badge tone="critical" label="Out of scope" />`
 * discloses exactly what `.lock--out-of-scope` would have disclosed, and it looks like a
 * design decision rather than a disclosure.
 *
 * Two mechanisms keep that shut, and neither is a convention:
 *
 * 1. **The vocabulary is closed and short.** Four tones, listed here, with no way to add a
 *    fifth without editing this file and the stylesheet and the test that reads both.
 *    `tests/status-primitives.test.tsx` reads `RedactionReason` and the collapsed outcomes
 *    out of the Python source and asserts that nothing in the console's state vocabulary
 *    is one of them, so the check is against the real enums rather than against a list
 *    somebody remembered to keep in step.
 * 2. **A tone is never computed from data at a call site.** `ui/Status.tsx` is the only
 *    module allowed to choose a tone from a value, and it does so through a frozen table
 *    of states it knows. Everywhere else a tone is written as a literal or not written at
 *    all, and a test parses every component in `src` to hold that.
 *
 * The tone names are about the health of a thing. "Critical" means somebody should look at
 * it now; it does not mean the reader is not allowed to. Nothing in this system knows,
 * inside a browser, what a person is allowed to see, and a colour computed from a guess at
 * that is a permission model with a palette.
 *
 * Colour is never the only channel. A badge always renders its word, so the state is
 * readable with the colour removed, on a monochrome screen, and to a screen reader. The
 * navigation makes the same choice with an underline, for the same reason.
 *
 * Task ids: M32.5.2.4
 */

/**
 * Written down because "add a tone for refused" is the change that would look reasonable
 * in a pull request and would be the whole leak.
 */
export const A_TONE_IS_NEVER_A_REASON =
  "A tone says how urgent a state is and never who was permitted to see it. A fifth tone " +
  "meaning refused, withheld, restricted or out of scope would rebuild the side channel " +
  "that render_lock takes no arguments to prevent: two people comparing screens could " +
  "read the colour and learn which of them was refused, and for what. A withheld field " +
  "renders through Lock, which takes no props and has one appearance.";

/**
 * Every tone there is. Ordered from quietest to loudest, which is also the order the
 * stylesheet declares them in, so a reader comparing the two files reads down both.
 */
export const TONES = ["neutral", "positive", "caution", "critical"] as const;

export type Tone = (typeof TONES)[number];

/** The default for anything the console does not recognise. See `Status`. */
export const DEFAULT_TONE: Tone = "neutral";

/**
 * The class that carries a tone's colour. One class per tone, named after it, so the
 * stylesheet and this module cannot drift into a state where a tone exists with no rule.
 * The colours themselves are tokens in `theme/tokens.css` and appear nowhere else.
 */
export function toneClass(tone: Tone): string {
  return `tone--${tone}`;
}
