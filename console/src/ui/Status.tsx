/**
 * The one place a value is turned into a colour.
 *
 * Every other component takes its tone as a literal. This one reads a state word out of a
 * payload and looks it up, which is exactly the step that has to be concentrated in a
 * single readable file rather than spread across call sites. A reviewer asking "what can
 * make something red in this console" reads `STATE_TONES` below and is finished.
 *
 * **The table is closed, and unknown means neutral.** A word this console has never heard
 * of renders in the quietest tone with its own spelling intact. Two things follow, and both
 * are the point:
 *
 * - The console cannot invent an alarm. A source that started sending `"compromised"` would
 *   render as a plain grey label saying `compromised`, which is the truth, rather than as a
 *   red one, which would be the console asserting a severity nobody told it.
 * - The console cannot invent a reassurance either, and that is the direction that matters
 *   more. Falling back to "positive" would paint every unrecognised state green, so an API
 *   that grew a state meaning "this connector is leaking" would show as healthy until
 *   somebody noticed.
 *
 * **The word is rendered exactly as it arrived.** No mapping table of prettier names, no
 * sentence built around it. The API owns the vocabulary; capitalisation is done in CSS, so
 * the text in the DOM, in a copied screenshot and in a screen reader is the API's own word.
 *
 * **The vocabulary is `brain.connectors.contract.HealthState` and is checked against it.**
 * Those four are the only state vocabulary the API has today, and the test reads the enum
 * out of the Python source: a fifth member added there fails here rather than quietly
 * rendering neutral for ever. The test also reads `brain.core.redaction.RedactionReason` and
 * the outcomes that `handle_brain_error` collapses onto one status code, and asserts that
 * none of them is a key of this table. That second half is the one that matters:
 * `denied: "critical"` is a one-line change that would let two people comparing screens
 * learn which of them was refused, and it would look like completeness.
 *
 * The tone for `unconfigured` is neutral rather than caution, and the argument is the
 * Python enum's own: a connector nobody finished installing is a task for whoever installed
 * it, not an incident, and collapsing it into the amber band produces a dashboard that is
 * permanently amber during a rollout and is therefore permanently ignored.
 *
 * Task ids: M32.5.2.4
 */

import { Badge } from "./Badge";
import { DEFAULT_TONE, type Tone } from "./tone";

/**
 * Every state word this console recognises, and how loudly it says it.
 *
 * Frozen because it is a rule rather than a default: a module that mutated it at runtime
 * could give one viewer a different colour from another, which is the variation this whole
 * file exists to make impossible.
 */
export const STATE_TONES: Readonly<Record<string, Tone>> = Object.freeze({
  ok: "positive",
  degraded: "caution",
  down: "critical",
  unconfigured: "neutral",
});

/** The tone for a state word, and the quietest tone for anything unrecognised. */
export function toneFor(state: string): Tone {
  return STATE_TONES[state] ?? DEFAULT_TONE;
}

interface StatusProps {
  /**
   * The state as the API spelled it. A plain `string` rather than a union of the four
   * known words, deliberately: this value arrives from a JSON body, no type in a browser
   * can constrain what a server sent, and a signature that claimed otherwise would move
   * the unknown case from a fallback into a lie.
   */
  readonly state: string;
}

export function Status({ state }: StatusProps) {
  return <Badge label={state} tone={toneFor(state)} />;
}
