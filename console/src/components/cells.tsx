/**
 * Column cell renderers built from the shared primitives, so a grid does not grow its own.
 *
 * A grid is where a second set of labels appears. Somebody needs a status column, the
 * primitive is one directory away, and a `<span className="pill green">` is quicker to type
 * than an import. Two of those and the console has a second palette that no theme test
 * covers, because the colour was written in TypeScript rather than in a stylesheet.
 *
 * These are the three shapes a grid actually needs. Each is a few lines, and being a few
 * lines is the argument for them existing: the alternative is not a better renderer, it is
 * a hand-rolled one.
 *
 * **None of them takes a tone.** `statusCell` renders `Status`, which is the one module
 * allowed to choose a tone, and it chooses from a closed table of words the API defines. A
 * column cannot pass a colour in, so no grid can decide that some of its rows are alarming.
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

/**
 * Written down because rendering a value the console cannot show is the tempting fix, and
 * both spellings of it are worse than the empty cell.
 */
export const A_CELL_SHOWS_A_VALUE_OR_SHOWS_NOTHING =
  "A cell renders a JSON primitive as the API spelled it and renders nothing for anything " +
  "else. It never stringifies a structure: an object inside a record is a record, the " +
  "redactor walked it separately and its own withheld fields are locked against its own " +
  "id, so flattening it into one cell would print the fields that survived and silently " +
  "drop the locks that belong to them. It never substitutes a dash, a placeholder or the " +
  "word null either, because a cell that says something about an absent value is a cell " +
  "that says something about the values it is not showing.";

/**
 * Any value out of a record, as text.
 *
 * The renderer a grid over a payload needs, where the columns are whatever the API sent
 * and the values are whatever those columns hold. `chipCell` is the wrong tool for that
 * job in both directions: it drops a number, which is most of a row, and it says "tag"
 * about free text, which is most of the rest.
 *
 * A number and a boolean go through `String`, which is the JSON spelling of both. A string
 * goes through unchanged, including the empty one, which renders as nothing because that is
 * what it is. `null` renders as nothing for the same reason: the record carried no value,
 * and the console has no better word for that than no word.
 *
 * **A structured value renders as nothing rather than as text.** See
 * `A_CELL_SHOWS_A_VALUE_OR_SHOWS_NOTHING`. It is not reachable from the row plane today:
 * `brain.core.redaction` drops an untagged nested mapping whole before serialising, so a
 * JSON column arrives with its key already gone rather than as an object in a cell.
 */
export function valueCell(context: CellValue) {
  const value = context.getValue();
  if (typeof value === "string") {
    return value === "" ? null : value;
  }
  if (typeof value === "number" || typeof value === "boolean") {
    return String(value);
  }
  return null;
}
