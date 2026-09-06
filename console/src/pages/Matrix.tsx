/**
 * The routing matrix, and the editor for one rung of it.
 *
 * `brain.models.routing.RoutingChain` says why this screen exists: "In Postgres this is
 * `routing_rung`, editable from the console at runtime. Tier assignment changes roughly
 * monthly as providers ship models, and a change that needs an engineer and a release is a
 * change that stops happening, after which the pools rot." Until this screen the table was
 * reachable by nothing, so every timeout was still a deploy.
 *
 * **The address is the whole of the state.** `/routing` is the matrix and
 * `/routing/{rungId}` is the matrix with one rung's editor open, so a person can send a
 * colleague the rung they are arguing about. Holding the open rung in component state
 * instead would make the editor unlinkable and would put the back button somewhere it does
 * not belong.
 *
 * **Nothing here decides what may be seen or changed.** The request goes out identically for
 * every caller; the API answers from grants this browser never receives; a refusal comes
 * back as a value and is rendered in the API's own words. `editable` on the response decides
 * whether an edit control is drawn and decides nothing else, and every save is refused or
 * accepted by the route whatever this file believed. See `A_HIDDEN_EDITOR_IS_NOT_A_REFUSAL`.
 *
 * **The screen says nothing about how many rungs there are.** Not a total, not a page
 * number, not the count of what arrived. The grid holds that rule for the table; this file
 * holds it for everything around the table, which is where a heading like "4 rungs" would
 * go. What it does say, when the page came back full, is that there is more, in a sentence
 * with no number in it: that is `truncated`, and it is a flag rather than an arithmetic.
 *
 * **A saved edit is followed by a fresh request rather than by a local update.** The rows
 * component is keyed on a counter this file bumps, so a successful save remounts it and it
 * asks again. Patching the row in place would show what the console sent, and the whole
 * reason `brain.routing_routes.apply_edit` returns the stored row is that the value written
 * and the value stored are about to stop being the same thing: M5.3.2 derives `role` on
 * write, so a console trusting its own request would report a label the database does not
 * hold.
 *
 * **The form is a form rather than four boxes, and the argument is the bounds in it.**
 * `PATCH /api/v1/routing/rungs/{rung_id}` bounds all four fields, and a hand-written control
 * would be a third copy of those numbers after the route and its document. The copy nobody
 * keeps in step is the one that offers a person a number the API refuses, which returns
 * `HTTPValidationError` rather than `ErrorBody` and therefore reads as "Something went
 * wrong." The schema is in `matrixQuery.ts` and its numbers are checked against the route's
 * own description.
 *
 * **The form library is the records screen's, so this route is code-split too.**
 * `@rjsf/core` with the ajv validator is the largest thing in this console by a wide margin,
 * and a second eager import of it would put it back in the entry chunk for everybody
 * including a person who only opens the overview. `App.tsx` loads this route on demand and
 * `tests/bundle-split.test.ts` walks the static import graph to prove it.
 *
 * Task ids: M5.3.3
 */

import { useCallback, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { request } from "../api/client";
import type { ApiFailure } from "../api/errors";
import { useResource } from "../api/useResource";
import { DataTable } from "../components/DataTable";
import { SchemaForm } from "../components/SchemaForm";
import {
  editableDefaults,
  matrixApiPath,
  matrixColumns,
  readMatrixPage,
  RUNG_EDIT_SCHEMA,
  RUNG_EDIT_UI,
  rungAddress,
  rungApiPath,
  rungById,
  submittedEdit,
  type RungRow,
} from "./matrixQuery";

/**
 * What is said when the page came back full.
 *
 * No number, and none available to put in one: `readMatrixPage` keeps a flag. The route
 * sends no cursor, so raising what this screen asks for is a change to `MATRIX_PAGE_SIZE`
 * rather than something a person can do, and the sentence says what is true rather than
 * offering an action that does not exist.
 */
const THERE_IS_MORE = "This page came back full, so there are more rungs than it shows.";

/**
 * What is said when the address names a rung the page does not carry.
 *
 * About this page and not about the matrix, which is what makes it safe to say at all.
 * Every reader of the matrix is answered every live rung, so a rung absent from the grid is
 * absent from the live matrix and the reader can see the grid; there is no hidden set for
 * this sentence to describe. The same sentence on a records screen would be a disclosure,
 * which is why it is written here rather than in a shared component.
 */
const NO_SUCH_RUNG = "No rung on this page has that id.";

/**
 * One rung's editor.
 *
 * A separate component because it holds the state of one save in flight and the matrix does
 * not. Merging the two would put a busy flag belonging to a PATCH on a component whose other
 * job is rendering a GET, and the first person to reuse it would find the grid greyed out
 * while somebody typed.
 */
function RungEditor({
  rung,
  onSaved,
}: {
  readonly rung: RungRow;
  readonly onSaved: () => void;
}) {
  const [failure, setFailure] = useState<ApiFailure | null>(null);
  const [busy, setBusy] = useState(false);

  const save = useCallback(
    (submitted: unknown) => {
      const edit = submittedEdit(submitted);
      if (edit === null) {
        // Not an edit this screen recognises. Doing nothing is the answer: a PATCH built out
        // of values nobody read is a write nobody meant to make, and this is the one screen
        // here where a wrong request changes something.
        return;
      }
      setBusy(true);
      void (async () => {
        const result = await request<unknown>(rungApiPath(rung.id), {
          method: "PATCH",
          body: edit,
        });
        setBusy(false);
        if (!result.ok) {
          setFailure(result.failure);
          return;
        }
        setFailure(null);
        // The answer is discarded and the matrix is asked again. The route returns the
        // stored row and this could render it, but then the row in the grid and the row in
        // the form would be two copies of one thing that can disagree, and the one a reader
        // trusts would be whichever was on the screen.
        onSaved();
      })();
    },
    [rung.id, onSaved],
  );

  return (
    <section className="card">
      <SchemaForm
        caption={`Rung ${rung.tier} position ${String(rung.position)}`}
        schema={RUNG_EDIT_SCHEMA}
        uiSchema={RUNG_EDIT_UI}
        formData={editableDefaults(rung)}
        failure={failure}
        busy={busy}
        onSubmit={save}
      />
    </section>
  );
}

/**
 * The matrix itself.
 *
 * A separate component so that a successful save can remount it by key and it asks the API
 * again. `useResource` re-runs on a change of path, and the path must not change: it is the
 * request, and a console that varied its request to force a refresh would be asking a
 * different question to get the same answer.
 */
function MatrixRows({
  openRungId,
  onSaved,
}: {
  readonly openRungId: string | undefined;
  readonly onSaved: () => void;
}) {
  const answer = useResource<unknown>(matrixApiPath());
  const page = useMemo(() => readMatrixPage(answer.data), [answer.data]);

  const columns = useMemo(
    () =>
      matrixColumns(page.editable, (rungId) => (
        <Link to={rungAddress(rungId)}>Edit</Link>
      )),
    [page.editable],
  );

  const open = openRungId === undefined ? null : rungById(page.rungs, openRungId);

  return (
    <>
      <DataTable
        caption="The routing matrix"
        columns={columns}
        rows={page.rungs}
        rowId={(rung) => rung.id}
        failure={answer.failure}
        busy={answer.busy}
      />

      {page.truncated ? <p className="note">{THERE_IS_MORE}</p> : null}

      {/*
       * The editor appears when a rung is open and this caller may change it. When they may
       * not, nothing is rendered and nothing is said: a sentence explaining that they cannot
       * edit would be this console describing a refusal the API never made, and the API's
       * refusal, when a save is attempted, is the same one it gives somebody who cannot read
       * the matrix at all.
       */}
      {open !== null && page.editable ? <RungEditor rung={open} onSaved={onSaved} /> : null}

      {openRungId !== undefined && open === null && !answer.busy && answer.failure === null ? (
        <p className="note">{NO_SUCH_RUNG}</p>
      ) : null}
    </>
  );
}

export function Matrix() {
  const { rungId } = useParams();
  // A counter rather than a boolean, because two saves in a row must remount twice. Its
  // value is never rendered: it is a key, and a key that reached the screen would be a
  // number describing how many times somebody had saved.
  const [generation, setGeneration] = useState(0);
  const onSaved = useCallback(() => {
    setGeneration((current) => current + 1);
  }, []);

  return (
    <article className="page">
      <h1>Routing matrix</h1>
      <p className="lede">
        Which model handles a request, in what order, and what each rung is allowed to spend.
      </p>

      <MatrixRows key={generation} openRungId={rungId} onSaved={onSaved} />
    </article>
  );
}

/**
 * The two sentences this page adds to what the API said, exported so a test can assert on
 * them rather than on a copy. Neither carries a number and neither explains a refusal.
 */
export { THERE_IS_MORE, NO_SUCH_RUNG };
