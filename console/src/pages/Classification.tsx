/**
 * One document's column classification, and the editor for one column of it.
 *
 * `brain.knowledge.columns` says why this screen exists: "A price list carries the sell
 * price everybody needs beside the cost and margin almost nobody may see, and the obvious
 * move is to restrict the document and keep a second, safer copy for the rest of the
 * company. Within a year there are three near-identical price lists and two of them are
 * stale." The rules that avoid that were legible only to whoever had the source tree open,
 * which is the population that already knows them.
 *
 * **This is an administrative screen and it is the one place in this console where a
 * mistake widens what other people may see.** Everything it can do is guarded by
 * `admin:field_classification`, which `gate.admission.CHANNEL_VERBS` withholds from a
 * service-account token and `ASSURANCE_VERBS` withholds from a one-factor session. Nothing
 * here checks either. The API does, on every request, and this file draws or does not draw a
 * control according to a flag on the response. See
 * `AN_EDITOR_DRAWN_OR_HIDDEN_ASKS_THE_SAME_QUESTION`.
 *
 * **Nothing on this screen saves anything, and it says so on the screen rather than in a
 * comment.** There is no route that stores a classification, because there is no table
 * behind one: a `TableClassification` is a constant compiled into the API's process, and
 * applying a change is a source edit and a deploy. So no audit row is written either, and
 * `THERE_IS_NO_SAVE` is rendered wherever a person could otherwise conclude there was one.
 * An editor that let somebody believe they had changed a disclosure rule would be worse than
 * no editor, because they would stop checking.
 *
 * **The judgement about a change comes back from the API and is never made here.** Which
 * columns a rule affects, whether the change widens, and which other columns a caller short
 * of one column would newly reach are all answered by
 * `POST /api/v1/classifications/{entity}/columns/{column}/review`, which runs the same
 * closure that withholds a column at request time. A copy of that arithmetic in a browser
 * would be a second answer to what a person may see. See
 * `A_WIDENING_IS_NAMED_BY_THE_API_AND_NEVER_WORKED_OUT_HERE`.
 *
 * **A widening is named, never counted, and never softened.** When the API says a proposal
 * widens, this screen lists the columns it said would be exposed, in the API's own words,
 * under a sentence saying what that means. The exposed columns are named because naming is
 * what the reader has to act on; nothing anywhere on this screen is counted, which is the
 * rule `paging.ts` states as `A_PAGE_NEVER_CARRIES_A_COUNT` and this file keeps for
 * everything outside the table.
 *
 * **The address is the whole of the state.** `/classification` is the form,
 * `/classification/{entity}` is one classification, and `/classification/{entity}/{column}`
 * is that classification with one column's editor open, so a person can send a colleague the
 * exact rule they are arguing about. Holding either in component state would make the screen
 * unlinkable and would put the back button somewhere it does not belong.
 *
 * **This console does not know which entities are classified and does not ask.** There is no
 * route that lists them and there must not be one, so somebody types a name. See
 * `THE_CONSOLE_DOES_NOT_KNOW_WHAT_IS_CLASSIFIED`; it is the records screen's rule, one level
 * up, and the cost is the same: a person has to know the name.
 *
 * **The form library is the records screen's, so this route is code-split too.**
 * `@rjsf/core` with the ajv validator is the largest thing in this console by a wide margin,
 * and a third eager import of it would put it back in the entry chunk for everybody. Its
 * ajv validator also needs `unsafe-eval`, which `SchemaForm` records as a live conflict with
 * the proposed Content-Security-Policy; that is unresolved and is not resolved here.
 *
 * Task ids: M7.5.3
 */

import { useCallback, useMemo, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { request } from "../api/client";
import type { ApiFailure } from "../api/errors";
import { useResource } from "../api/useResource";
import { DataTable } from "../components/DataTable";
import { SchemaForm } from "../components/SchemaForm";
import { Notice } from "../ui/Notice";
import {
  CLASSIFICATION_QUERY_SCHEMA,
  CLASSIFICATION_QUERY_UI,
  COLUMN_EDIT_UI,
  classificationAddress,
  classificationApiPath,
  classificationColumns,
  columnAddress,
  columnByName,
  columnEditSchema,
  derivationOptions,
  editableDefaults,
  ENTITY_FIELD,
  readClassification,
  readReview,
  reviewApiPath,
  submittedEdit,
  submittedEntity,
  type ColumnRow,
  type Review,
} from "./classificationQuery";

/**
 * What is shown when no entity has been named.
 *
 * It names no entity, for the reason `Records.tsx` gives about its own: "try price_list"
 * would be this console publishing a guess at what the company keeps, to everybody who can
 * open the page, before anybody asked the API anything.
 */
const NOTHING_ASKED_FOR = "Name a document above to see how its columns are classified.";

/**
 * The sentence that stops this screen reading as an editor that saves.
 *
 * Rendered beside the form rather than once at the top, because the place a person concludes
 * they have saved something is the place they pressed a button. It carries no number and
 * offers no action, because there is no action to offer.
 */
const THERE_IS_NO_SAVE =
  "Nothing here changes what anybody may see. A review says what a proposed rule would do; " +
  "applying it is a change to the source and a deploy, and no record of a review is kept.";

/** What is said when the address names a column this classification does not carry. */
const NO_SUCH_COLUMN = "No column of this classification has that name.";

/** The heading over a proposal the API said would not load at all. */
const IT_WOULD_NOT_LOAD = "This rule would not load";

/** The heading over a proposal the API called a widening. */
const IT_WIDENS = "This would let more people see more";

/**
 * What a widening means, in the console's own words rather than the API's.
 *
 * Safe to write here because it is a statement about the screen and about the mechanism,
 * not about anybody's data and not an explanation of a refusal. It says what the API's
 * `exposed` list is, which is the one thing a reader has to understand before acting on it:
 * those columns are not the ones being edited.
 */
const WHAT_A_WIDENING_MEANS =
  "The columns below would be reachable by people who cannot reach them today. They are " +
  "not necessarily the column being edited: withholding one column is often what keeps " +
  "another from being worked out.";

/** The heading over a proposal the API said changes nothing that widens. */
const IT_DOES_NOT_WIDEN = "This would not widen anything";

/** What is said when the proposed rule is the one that already stands. */
const NOTHING_WOULD_CHANGE = "This is the rule that already stands.";

/**
 * What one review said, rendered.
 *
 * A separate component so that the branch structure of a verdict is one thing to read. Every
 * word of substance in it came from the API: the changes are its vocabulary, the exposed
 * columns are its list, and the sentence explaining a failure to load is its own. What this
 * console adds are the four headings above, each of which is about this screen.
 */
function ReviewNotice({ review }: { readonly review: Review }) {
  if (review.wouldNotLoad !== "") {
    return (
      <Notice title={IT_WOULD_NOT_LOAD}>
        <p>{review.wouldNotLoad}</p>
      </Notice>
    );
  }
  if (review.widens) {
    return (
      <Notice title={IT_WIDENS}>
        <p>{WHAT_A_WIDENING_MEANS}</p>
        <ul className="review__exposed">
          {review.exposed.map((name) => (
            <li key={name}>{name}</li>
          ))}
        </ul>
        <p className="review__changes">{review.changes.join(" ")}</p>
        <p>{THERE_IS_NO_SAVE}</p>
      </Notice>
    );
  }
  return (
    <Notice title={IT_DOES_NOT_WIDEN}>
      {review.changes.length === 0 ? (
        <p>{NOTHING_WOULD_CHANGE}</p>
      ) : (
        <p className="review__changes">{review.changes.join(" ")}</p>
      )}
      <p>{THERE_IS_NO_SAVE}</p>
    </Notice>
  );
}

/**
 * One column's editor.
 *
 * A separate component because it holds the state of one review in flight and the
 * classification does not, which is `Matrix.tsx`'s reason for splitting its own editor out:
 * merging them would put a busy flag belonging to a POST on a component whose other job is
 * rendering a GET, and the first person to reuse it would find the grid greyed out while
 * somebody typed.
 *
 * A returned review replaces the previous one and a refusal replaces both, so the screen
 * never shows a verdict about a rule that is no longer in the form.
 */
function ColumnEditor({
  entity,
  row,
  options,
}: {
  readonly entity: string;
  readonly row: ColumnRow;
  readonly options: readonly string[];
}) {
  const [failure, setFailure] = useState<ApiFailure | null>(null);
  const [review, setReview] = useState<Review | null>(null);
  const [busy, setBusy] = useState(false);

  // Memoised on the options rather than rebuilt each render, because `SchemaForm` memoises
  // on the schema's identity and the ajv validator recompiles whenever it changes.
  const schema = useMemo(() => columnEditSchema(options), [options]);
  const defaults = useMemo(() => editableDefaults(row), [row]);

  const ask = useCallback(
    (submitted: unknown) => {
      const edit = submittedEdit(submitted);
      if (edit === null) {
        // Not a rule this screen recognises. Doing nothing is the answer: a proposal
        // assembled out of values nobody read is a question nobody meant to ask, and the
        // answer to it would be a verdict about who may see what.
        return;
      }
      setBusy(true);
      void (async () => {
        const result = await request<unknown>(reviewApiPath(entity, row.column), {
          method: "POST",
          body: edit,
        });
        setBusy(false);
        if (!result.ok) {
          setReview(null);
          setFailure(result.failure);
          return;
        }
        setFailure(null);
        setReview(readReview(result.data));
      })();
    },
    [entity, row.column],
  );

  return (
    <section className="card">
      <SchemaForm
        caption={`The rule for ${row.column}`}
        schema={schema}
        uiSchema={COLUMN_EDIT_UI}
        formData={defaults}
        failure={failure}
        busy={busy}
        onSubmit={ask}
      />
      <p className="note">{THERE_IS_NO_SAVE}</p>
      {review === null ? null : <ReviewNotice review={review} />}
    </section>
  );
}

/**
 * One classification, and the editor when a column is open.
 *
 * A separate component because a hook cannot be called conditionally and there is no request
 * to make until an entity has been named. Merging the two would mean asking the API for the
 * classification of the empty string every time somebody opened the screen.
 */
function ClassifiedColumns({
  entity,
  openColumn,
}: {
  readonly entity: string;
  readonly openColumn: string | undefined;
}) {
  const answer = useResource<unknown>(classificationApiPath(entity));
  const page = useMemo(() => readClassification(answer.data), [answer.data]);

  const columns = useMemo(
    () =>
      classificationColumns(page.editable, (column) => (
        <Link to={columnAddress(entity, column)}>Edit</Link>
      )),
    [page.editable, entity],
  );

  const open = openColumn === undefined ? null : columnByName(page.columns, openColumn);
  const options = useMemo(
    () => (open === null ? [] : derivationOptions(page.columns, open.column)),
    [page.columns, open],
  );

  return (
    <>
      <DataTable
        caption={`How the columns of ${entity} are classified`}
        columns={columns}
        rows={page.columns}
        rowId={(row) => row.column}
        failure={answer.failure}
        busy={answer.busy}
      />

      {/*
       * The editor appears when a column is open and this caller may have a change
       * reviewed. When they may not, nothing is rendered and nothing is said: a sentence
       * explaining that they cannot would be this console describing a refusal the API never
       * made, and the API's refusal, when a review is attempted, is the same one it gives
       * somebody who cannot read the classification at all.
       */}
      {open !== null && page.editable ? (
        <ColumnEditor entity={entity} row={open} options={options} />
      ) : null}

      {openColumn !== undefined && open === null && !answer.busy && answer.failure === null ? (
        <p className="note">{NO_SUCH_COLUMN}</p>
      ) : null}
    </>
  );
}

export function Classification() {
  const { entity, column } = useParams();
  const navigate = useNavigate();

  // Memoised on the address rather than rebuilt each render, so the form is not handed a new
  // object while somebody is typing into it.
  const asked = useMemo(() => ({ [ENTITY_FIELD]: entity ?? "" }), [entity]);

  return (
    <article className="page">
      <h1>Classification</h1>
      <p className="lede">
        Which columns of a document are confidential, what it takes to see each, and which
        ones can be worked out from the others.
      </p>

      <section className="card">
        <SchemaForm
          caption="Which document"
          schema={CLASSIFICATION_QUERY_SCHEMA}
          uiSchema={CLASSIFICATION_QUERY_UI}
          formData={asked}
          onSubmit={(submitted) => {
            const named = submittedEntity(submitted);
            if (named === null) {
              // Not a question this screen recognises. An address assembled out of a value
              // nobody read is a request nobody meant to make.
              return;
            }
            void navigate(classificationAddress(named));
          }}
        />
      </section>

      {entity === undefined ? (
        <p className="note">{NOTHING_ASKED_FOR}</p>
      ) : (
        // Keyed by the entity, so opening a different document starts from a fresh request
        // and a fresh editor rather than showing the previous classification's columns while
        // the new answer is in flight.
        <ClassifiedColumns key={entity} entity={entity} openColumn={column} />
      )}
    </article>
  );
}

/**
 * The sentences this page adds to what the API said, exported so a test can assert on them
 * rather than on a copy. None carries a number and none explains a refusal.
 */
export {
  IT_DOES_NOT_WIDEN,
  IT_WIDENS,
  IT_WOULD_NOT_LOAD,
  NOTHING_ASKED_FOR,
  NOTHING_WOULD_CHANGE,
  NO_SUCH_COLUMN,
  THERE_IS_NO_SAVE,
  WHAT_A_WIDENING_MEANS,
};
