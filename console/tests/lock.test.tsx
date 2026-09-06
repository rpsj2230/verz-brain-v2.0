/**
 * The lock: one appearance, for everybody, always.
 *
 * `brain.core.redaction.render_lock` takes no arguments so that a withheld field cannot
 * render differently for different people, fields, classifications or reasons, because a
 * lock that varied would make its own shape a side channel: two people comparing screens
 * could read the difference and learn which of them was refused, and for what. `Lock` is
 * the console's half of that rule, and these are the tests that hold it there.
 *
 * Task ids: M32.5.1.1
 */

import { createElement } from "react";
import { render } from "@testing-library/react";
import { describe, expect, test } from "vitest";
import { Lock } from "../src/ui/Lock";
import { parseCss } from "./support/css";
import { backendLockText } from "./support/python";
import { readConsoleFile } from "./support/repo";

/** Every lock rendered inside a container, as markup. Empty is a failure, not a pass. */
function locksIn(container: HTMLElement): string[] {
  const found = [...container.querySelectorAll(".lock")].map((lock) => lock.outerHTML);
  if (found.length === 0) {
    throw new Error("Nothing with the lock's class rendered, so there is nothing to compare.");
  }
  return found;
}

describe("the lock", () => {
  test("the lock renders identically in every context", () => {
    // What breaks if this is deleted: the console's copy of the rule that a withheld field
    // looks the same everywhere. A lock that picked up its surroundings, its position in a
    // list, or an ancestor's state would let two people compare screens and infer which of
    // them was refused, which is the disclosure the whole redaction design exists to
    // prevent. This is the behavioural half; the signature test below is the mechanism.
    const seen = new Set<string>();

    for (const markup of locksIn(render(<Lock />).container)) {
      seen.add(markup);
    }

    for (const theme of ["light", "dark"]) {
      document.documentElement.setAttribute("data-theme", theme);
      for (const markup of locksIn(render(<Lock />).container)) {
        seen.add(markup);
      }
    }
    document.documentElement.removeAttribute("data-theme");

    const nested = render(
      <dl className="fields">
        <div className="fields__row">
          <dt>Contract value</dt>
          <dd>
            <Lock />
          </dd>
        </div>
        <div className="fields__row">
          <dt>Owner</dt>
          <dd>
            <Lock />
          </dd>
        </div>
      </dl>,
    );
    expect(locksIn(nested.container)).toHaveLength(2);
    for (const markup of locksIn(nested.container)) {
      seen.add(markup);
    }

    const inTable = render(
      <table>
        <tbody>
          <tr>
            <td>
              <Lock />
            </td>
          </tr>
        </tbody>
      </table>,
    );
    for (const markup of locksIn(inTable.container)) {
      seen.add(markup);
    }

    expect([...seen]).toHaveLength(1);
  });

  test("the lock component accepts no props", () => {
    // What breaks if this is deleted: the mechanism rather than the rendering. A signature
    // with nothing in it cannot vary by anything, so the property is checked by reading
    // the signature rather than by trusting the body. The moment `Lock` takes a `reason`
    // or a `field`, varying the lock stops being impossible and becomes merely unusual,
    // and the next person who wants a nicer empty state has an argument.
    expect(Lock.length).toBe(0);

    // And the behavioural statement of the same thing, for the shape a default parameter
    // would take: anything handed to it must change nothing about what it renders.
    const plain = render(<Lock />).container.innerHTML;
    const smuggled = render(
      createElement(Lock, {
        reason: "out-of-scope",
        field: "contract_value",
        classification: "restricted",
        viewer: "someone-else",
        children: "Not restricted",
      } as never),
    ).container.innerHTML;
    expect(smuggled).toBe(plain);
  });

  test("the lock text matches the backend constant", () => {
    // What breaks if this is deleted: the console and the API drift apart on what a
    // withheld field says, and a person comparing two channels learns which system
    // withheld which field. `scripts/export-openapi.py` checks this too, but only when
    // somebody regenerates the schema, which is not a thing that happens on every change.
    // The backend value is read out of the Python source, so this is not the constant
    // being compared with itself.
    const backend = backendLockText();
    expect(backend).not.toBe("");

    const { container } = render(<Lock />);
    expect(container.textContent).toBe(backend);
  });

  test("the lock carries one class name and the stylesheet gives it no modifiers", () => {
    // What breaks if this is deleted: the leak written in CSS. `.lock--out-of-scope` would
    // say the field exists on records in another department, and `.lock--unclassified`
    // would say something about the policy; the reason a field was withheld is the part
    // that discloses. The stylesheet is parsed rather than searched because both style
    // files name the forbidden modifier in a comment in order to forbid it, and a
    // substring search would be satisfied by that comment with the real rule gone.
    const { container } = render(<Lock />);
    const lock = container.querySelector(".lock");
    expect(lock?.getAttribute("class")).toBe("lock");

    const selectors = parseCss(readConsoleFile("src/styles/app.css"))
      .map((rule) => rule.selector)
      .filter((selector) => /(^|[\s,>+~])\.lock\b/.test(selector) || selector.includes(".lock"));
    expect(selectors).toEqual([".lock"]);
  });

  test("the lock is the only thing the component renders", () => {
    // What breaks if this is deleted: a helpful addition next to the lock. A tooltip, a
    // title attribute, an aria-label naming the field or a "why?" link would each be a
    // place for the reason to reappear, and each would be added by somebody trying to
    // improve the experience rather than to leak anything.
    const { container } = render(<Lock />);
    expect(container.children).toHaveLength(1);
    const lock = container.firstElementChild as HTMLElement;
    expect(lock.tagName).toBe("SPAN");
    expect(lock.getAttributeNames()).toEqual(["class"]);
    expect(lock.children).toHaveLength(0);
  });
});
