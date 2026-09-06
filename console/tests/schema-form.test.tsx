/**
 * Generated forms: what a schema may build, and the three things a form must not do.
 *
 * A generated form is the one screen in this console that nobody looked at before it
 * rendered. Every rule about what may reach a person therefore has to be a property of the
 * assembly rather than a habit of whoever laid the screen out, and these are those
 * properties.
 *
 * **A withheld field renders the lock and nothing else.** `ui/Lock.tsx` takes no props and
 * `brain.core.redaction.render_lock` takes no arguments, so that two people comparing screens
 * cannot read off which of them was refused and why. A form is the third place that rule gets
 * rebuilt after the stylesheet and the grid, and it gets rebuilt as a disabled input with a
 * tooltip: one word in a library that offers exactly that word for exactly this case.
 *
 * **A field the API did not send leaves no shape behind.** A form has a layout, so absence can
 * be read from a gap, from a `required` entry naming nothing, or from a control holding a
 * place. Two callers whose schemas differ by one property must get two forms that differ by
 * that property and by nothing else.
 *
 * **A withheld value is never written back.** This is the one here that damages data rather
 * than disclosing it: a form library hands its caller a whole object on submit, and a locked
 * property arrives in it empty.
 *
 * The lock is compared against `Lock` rendered on its own rather than against a description of
 * it, and the redaction reasons are read out of the Python enum rather than listed, for the
 * reason the rest of this directory does both: a check written from memory is a check that
 * stops checking on the day the thing it copied changes.
 *
 * Task ids: M32.5.2.2
 */

import { fireEvent, render } from "@testing-library/react";
import type { RJSFSchema } from "@rjsf/utils";
import { describe, expect, test } from "vitest";
import {
  CREDENTIAL_FORMAT,
  formShape,
  lockedFieldsFor,
  UnreadableSchema,
  withoutWithheld,
} from "../src/components/formSchema";
import { lockedCellKey, lockedCellsFrom } from "../src/components/paging";
import { CHECK_THESE_ANSWERS, SchemaForm } from "../src/components/SchemaForm";
import { Lock } from "../src/ui/Lock";
import { backendLockedFieldFields, backendRedactionReasons } from "./support/python";

/** A record with one field somebody may hold and one they may not. No digits anywhere. */
const CLIENT_SCHEMA: RJSFSchema = {
  type: "object",
  title: "Client",
  required: ["name", "contract_value"],
  properties: {
    name: { type: "string", title: "Name" },
    owner: { type: "string", title: "Owner" },
    contract_value: { type: "string", title: "Contract value" },
  },
};

const WITHHELD = "contract_value";

function form(props: Partial<Parameters<typeof SchemaForm>[0]> = {}) {
  return render(<SchemaForm caption="Client" schema={CLIENT_SCHEMA} {...props} />).container;
}

/** Everything a person could tab to, in document order. */
function focusable(container: HTMLElement): string[] {
  return [...container.querySelectorAll("input, select, textarea, button, [tabindex]")].map(
    (element) => element.getAttribute("id") ?? element.tagName.toLowerCase(),
  );
}

/** The ids of every control the form built, which is its inventory of fields. */
function fieldIds(container: HTMLElement): string[] {
  return [...container.querySelectorAll("input, select, textarea")].map(
    (element) => element.id,
  );
}

describe("a field the caller may not see", () => {
  test("a withheld field renders the lock and no explanation of itself", () => {
    // What breaks if this is deleted: the disclosure the whole redaction design exists to
    // prevent, arriving through the most natural line in a form library. `ui:readonly`,
    // `ui:disabled` or a placeholder saying why are each one word, and each varies with the
    // reason: "out of scope" says the field exists on records in another department. The
    // reasons are read out of the Python enum rather than listed here, so a reason added
    // there is covered without anybody updating this file.
    const container = form({ locked: new Set([WITHHELD]) });

    expect(container.querySelectorAll(".lock")).toHaveLength(1);
    for (const reason of backendRedactionReasons()) {
      expect(container.innerHTML).not.toContain(reason);
    }
    // No control for it, of any kind. A read-only input would render the value and a
    // disabled one would carry it in the DOM, which is one developer-tools panel from being
    // read.
    expect(fieldIds(container)).toEqual(["root_name", "root_owner"]);
  });

  test("a lock in a form is the same lock as anywhere else", () => {
    // What breaks if this is deleted: the form grows its own way of saying "withheld". A
    // greyed box, a dash, an italic "restricted" or a tooltip would each be the lock's reason
    // rendered in a new place. This asserts the markup that reaches a form is byte for byte
    // the markup that reaches a table cell and a definition list, which is what makes the
    // registered field name load-bearing: a name that did not match the form's own registry
    // would render the library's fallback field and no lock at all.
    const standalone = render(<Lock />).container.innerHTML;
    const container = form({ locked: new Set([WITHHELD]) });
    const slot = container.querySelector(".form-withheld") as HTMLElement;

    expect(slot.innerHTML).toBe(standalone);
  });

  test("a withheld field is not a control and cannot be tabbed into", () => {
    // What breaks if this is deleted: a disabled input holding the field's place. It is the
    // library's own answer to this case, it looks tidy, and it is a control in the layout
    // that says "you could have this" while carrying the withheld value in the DOM. The tab
    // order is the half nobody checks by looking: a focus ring landing on a dead box is a
    // second channel saying the same thing the lock says, in a place a screenshot does not
    // show.
    const container = form({ locked: new Set([WITHHELD]) });

    expect(container.querySelectorAll("[disabled]")).toHaveLength(0);
    expect(focusable(container)).toEqual(["root_name", "root_owner", "button"]);
  });

  test("a withheld field cannot make the form unsubmittable", () => {
    // What breaks if this is deleted: the schema requires a field this caller cannot fill, so
    // the form refuses to submit for them and submits for their colleague. That difference is
    // readable by two people comparing screens, and it arrives as a validation message rather
    // than as anything anybody wrote. The fixture requires the withheld field on purpose.
    const submitted: unknown[] = [];
    const container = form({
      locked: new Set([WITHHELD]),
      formData: { name: "Acme", owner: "Ada" },
      onSubmit: (data) => submitted.push(data),
    });

    fireEvent.submit(container.querySelector("form") as HTMLFormElement);

    expect(submitted).toHaveLength(1);
    expect(container.querySelector(".notice")).toBeNull();
  });

  test("the value of a withheld field is never sent back", () => {
    // What breaks if this is deleted: the failure here is a write, not a read, and nothing on
    // the screen reports it. A form library returns a whole object on submit; a locked
    // property in the schema with no value arrives in it empty, and an endpoint that writes
    // what it is given replaces a figure this caller was never permitted to read with
    // nothing. The value is stripped rather than assumed absent, so a stale record handed in
    // as form data cannot carry it back out either.
    const submitted: unknown[] = [];
    const container = form({
      locked: new Set([WITHHELD]),
      formData: { name: "Acme", owner: "Ada", contract_value: "ninety two thousand" },
      onSubmit: (data) => submitted.push(data),
    });

    // And it never reached the screen on the way in.
    expect(container.innerHTML).not.toContain("ninety two thousand");

    fireEvent.submit(container.querySelector("form") as HTMLFormElement);

    expect(submitted).toEqual([{ name: "Acme", owner: "Ada" }]);
  });

  test("the whole field is replaced rather than its widget", () => {
    // What breaks if this is deleted: the lock keeps its surroundings. Replacing only the
    // widget leaves the library's field template wrapped around it, and that template is
    // where the description, the help block, the error slot and the described-by come from.
    // Each is an empty place beside a lock into which somebody later writes why. Asserting
    // the whole entry rather than the absence of a few keys is deliberate: `ui:readonly` is
    // only the spelling somebody would reach for today.
    const shape = formShape(CLIENT_SCHEMA, new Set([WITHHELD]));
    expect(Object.keys(shape.uiSchema[WITHHELD] as object)).toEqual(["ui:field"]);
    expect(shape.schema.required).toEqual(["name"]);
    expect(shape.withheld).toEqual(new Set([WITHHELD]));
  });

  test("a locked name the schema does not carry builds nothing", () => {
    // What breaks if this is deleted: the console invents a field. A locked entry naming a
    // property the schema does not have gives this form no title, no type and no position, so
    // the only thing it could render is a lock captioned with a raw identifier out of a
    // payload. That is the console describing a field it was never told about, in words the
    // API did not choose.
    const container = form({ locked: new Set(["salary"]) });
    expect(container.querySelectorAll(".lock")).toHaveLength(0);
    expect(container.innerHTML).not.toContain("salary");
  });
});

describe("what a payload says was withheld", () => {
  test("a locked field is two names and a reason attached to it reaches nothing", () => {
    // What breaks if this is deleted: the reason arrives in the browser and something renders
    // it. `LockedField` carries entity, record id and field and no reason, because the reason
    // is the part that discloses. A payload that grew one, from a middleware being helpful or
    // a model copied into place, must find nothing here to carry it.
    const reasons = backendRedactionReasons();
    expect(reasons.length).toBeGreaterThan(0);

    const names = lockedFieldsFor(
      [{ entity: "client", record_id: "two", field: WITHHELD, reason: reasons[0] }],
      "two",
    );

    expect([...names]).toEqual([WITHHELD]);
    for (const reason of reasons) {
      expect([...names].join(" ")).not.toContain(reason);
    }
  });

  test("the form and the grid agree about which fields a payload locked", () => {
    // What breaks if this is deleted: two readers of one payload drift apart, and a field
    // locked in a table renders its value in the form beside it. They are separate functions
    // because a grid needs a key per cell and a form needs a name per field, and separate
    // functions are exactly the shape that drifts, so the agreement is asserted rather than
    // assumed. The locked-field model is read out of Python too, so a field added there fails
    // here instead of being quietly ignored by both.
    expect(backendLockedFieldFields().sort()).toEqual(["entity", "field", "record_id"]);

    const payload = [
      { entity: "client", record_id: "two", field: WITHHELD },
      { entity: "client", record_id: "two", field: "owner" },
      { entity: "client", record_id: "three", field: "name" },
    ];

    const cells = lockedCellsFrom(payload);
    const names = lockedFieldsFor(payload, "two");

    expect([...names].sort()).toEqual(["contract_value", "owner"]);
    for (const name of names) {
      expect(cells.has(lockedCellKey("two", name))).toBe(true);
    }
    // And a field locked on another record is not locked on this one. Locking it here would
    // say something true about the other record to somebody looking at this one.
    expect(names.has("name")).toBe(false);
  });

  test("an entry with no record or no field locks nothing", () => {
    // What breaks if this is deleted: a malformed entry locks the wrong field or silently
    // locks none. Both are worse than reading nothing, and neither is visible on a screen.
    expect(lockedFieldsFor([{ field: "owner" }, { record_id: "two" }, 7, null], "two").size).toBe(
      0,
    );
    expect(lockedFieldsFor("not an array", "two").size).toBe(0);
  });
});

describe("a field the API did not send", () => {
  test("a field that is not in the schema leaves no trace of itself", () => {
    // What breaks if this is deleted: absence acquires a shape. A form has a layout, so a
    // missing field can be read from a gap, a numbered legend, or a control holding a place,
    // and none of those is something anybody would write deliberately. Two callers whose
    // schemas differ by one property get two forms differing by that property alone: the
    // remaining ids are unchanged, so nothing is renumbered around the hole.
    const narrower: RJSFSchema = {
      ...CLIENT_SCHEMA,
      required: ["name"],
      properties: { name: { type: "string", title: "Name" }, owner: { type: "string", title: "Owner" } },
    };

    const whole = fieldIds(form());
    const narrow = fieldIds(render(<SchemaForm caption="Client" schema={narrower} />).container);

    expect(whole).toEqual(["root_name", "root_owner", "root_contract_value"]);
    expect(narrow).toEqual(["root_name", "root_owner"]);

    const container = render(<SchemaForm caption="Client" schema={narrower} />).container;
    expect(container.innerHTML).not.toContain("contract_value");
    expect(container.innerHTML).not.toContain("Contract value");
  });

  test("nothing on a generated form is a count of fields", () => {
    // What breaks if this is deleted: "3 of 5 completed", or its innocent cousin "5 fields".
    // A count beside a form assembled from a schema filtered by what somebody may see
    // discloses the rest by subtraction, exactly as a row count beside a filtered list does.
    // The fixture carries no digits of its own, so any digit on the screen came from the form
    // rather than from the schema.
    const text = form({ locked: new Set([WITHHELD]) }).textContent ?? "";
    expect(text).not.toMatch(/\d/);
  });
});

describe("what a generated form refuses to build", () => {
  test("a property that asks for a secret gets no input at all", () => {
    // What breaks if this is deleted: the boundary check is bypassed by a payload.
    // `scripts/check-boundaries.mjs` refuses a password input in this console's own source,
    // and a form assembled from a document is the way round a check that reads source. The
    // property is dropped rather than rendered as an ordinary text box, which would be worse:
    // the secret would be on the screen in plain sight.
    const withSecret: RJSFSchema = {
      type: "object",
      required: ["token"],
      properties: {
        name: { type: "string", title: "Name" },
        token: { type: "string", title: "Token", format: CREDENTIAL_FORMAT },
      },
    };
    const container = render(<SchemaForm caption="Connector" schema={withSecret} />).container;

    expect(fieldIds(container)).toEqual(["root_name"]);
    expect(container.querySelectorAll("input[type=password]")).toHaveLength(0);
    expect(container.innerHTML).not.toContain("Token");
  });

  test("a required entry for a property that is gone does not survive", () => {
    // What breaks if this is deleted: the form is permanently invalid with no field to point
    // the failure at. A required name with no property behind it fails validation on every
    // submission and the message names something nobody can see, which reads as a permission
    // problem and is a bug in the schema handling.
    const withSecret: RJSFSchema = {
      type: "object",
      required: ["name", "token"],
      properties: {
        name: { type: "string" },
        token: { type: "string", format: CREDENTIAL_FORMAT },
      },
    };
    const shape = formShape(withSecret);

    expect(shape.schema.required).toEqual(["name"]);
    expect(Object.keys(shape.schema.properties ?? {})).toEqual(["name"]);
    expect(withoutWithheld({ name: "a", token: "shh" }, shape.withheld)).toEqual({ name: "a" });
  });

  test("a schema that is not a schema is a failure rather than an empty form", () => {
    // What breaks if this is deleted: a broken endpoint renders as a form with no fields,
    // which a person reads as "there is nothing to fill in here" rather than as a fault. An
    // empty schema is a legitimate answer and a malformed body is not.
    expect(() => formShape(null as unknown as RJSFSchema)).toThrow(UnreadableSchema);
    expect(() => formShape([] as unknown as RJSFSchema)).toThrow(UnreadableSchema);
  });
});

describe("what a generated form does build", () => {
  test("a form is built from the schema and submits what was typed into it", () => {
    // What breaks if this is deleted: every test above is satisfied by a component that
    // renders nothing at all. This is the positive sibling and it is also the plainest
    // statement of the leaf: the fields come from a document rather than from anybody's
    // markup, including kinds of field nobody here wrote a renderer for.
    const submitted: unknown[] = [];
    const schema: RJSFSchema = {
      type: "object",
      properties: {
        name: { type: "string", title: "Name" },
        live: { type: "boolean", title: "Live" },
        tier: { type: "string", title: "Tier", enum: ["bronze", "silver"] },
      },
    };
    const container = render(
      <SchemaForm
        caption="Agent"
        schema={schema}
        formData={{ name: "Acme" }}
        onSubmit={(data) => submitted.push(data)}
      />,
    ).container;

    expect(fieldIds(container)).toEqual(["root_name", "root_live", "root_tier"]);
    expect((container.querySelector("#root_live") as HTMLInputElement).type).toBe("checkbox");
    expect(container.querySelector("#root_tier")?.tagName).toBe("SELECT");

    fireEvent.change(container.querySelector("#root_name") as HTMLInputElement, {
      target: { value: "Borden" },
    });
    fireEvent.submit(container.querySelector("form") as HTMLFormElement);

    expect(submitted).toEqual([{ name: "Borden" }]);
  });

  test("the form's own buttons and messages are the console's, not the library's", () => {
    // What breaks if this is deleted: a second design system arrives one class at a time. The
    // library's submit button is `btn btn-info` and its error list is a red panel, which is a
    // severity variant of the thing `ui/Notice.tsx` deliberately has exactly one of. Neither
    // has a rule in this console's stylesheet, so both would render unstyled in a project
    // whose whole theme is one file of tokens, and the fix somebody reaches for is a colour
    // written in a component.
    const submitted: unknown[] = [];
    const container = form({ onSubmit: (data) => submitted.push(data) });

    const button = container.querySelector("button") as HTMLButtonElement;
    expect(button.getAttribute("class")).toBe("button");
    expect(container.innerHTML).not.toContain("btn-info");

    // Submitting with a required field empty is the console's own validation, about what
    // somebody typed here and never about what the API decided, which is why showing it is
    // safe at all.
    fireEvent.submit(container.querySelector("form") as HTMLFormElement);

    expect(submitted).toHaveLength(0);
    expect(container.querySelector(".notice__title")?.textContent).toBe(CHECK_THESE_ANSWERS);
    expect(container.querySelector(".panel-danger")).toBeNull();
  });
});
