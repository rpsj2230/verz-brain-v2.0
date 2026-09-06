/**
 * The facts this console copies from the Python side, read back out of the Python side.
 *
 * Three constants in `src/` are copies: the lock's text, the fallback sentences for a
 * failed request, and the statuses those sentences belong to. There is no shared artefact
 * between a Python package and a browser bundle to carry any of them, so the only honest
 * check is to read the original. `console/scripts/export-openapi.py` does this for the
 * lock text, but only when somebody regenerates the schema; these tests do it every run.
 *
 * The parsing is deliberately narrow and throws when it finds nothing. A tolerant parser
 * that returned an empty map would turn every assertion built on it into a comparison
 * between two empty things.
 */

import { extractOne, readRepoFile } from "./repo";

const REDACTION = "src/brain/core/redaction.py";
const ERRORS = "src/brain/core/errors.py";
const APP = "src/brain/app.py";

/**
 * What `brain.core.redaction.render_lock` returns, taken from its only definition.
 *
 * Anchored on the whole annotated assignment rather than on the word, so that renaming or
 * retyping the constant fails here instead of matching a mention of it in a docstring.
 */
export function backendLockText(): string {
  return extractOne(
    readRepoFile(REDACTION),
    /^LOCK_TEXT: Final = "([^"]*)"$/m,
    "LOCK_TEXT in brain.core.redaction",
  );
}

/**
 * Every outcome in the taxonomy and the sentence a person is allowed to be told for it.
 *
 * `Failed` and the base class carry the default rather than restating it, so the class
 * bodies are read in order and each unstated value inherits the last one declared on the
 * base. That mirrors what Python does and is why this is not a single regular expression.
 */
export function backendPublicMessages(): Record<string, string> {
  const source = readRepoFile(ERRORS);
  const classPattern = /^class\s+(\w+)\(([^)]*)\):/gm;
  const bodies: { name: string; base: string; body: string }[] = [];

  let match = classPattern.exec(source);
  while (match !== null) {
    const start = match.index + match[0].length;
    const next = classPattern.exec(source);
    bodies.push({
      name: match[1] ?? "",
      base: (match[2] ?? "").trim(),
      body: source.slice(start, next ? next.index : source.length),
    });
    match = next;
  }
  if (bodies.length === 0) {
    throw new Error(`No class definitions found in ${ERRORS}; the parser has gone stale.`);
  }

  const declaredOutcome: Record<string, string> = {};
  const declaredMessage: Record<string, string> = {};
  const baseOf: Record<string, string> = {};
  for (const entry of bodies) {
    baseOf[entry.name] = entry.base;
    const outcome = /^\s{4}outcome(?::\s*Outcome)?\s*=\s*Outcome\.(\w+)$/m.exec(entry.body);
    const message = /^\s{4}public_message(?::\s*str)?\s*=\s*"([^"]*)"$/m.exec(entry.body);
    if (outcome?.[1]) {
      declaredOutcome[entry.name] = outcome[1];
    }
    if (message?.[1] !== undefined) {
      declaredMessage[entry.name] = message[1];
    }
  }

  function resolve(table: Record<string, string>, name: string): string | undefined {
    let current: string | undefined = name;
    while (current) {
      const value = table[current];
      if (value !== undefined) {
        return value;
      }
      current = baseOf[current];
    }
    return undefined;
  }

  const messages: Record<string, string> = {};
  for (const entry of bodies) {
    const outcome = resolve(declaredOutcome, entry.name);
    const message = resolve(declaredMessage, entry.name);
    if (outcome && message !== undefined) {
      messages[outcome] = message;
    }
  }
  if (Object.keys(messages).length === 0) {
    throw new Error(`No outcome messages parsed from ${ERRORS}; the parser has gone stale.`);
  }
  return messages;
}

/**
 * The status each outcome leaves the application as, read from the one handler that
 * decides it. DENIED and ABSENT both being 404 is the property the console has to keep,
 * and it is a fact about this table rather than about anything in the console.
 */
export function backendOutcomeStatuses(): Record<string, number> {
  const source = readRepoFile(APP);
  const handler = source.slice(source.indexOf("async def handle_brain_error"));
  const table = /status = \{([\s\S]*?)\}\[exc\.outcome\]/.exec(handler);
  if (!table?.[1]) {
    throw new Error(`No outcome-to-status table found in ${APP}; the parser has gone stale.`);
  }
  const statuses: Record<string, number> = {};
  for (const entry of table[1].matchAll(/Outcome\.(\w+):\s*(\d+)/g)) {
    if (entry[1] && entry[2]) {
      statuses[entry[1]] = Number(entry[2]);
    }
  }
  if (Object.keys(statuses).length === 0) {
    throw new Error(`Parsed an empty status table from ${APP}; the parser has gone stale.`);
  }
  return statuses;
}
