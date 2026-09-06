/**
 * Turning a schema the API sent into the form this console is willing to render. No React.
 *
 * The split is the one `paging.ts` and `useServerPage.ts` make, and it is the same split the
 * Python side keeps between a module that holds a rule and a module that holds a client: this
 * module decides what a generated form may contain, `SchemaForm.tsx` renders it. The reason
 * is the case that is always wrong. What a form does with a field it may not show cannot be
 * tested through a component that mounts a form library, and it is the only part of a
 * generated form that has to be right.
 *
 * **A generated form is a form nobody read before it rendered.** That is the whole of what is
 * different here. Every other screen in this console was written by somebody who looked at it;
 * this one is assembled from a document the API sent, so every rule about what may reach a
 * person has to be a property of the assembly rather than a habit of whoever laid the screen
 * out. Three rules follow, and they are the three functions below.
 *
 * **A withheld field renders through `ui/Lock.tsx` and through nothing else.** `Lock` takes no
 * props, `brain.core.redaction.render_lock` takes no arguments, and a form is the third place
 * this rule is rebuilt by accident, after the stylesheet and the grid. The natural spellings
 * here are all wrong in the same way: a disabled input, a read-only input, a placeholder
 * reading "restricted", a tooltip saying why, an `aria-describedby` pointing at an
 * explanation. Each of them varies with the reason, and the reason is the part that
 * discloses: "out of scope" says the field exists on records in another department.
 * `LockedField` in the Python model carries `entity`, `record_id` and `field` and no reason,
 * so the reader below keeps two names per entry and there is no value for a renderer to
 * reach for.
 *
 * **A field the API did not send is absent, and absence leaves no shape.** This is the half a
 * form gets wrong that a grid does not, because a form has a layout: a gap where a field was,
 * a numbered legend, a disabled control holding a place, a `required` entry naming a property
 * that is not there. The rule is that a field the schema does not carry contributes nothing:
 * no element, no id, no entry in the tab order and no arithmetic. Two callers whose schemas
 * differ by one property must get two forms that differ by that property and by nothing else.
 *
 * **A withheld field cannot fail validation and cannot be written back.** The second is the
 * one that damages data rather than disclosing it. `Form` hands its caller a complete
 * `formData` object on submit, and a locked property that is in the schema and has no value
 * arrives in it as an absent or defaulted key. Sending that to an API that writes what it is
 * given replaces a value this caller was never allowed to read with an empty one. So the
 * names are stripped on the way out, by `withoutWithheld`, rather than by remembering not to
 * send them. The first matters less and is still real: a `required` entry for a field nobody
 * can fill makes the form unsubmittable for one person and submittable for another, which is
 * a difference two people comparing screens can read.
 *
 * Rejected: computing the locked set in the browser from anything about the caller. The
 * console is not a trust boundary and holds no fact about entitlement; `locked` arrives from
 * the API with the record, the same way it does for a grid. Rejected also: hiding a locked
 * field entirely, which sounds safer and is not, because the field's existence was already
 * disclosed by the API putting it in the schema, and hiding it would leave the person
 * wondering why their form is shorter than a colleague's with no way to ask.
 *
 * Task ids: M32.5.2.2
 */

import type { RJSFSchema, UiSchema } from "@rjsf/utils";

/**
 * Written down because "grey it out and explain why" is the change that will look like an
 * improvement in a pull request, and because it is the same change three times over.
 */
export const A_WITHHELD_FIELD_IS_A_LOCK_AND_NOTHING_ELSE =
  "A field this caller may not see renders the lock, which takes no props and has one " +
  "appearance. Not a disabled input, not a read-only one, not a placeholder, not a tooltip " +
  "and not a described-by pointing at a reason. The reason a field was withheld is the part " +
  "that discloses, and render_lock takes no arguments so that no path exists from a reason " +
  "to a rendering. A form is the third place this rule gets rebuilt, after the stylesheet " +
  "and the grid.";

/**
 * Written down because the failure it prevents is silent, and is a write rather than a read.
 */
export const A_FORM_NEVER_SENDS_A_FIELD_IT_COULD_NOT_SHOW =
  "The value of a withheld field is stripped from the submitted data rather than trusted to " +
  "be absent. A form library hands back a whole object, and a locked property with no value " +
  "arrives in it as an empty or defaulted key. Sending that to an endpoint that writes what " +
  "it is given replaces a figure this caller was never permitted to read with nothing, and " +
  "nothing in the response says so.";

/**
 * Written down because a generated form is the one screen whose fields nobody chose.
 *
 * `scripts/check-boundaries.mjs` refuses a password input in this console's own source. A
 * form assembled from a payload is the way round that check, because the field type comes
 * from the document rather than from a file anybody greps.
 */
export const A_GENERATED_FORM_COLLECTS_NO_CREDENTIAL =
  "A property whose format asks for a secret is dropped from a generated form rather than " +
  "rendered. The console has no way to verify it is entitled to collect a credential, the " +
  "realm disables the direct grant for the same reason, and a schema is a document from a " +
  "system of record rather than a decision anybody here reviewed. Rendering it as an " +
  "ordinary text box instead would be worse: the secret would be on the screen.";

/**
 * The format this console will not build an input for.
 *
 * One string, matched exactly, because the point is not to guess at every schema that might
 * carry a secret. It is to stop the one spelling that produces a credential input from a
 * document. Anything else needing this treatment is a decision for whoever adds it.
 */
export const CREDENTIAL_FORMAT = "password";

/**
 * The name a locked property's field is registered under in the form's field registry.
 *
 * A name rather than a component reference, so that this module stays free of React and the
 * only module that can supply the component is the one that renders the form. A `ui:field`
 * naming something that is not registered renders the fallback field, which is loud.
 */
export const LOCK_FIELD = "brainWithheldField";

/** What a form was given and what it may do with it. Everything else is derived from this. */
export interface FormShape {
  /** The schema as the form will see it: locked fields still present, credentials gone. */
  readonly schema: RJSFSchema;
  /** The ui schema, with every locked property pointed at the lock field. */
  readonly uiSchema: UiSchema;
  /**
   * The property names whose values this form must never send back: the ones it locked and
   * the ones it refused to build an input for. Never rendered, and never counted.
   */
  readonly withheld: ReadonlySet<string>;
}

/** A schema that was not a schema. A bug in the console or the API, never an answer. */
export class UnreadableSchema extends Error {}

/** The shape a schema's `properties` map has, named once so it is not spelled out twice. */
type SchemaProperties = NonNullable<RJSFSchema["properties"]>;

/** The top-level properties of an object schema, or an empty map. */
function propertiesOf(schema: RJSFSchema): SchemaProperties {
  const properties = schema.properties;
  if (typeof properties !== "object" || properties === null || Array.isArray(properties)) {
    return {};
  }
  return properties;
}

/** Whether a property asks for a credential. Absent format, or any other, is false. */
function isCredential(property: unknown): boolean {
  if (typeof property !== "object" || property === null) {
    return false;
  }
  return (property as { format?: unknown }).format === CREDENTIAL_FORMAT;
}

/**
 * The fields withheld from one record, as names.
 *
 * **Everything except the record id and the field name is dropped**, for the reason
 * `lockedCellsFrom` in `paging.ts` gives at length: `brain.core.redaction.LockedField` carries
 * `entity`, `record_id` and `field` and deliberately carries no reason, and a payload that
 * grew one must find nothing here to carry it. The two readers are checked against each other
 * rather than sharing an implementation, because a grid needs a key per cell and a form needs
 * a name per field, and one function returning both shapes would be the place they drift.
 *
 * An entry for another record is skipped. A form shows one record, and locking a field on it
 * because a different record withheld that field would say something true about the other
 * record to somebody looking at this one.
 */
export function lockedFieldsFor(value: unknown, recordId: string): ReadonlySet<string> {
  const names = new Set<string>();
  if (!Array.isArray(value)) {
    return names;
  }
  for (const entry of value) {
    if (typeof entry !== "object" || entry === null) {
      continue;
    }
    const { record_id: entryId, field } = entry as { record_id?: unknown; field?: unknown };
    if (entryId === recordId && typeof field === "string") {
      names.add(field);
    }
  }
  return names;
}

/**
 * What a form may render, given the schema the API sent and the fields it withheld.
 *
 * A locked name that the schema does not carry is ignored rather than rendered. There is no
 * title, no type and no position for it, so the alternative is a lock captioned with a raw
 * identifier, which is the console inventing a label for a field it was never told about.
 *
 * A locked name is removed from `required` and stays in `properties`, which is the pair that
 * makes the lock renderable and the form submittable. A credential property is removed from
 * both, because there is nothing left to render.
 */
export function formShape(
  schema: RJSFSchema,
  locked: ReadonlySet<string> = new Set(),
  uiSchema: UiSchema = {},
): FormShape {
  if (typeof schema !== "object" || schema === null || Array.isArray(schema)) {
    throw new UnreadableSchema("A form schema must be an object.");
  }

  const properties = propertiesOf(schema);
  const kept: SchemaProperties = {};
  const withheld = new Set<string>();
  const lockedHere = new Set<string>();

  for (const [name, property] of Object.entries(properties)) {
    if (isCredential(property)) {
      withheld.add(name);
      continue;
    }
    kept[name] = property;
    if (locked.has(name)) {
      lockedHere.add(name);
      withheld.add(name);
    }
  }

  const effective: RJSFSchema = { ...schema, properties: kept };
  if (Array.isArray(schema.required)) {
    // Rebuilt rather than left alone. A required entry naming a property that is no longer
    // there makes the form permanently invalid with no field to point the failure at, and a
    // required entry naming a locked one makes it invalid for the person who cannot fill it
    // and valid for the person who can, which is the difference two screens would show.
    effective.required = schema.required.filter(
      (name) => name in kept && !lockedHere.has(name),
    );
  }

  const effectiveUi: UiSchema = { ...uiSchema };
  for (const name of lockedHere) {
    // The whole field is replaced rather than only its widget. A widget leaves the library's
    // own field template around it, which is where the description, the help text, the error
    // slot and the described-by come from, and every one of those is a place a reason could
    // be shown next to a lock.
    const stated: unknown = effectiveUi[name];
    effectiveUi[name] = {
      ...(typeof stated === "object" && stated !== null ? stated : {}),
      "ui:field": LOCK_FIELD,
    };
  }

  return { schema: effective, uiSchema: effectiveUi, withheld };
}

/**
 * The submitted data with the withheld names removed.
 *
 * A copy rather than a delete in place, because the caller's object is the form's state and
 * mutating it would empty the field on the screen as a side effect of submitting.
 */
export function withoutWithheld<T>(data: T, withheld: ReadonlySet<string>): T {
  if (typeof data !== "object" || data === null || Array.isArray(data)) {
    return data;
  }
  const kept: Record<string, unknown> = {};
  for (const [name, value] of Object.entries(data as Record<string, unknown>)) {
    if (!withheld.has(name)) {
      kept[name] = value;
    }
  }
  return kept as T;
}
