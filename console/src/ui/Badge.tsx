/**
 * A compact label with a tone. The visual atom, and nothing else.
 *
 * **It decides nothing.** It takes a word and one of four tones and renders them. It does
 * not look anything up, does not read a payload, and does not know what a state means. The
 * deciding is in `Status`, which is the only module allowed to turn a value into a tone,
 * and keeping the two apart is the same split the Python side makes between a module that
 * holds a rule and a module that holds a client: a component that both chose and rendered
 * a colour would be the place a reviewer stops being able to see which values can reach
 * which appearance.
 *
 * **The tone is written as a literal at every call site except one.** `tone={ok ? "positive"
 * : "critical"}` is the shape to refuse in review, because the condition is where a fact
 * about the reader gets turned into a colour, and `tests/status-primitives.test.tsx` parses
 * every component in `src` and fails on any `tone` attribute that is not a string literal,
 * allowing `ui/Status.tsx` and nothing else. That is the console's version of the rule that
 * `render_lock` takes no arguments: the property is checked by reading the call, not by
 * trusting the caller.
 *
 * **The word is always rendered.** A badge that was only a coloured dot would put the whole
 * meaning in the colour, which is unreadable to about one man in twelve, invisible on a
 * monochrome screen and silent to a screen reader. The colour is the second channel here,
 * never the first, in the same way the current navigation item carries an underline.
 *
 * Task ids: M32.5.2.4
 */

import { toneClass, type Tone } from "./tone";

interface BadgeProps {
  /** The word a person reads. The badge is legible with every colour removed. */
  readonly label: string;
  /**
   * How loudly to say it. Optional, and neutral when unstated, so that the quiet case
   * needs no decision at all and a call site that forgot cannot end up shouting.
   */
  readonly tone?: Tone;
}

export function Badge({ label, tone = "neutral" }: BadgeProps) {
  return <span className={`badge ${toneClass(tone)}`}>{label}</span>;
}
