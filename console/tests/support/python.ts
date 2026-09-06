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
const CONNECTOR_CONTRACT = "src/brain/connectors/contract.py";

/**
 * The members of one `enum.StrEnum`, member name to value, read from the Python source.
 *
 * Narrow on purpose. It reads the class body up to the next top-level definition and
 * matches whole assignments at one level of indentation, so a member written any other way
 * is not silently skipped: the caller gets a short map and the assertion built on it fails,
 * which is the loud direction. An empty result throws for the same reason every other
 * parser in this directory does.
 */
export function backendEnumMembers(
  modulePath: string,
  className: string,
): Record<string, string> {
  const source = readRepoFile(modulePath);
  const opened = new RegExp(`^class\\s+${className}\\(enum\\.StrEnum\\):$`, "m").exec(source);
  if (!opened) {
    throw new Error(
      `No StrEnum named ${className} in ${modulePath}. It has been renamed or restyled, ` +
        "so anything checked against it is no longer being checked.",
    );
  }
  const after = source.slice(opened.index + opened[0].length);
  const ends = /^(?:class\s|def\s|@\w)/m.exec(after);
  const body = ends ? after.slice(0, ends.index) : after;
  const members: Record<string, string> = {};
  for (const entry of body.matchAll(/^ {4}([A-Z][A-Z0-9_]*) = "([^"]*)"$/gm)) {
    if (entry[1] && entry[2] !== undefined) {
      members[entry[1]] = entry[2];
    }
  }
  if (Object.keys(members).length === 0) {
    throw new Error(`Parsed no members from ${className} in ${modulePath}; parser is stale.`);
  }
  return members;
}

/**
 * The declared field names of one pydantic model or dataclass, in source order.
 *
 * Only annotated assignments at one level of indentation count, so `model_config`, a
 * `__post_init__` and any method on the class are skipped without a special case. As with the
 * enum reader, an empty result throws: a model whose fields cannot be found makes every
 * comparison against it a comparison between two empty lists.
 *
 * The base-class list is optional in the pattern because a frozen dataclass declares none.
 * `brain.ops.tracing.Span` is written that way and is the one thing a trace graph's node
 * reader has to be checked against.
 */
export function backendModelFields(modulePath: string, className: string): string[] {
  const source = readRepoFile(modulePath);
  const opened = new RegExp(
    `^class\\s+${className}(?:\\[[^\\]]*\\])?(?:\\([^)]*\\))?:$`,
    "m",
  ).exec(source);
  if (!opened) {
    throw new Error(
      `No model named ${className} in ${modulePath}. It has moved or changed shape, so the ` +
        "console's copy of it is no longer being checked against anything.",
    );
  }
  const after = source.slice(opened.index + opened[0].length);
  const ends = /^(?:class\s|def\s|@\w)/m.exec(after);
  const body = ends ? after.slice(0, ends.index) : after;
  const fields = [...body.matchAll(/^ {4}(\w+):\s+[^=\n]+/gm)].map((entry) => entry[1] ?? "");
  if (fields.length === 0) {
    throw new Error(`Parsed no fields from ${className} in ${modulePath}; parser is stale.`);
  }
  return fields;
}

/**
 * The bounds the row plane puts on a page size, from the module that enforces them.
 *
 * The console's query form offers a number, and a form that offers a number the route
 * refuses spends a round trip producing `HTTPValidationError`, which is not `ErrorBody` and
 * therefore reaches a person as the least useful sentence this console has. Read from
 * `brain.knowledge.rows` rather than from the console's own constants, because two copies of
 * a bound compared with each other are green for every value they could hold.
 */
export function backendRowLimits(): { max: number; fallback: number } {
  const source = readRepoFile("src/brain/knowledge/rows.py");
  return {
    max: Number(extractOne(source, /^MAX_ROW_LIMIT: Final = (\d+)$/m, "MAX_ROW_LIMIT")),
    fallback: Number(
      extractOne(source, /^DEFAULT_ROW_LIMIT: Final = (\d+)$/m, "DEFAULT_ROW_LIMIT"),
    ),
  };
}

/** The caller's own facts, as `brain.api_routes.CallerView` declares them. */
export function backendCallerViewFields(): string[] {
  return backendModelFields("src/brain/api_routes.py", "CallerView");
}

/** The envelope every list endpoint returns. `brain.api.Page`. */
export function backendPageFields(): string[] {
  return backendModelFields("src/brain/api.py", "Page");
}

/** A field withheld from a record the caller may otherwise see. Carries no reason. */
export function backendLockedFieldFields(): string[] {
  return backendModelFields(REDACTION, "LockedField");
}

/**
 * One unit of trace as the API records it. `brain.ops.tracing.Span`.
 *
 * Read for the two fields a graph must never carry: `payload_in` and `payload_out` are the
 * question and the answer, they are the two the module refuses to allowlist, and they are
 * masked before a span leaves the process that made it. A console that drew them on a canvas
 * would put the masked question on a screen; a console written against an older shape would
 * quietly draw the unmasked one.
 */
export function backendSpanFields(): string[] {
  return backendModelFields("src/brain/ops/tracing.py", "Span");
}

/**
 * Why a field was withheld, in the API's own words. Never shown to anybody, which is the
 * whole reason the console needs to be able to prove it does not render any of them.
 */
export function backendRedactionReasons(): string[] {
  return Object.values(backendEnumMembers(REDACTION, "RedactionReason"));
}

/** What a connector's last probe found. The only state vocabulary the API has today. */
export function backendHealthStates(): string[] {
  return Object.values(backendEnumMembers(CONNECTOR_CONTRACT, "HealthState"));
}

/**
 * The outcomes the API deliberately makes indistinguishable, derived rather than listed.
 *
 * Two outcomes that leave `handle_brain_error` with the same status code are two answers
 * the caller is not allowed to tell apart, and today that is DENIED and ABSENT. Deriving
 * the pair from the status table means the console's rule follows the API's if the table
 * ever changes, and it means this list is not a copy of anything a reader here decided.
 */
export function backendCollapsedOutcomes(): string[] {
  const statuses = backendOutcomeStatuses();
  const values = backendEnumMembers(ERRORS, "Outcome");
  const perStatus = new Map<number, string[]>();
  for (const [name, status] of Object.entries(statuses)) {
    perStatus.set(status, [...(perStatus.get(status) ?? []), name]);
  }
  const collapsed: string[] = [];
  for (const names of perStatus.values()) {
    if (names.length < 2) {
      continue;
    }
    for (const name of names) {
      const value = values[name];
      if (value === undefined) {
        throw new Error(`Outcome.${name} is in the status table and not in the enum.`);
      }
      collapsed.push(value);
    }
  }
  if (collapsed.length === 0) {
    throw new Error(
      "No two outcomes share a status code, so DENIED and ABSENT are now distinguishable " +
        "to a client. That is either a change to the taxonomy or a stale parser here, and " +
        "either way it is not something to pass over quietly.",
    );
  }
  return collapsed;
}

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
