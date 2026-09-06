/**
 * The first page after sign-in: who the API thinks is asking, and nothing else.
 *
 * **Every fact on this page came from `GET /api/v1/me`, and none of it was read out of a
 * token.** That is the whole reason the page exists in this shape. The console holds an
 * opaque string it never decodes, so the only way it can know who is signed in is to ask,
 * and the answer is computed from grants this browser never receives. `CallerView` is
 * deliberately the caller's own facts with no list of capabilities on it, for the reason its
 * own docstring gives: a capability list would be the first thing cached in a browser and
 * used to decide what to render, which is a permission model in the copy an attacker edits.
 *
 * **The page adds no interpretation to any value.** `assurance` is on the response because
 * it is the one fact a person can act on, and this page still renders it as the word the API
 * sent rather than as a sentence about signing in again. A sentence would be a mapping from
 * a value to a meaning, written here, out of step with the API within a release; the API
 * owns the vocabulary, in the same way `ui/Status.tsx` renders a state word exactly as it
 * arrived. The four values that read as tags render through `Chip`, which has one appearance
 * and no tone, so nothing on this screen can decide that somebody's assurance is alarming.
 *
 * **A field the API did not send contributes nothing.** `primary_department` is nullable and
 * a caller can legitimately have none, so the row is absent rather than present and empty.
 * There is no lock on this page and there must not be one: a lock says the API told us a
 * field exists and withheld it, `/me` sends no `locked`, and inventing one from a null would
 * be the console asserting a refusal nobody made.
 *
 * **A failure is the API's sentence and the trace id, and nothing else.** Including a 404,
 * which on this route means the token authenticated and the subject maps to no principal
 * this company wrote down. Saying so would be the console explaining a refusal it did not
 * observe. See `A_404_IS_NOT_AN_EXPLANATION`.
 *
 * The lock sample that used to be on this page is gone. It was there so the lock could be
 * seen in both themes before any record rendered, and its own comment said to delete it when
 * a real record rendered anywhere. `/records/{entity}` renders one now.
 *
 * Task ids: M32.5.1.1, M32.5.1.2
 */

import { useResource } from "../api/useResource";
import type { components } from "../api/schema";
import { Chip } from "../ui/Chip";
import { Notice } from "../ui/Notice";

type CallerView = components["schemas"]["CallerView"];

/** Where the API says who is asking. One route, named once. */
export const ME_PATH = "/me";

/** The one heading over any failure. The API's own sentence goes underneath it. */
export const SOMETHING_DID_NOT_WORK = "That did not work";

/**
 * How each fact on the response is shown.
 *
 * A list rather than seven blocks of markup, so that "every fact the API sends about the
 * caller reaches the screen" is a property a test can hold against the Python model rather
 * than a thing somebody checks by eye. `tests/overview-page.test.tsx` reads the field names
 * off `brain.api_routes.CallerView` and fails when this list and that model disagree, in
 * either direction: a field added there and not here would arrive and be dropped silently,
 * which is the failure nobody notices.
 *
 * `chip` says the value is a short closed-vocabulary word rather than prose. It chooses
 * between two appearances that carry no colour and no severity, so it is a layout decision
 * and not a tone. `code` is for the two identifiers that exist to be copied into a message
 * to somebody.
 */
export const CALLER_FIELDS: readonly {
  readonly name: keyof CallerView;
  readonly label: string;
  readonly as: "text" | "chip" | "code";
}[] = [
  { name: "display_name", label: "Name", as: "text" },
  { name: "principal_id", label: "Principal", as: "code" },
  { name: "primary_department", label: "Department", as: "chip" },
  { name: "employment", label: "Employment", as: "chip" },
  { name: "assurance", label: "Assurance", as: "chip" },
  { name: "channel", label: "Channel", as: "chip" },
  { name: "ent_hash", label: "Entitlement digest", as: "code" },
];

/** One value, in the appearance its row asked for. Never a value this file composed. */
function CallerValue({ value, as }: { readonly value: string; readonly as: "text" | "chip" | "code" }) {
  if (as === "chip") {
    return <Chip label={value} />;
  }
  if (as === "code") {
    return <code>{value}</code>;
  }
  return <>{value}</>;
}

export function Overview() {
  const caller = useResource<CallerView>(ME_PATH);

  return (
    <article className="page">
      <h1>Overview</h1>
      <p className="lede">Who this console is signed in as, according to the API.</p>

      <section className="card">
        <h2>You</h2>

        {caller.failure ? (
          <Notice title={SOMETHING_DID_NOT_WORK} traceId={caller.failure.traceId}>
            <p>{caller.failure.message}</p>
          </Notice>
        ) : null}

        {caller.busy ? (
          <p className="note" role="status">
            Loading.
          </p>
        ) : null}

        {caller.data ? (
          <dl className="fields">
            {CALLER_FIELDS.map((field) => {
              const value = caller.data?.[field.name];
              // Absent stays absent. A row rendered with nothing in it is a shape where a
              // fact would be, and two people comparing screens can read a shape.
              if (typeof value !== "string" || value === "") {
                return null;
              }
              return (
                <div className="fields__row" key={field.name}>
                  <dt>{field.label}</dt>
                  <dd>
                    <CallerValue value={value} as={field.as} />
                  </dd>
                </div>
              );
            })}
          </dl>
        ) : null}
      </section>
    </article>
  );
}
