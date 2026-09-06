/**
 * A compact label whose text came from the data. One appearance, always neutral.
 *
 * **A chip has no tone, and that is the point of having a separate component for it.** The
 * text in a chip is a value out of a record: a tag, a department, the filter somebody
 * typed. The moment a chip's colour could depend on its text, the console is holding an
 * opinion about data it did not produce, and the first opinion anybody writes is the one
 * that looks helpful: red for the restricted ones. That is `.lock--out-of-scope` arrived at
 * from the other direction, through a component that was only ever meant to show a tag.
 *
 * So the split across these three files is by what supplies the appearance, not by what
 * the thing looks like:
 *
 * - `Chip` takes arbitrary text and has exactly one appearance.
 * - `Badge` takes a tone, and every call site writes it as a literal.
 * - `Status` is the only place a tone is chosen from a value, through a closed table.
 *
 * A chip is not a count. Nothing in this console renders a number of things it is not also
 * showing, because "3" beside a list of two is the count of hidden items that
 * `brain.core.redaction` refuses to let out of the walker.
 *
 * Task ids: M32.5.2.4
 */

interface ChipProps {
  /** The value to show. Rendered as text; the console adds nothing to it. */
  readonly label: string;
}

export function Chip({ label }: ChipProps) {
  return <span className="chip">{label}</span>;
}
