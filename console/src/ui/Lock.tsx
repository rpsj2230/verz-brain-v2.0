/**
 * How a withheld field renders. One appearance, for everybody, always.
 *
 * **This component takes no props, and that is the mechanism rather than an accident.**
 * `brain.core.redaction.render_lock` takes no arguments for exactly this reason: a lock
 * that varied by viewer, by field, by classification or by reason would make its own shape
 * a side channel, and two people comparing screens could read the difference and learn
 * which of them was refused for which reason. A signature with nothing in it cannot vary
 * by anything, so the property can be checked by reading the signature rather than by
 * trusting the body. Adding a prop here, any prop, is the change to refuse in review.
 *
 * The same rule applies to the class name. There is one `.lock` rule in `styles/app.css`
 * and no modifiers. `.lock--out-of-scope` would be the leak written in CSS: the reason a
 * field was withheld is the part that discloses something, because "out of scope" says the
 * field exists on records in some other department and "unclassified" says something about
 * the policy. `RedactionReason` never leaves the trace, and the trace is read by auditors.
 *
 * **Why the field's existence is disclosed at all**, when a whole record the caller cannot
 * see is dropped rather than emptied: the lock is about fields inside a record whose
 * existence was already legitimately disclosed. The record-level rule and the field-level
 * rule are different rules, and the redaction module argues both.
 *
 * LOCK_TEXT is a copy of `brain.core.redaction.LOCK_TEXT`. There is no shared artefact
 * between Python and this bundle to carry it, so `scripts/export-openapi.py` compares the
 * two every time the schema is regenerated and refuses to generate anything if they have
 * drifted. That check matches this exact export statement, so renaming the constant fails
 * loudly rather than quietly turning the check off.
 */

export const LOCK_TEXT = "Restricted";

export function Lock() {
  return <span className="lock">{LOCK_TEXT}</span>;
}
