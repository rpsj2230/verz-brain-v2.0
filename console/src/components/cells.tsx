/**
 * Column cell renderers built from the shared primitives, so a grid does not grow its own.
 *
 * A grid is where a second set of labels appears. Somebody needs a status column, the
 * primitive is one directory away, and a `<span className="pill green">` is quicker to type
 * than an import. Two of those and the console has a second palette that no theme test
 * covers, because the colour was written in TypeScript rather than in a stylesheet.
 *
 * These are the two shapes a grid actually needs. Both are one line, and being one line is
 * the argument for them existing: the alternative is not a better renderer, it is a
 * hand-rolled one.
 *
 * **Neither takes a tone.** `statusCell` renders `Status`, which is the one module allowed
 * to choose a tone, and it chooses from a closed table of words the API defines. A column
 * cannot pass a colour in, so no grid can decide that some of its rows are alarming.
 *
 * Task ids: M32.5.2.4
 */

import { Chip } from "../ui/Chip";
import { Status } from "../ui/Status";

/** What TanStack hands a cell renderer. Only the value is used; nothing branches on a row. */
interface CellValue {
  getValue: () => unknown;
}

/**
 * A value shown as a neutral chip.
 *
 * Anything that is not a string renders as nothing rather than as `[object Object]` or as
 * the string "null". An empty cell is the honest rendering of a field that is absent, and
 * `exactOptionalPropertyTypes` is on in this project precisely because absent and present
 * but empty are different things in a payload from the gate.
 */
export function chipCell(context: CellValue) {
  const value = context.getValue();
  return typeof value === "string" && value !== "" ? <Chip label={value} /> : null;
}

/**
 * A state word shown with its tone.
 *
 * The word goes through unchanged; `Status` looks up how loudly to say it and falls back to
 * the quietest tone for anything it does not recognise, so a vocabulary this console has
 * not caught up with renders as the API's own word in grey rather than as an invented alarm
 * or an invented reassurance.
 */
export function statusCell(context: CellValue) {
  const value = context.getValue();
  return typeof value === "string" && value !== "" ? <Status state={value} /> : null;
}
