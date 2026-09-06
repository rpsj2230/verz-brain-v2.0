/**
 * A message to a person: a failure, a configuration problem, a state the console is in.
 *
 * **One appearance, with no severity variants.** There is no `type="error"` and no
 * `type="warning"`, and the absence is deliberate. The moment a notice can look different,
 * something has to decide which look a given message gets, and the only fact available to
 * decide with is the API's outcome. A 404 that looked different from a 503 would be
 * harmless; a 404 that looked different depending on which kind of 404 it was would not,
 * and the two changes are one line apart. Keeping one appearance means the question never
 * arises.
 *
 * The trace id is shown when there is one, in a monospace face because it exists to be
 * copied into a message to somebody. It identifies a request rather than a record, which
 * is why it is safe to show at all.
 *
 * `role="status"` rather than `role="alert"`: alert interrupts a screen reader mid
 * sentence, which is right for a fire alarm and wrong for "I could not find that".
 */

import type { ReactNode } from "react";

interface NoticeProps {
  readonly title: string;
  readonly children?: ReactNode;
  readonly traceId?: string;
}

export function Notice({ title, children, traceId }: NoticeProps) {
  return (
    <div className="notice" role="status">
      <p className="notice__title">{title}</p>
      {children ? <div className="notice__body">{children}</div> : null}
      {traceId ? (
        <p className="notice__trace">
          Reference <code>{traceId}</code>
        </p>
      ) : null}
    </div>
  );
}
