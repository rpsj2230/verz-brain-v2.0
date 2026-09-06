/**
 * Rows of one entity, as the API answered them. The first screen in this console that
 * shows anybody's data.
 *
 * **The address is the whole of the state.** The entity is a path segment and the limit is
 * a query parameter, so a person can send a colleague a link and the colleague gets the
 * same question asked with their own grants, which is the only sense in which two people
 * here see "the same screen". Holding the entity in component state instead would make
 * every screen unlinkable and would put the back button somewhere it does not belong.
 *
 * **Nothing here decides what may be seen.** The path and the limit go out as written; the
 * API answers from grants this browser never receives; a refusal comes back as a value and
 * is rendered in the API's own words. That is the point of the screen rather than a caveat
 * on it. `brain.api_routes` answers an entity nothing classifies, an entity with no tool,
 * an ambiguous one and one whose rows this caller reaches no column of with one 404 and one
 * sentence, so a person typing names cannot map the installation. This file must not undo
 * that by being helpful about which of those happened, and the way a console breaks it is
 * never by writing "access denied": it is by adding "check the spelling" underneath.
 *
 * **The screen says nothing about how many rows there are, or were.** Not a total, not a
 * page number, not a range, and not the count of what arrived. The grid holds that rule for
 * the table itself; this file holds it for everything around the table, which is where a
 * heading like "3 records" would go.
 *
 * **A person cannot tell that a page was cut short, and that is a gap rather than a rule.**
 * `RecordPage` carries `truncated`, which says there is more without saying how much more,
 * and `readPage` in `paging.ts` keeps two fields and drops it on the way in. There is
 * nowhere for this screen to read it from, so somebody looking at twenty-five rows of a
 * larger set has no sign of it. What they do have is the limit, in the address and in the
 * form, which they can raise to five hundred. Carrying `truncated` through the envelope,
 * the hook and the grid is the fix, and it is three modules and their tests, which is not a
 * change to make in the commit that first mounts any of them.
 *
 * **The form is a form rather than an input box, and the argument is the two numbers in
 * it.** `GET /api/v1/records/{entity}` bounds `limit` at one and five hundred. A
 * hand-written control would be a third copy of those bounds, after the route and its
 * document, and the copy nobody keeps in step is the one that offers a person a number the
 * API refuses, which returns `HTTPValidationError` rather than `ErrorBody` and therefore
 * reads as "Something went wrong." The schema is in `recordsQuery.ts` and its numbers are
 * checked against the route's own description. What is honestly not proved by mounting it:
 * no route sends a schema, so the lock path through `formShape` is exercised by tests and
 * not by a person. See the README.
 *
 * **The form costs more than the rest of this console put together.** `@rjsf/core` with the
 * ajv validator is 608 kB against an application of 267 kB, so this route is code-split in
 * `App.tsx` and nothing here is in the entry chunk. The measurement, before and after, is
 * in the README.
 *
 * Task ids: M32.5.1.2, M32.5.2.1, M32.5.2.2
 */

import { useMemo } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import { DataTable } from "../components/DataTable";
import { SchemaForm } from "../components/SchemaForm";
import { useServerPage } from "../components/useServerPage";
import {
  columnsFor,
  ENTITY_FIELD,
  LIMIT_FIELD,
  readLimit,
  RECORDS_QUERY_SCHEMA,
  RECORDS_QUERY_UI,
  recordsAddress,
  recordsApiPath,
  rowIdentity,
  submittedQuery,
  type RecordRow,
} from "./recordsQuery";

/**
 * What is shown when no entity has been asked for.
 *
 * It names no entity, which is the whole of what it has to get right. "Try clients or
 * invoices" would be this console publishing a guess at what the company runs, to everybody
 * who can open the page, before anybody asked the API anything.
 */
const NOTHING_ASKED_FOR = "Name an entity above to ask for its rows.";

/**
 * The rows themselves.
 *
 * A separate component because a hook cannot be called conditionally and there is no
 * request to make until an entity has been named. Merging the two would mean asking the API
 * for the rows of the empty string every time somebody opened the screen.
 */
function RecordRows({ entity, limit }: { readonly entity: string; readonly limit: number }) {
  const page = useServerPage<RecordRow>(recordsApiPath(entity), { pageSize: limit });

  // Both are derived from the answer and from nothing else. The columns are the keys that
  // arrived plus the fields the API locked, because a locked key is deleted from the record
  // and would otherwise have no column to render its lock in.
  const columns = useMemo(
    () => columnsFor(page.rows, page.lockedCells),
    [page.rows, page.lockedCells],
  );
  const identity = useMemo(() => rowIdentity(page.rows), [page.rows]);

  return (
    <DataTable
      caption={`Rows of ${entity}`}
      columns={columns}
      rows={page.rows}
      rowId={(row) => identity.get(row) ?? ""}
      lockedCells={page.lockedCells}
      failure={page.failure}
      busy={page.busy}
      hasNext={page.hasNext}
      canGoBack={page.canGoBack}
      onNext={page.showNext}
      onPrevious={page.showPrevious}
      filters={page.filters}
      onFilterChange={page.setFilter}
    />
  );
}

export function Records() {
  const { entity } = useParams();
  const [search] = useSearchParams();
  const navigate = useNavigate();
  const limit = readLimit(search.get(LIMIT_FIELD));

  // Memoised on the address rather than rebuilt each render, so that the form is not handed
  // a new object while somebody is typing into it.
  const asked = useMemo(
    () => ({ [ENTITY_FIELD]: entity ?? "", [LIMIT_FIELD]: limit }),
    [entity, limit],
  );

  return (
    <article className="page">
      <h1>Records</h1>
      <p className="lede">Rows of one entity, exactly as the API answered them.</p>

      <section className="card">
        <SchemaForm
          caption="Which rows to ask for"
          schema={RECORDS_QUERY_SCHEMA}
          uiSchema={RECORDS_QUERY_UI}
          formData={asked}
          onSubmit={(submitted) => {
            const query = submittedQuery(submitted);
            if (query === null) {
              // Not a query this screen recognises. Doing nothing is the answer: an address
              // assembled out of a value nobody read is a request nobody meant to make.
              return;
            }
            void navigate(recordsAddress(query.entity, query.limit));
          }}
        />
      </section>

      {entity === undefined ? (
        <p className="note">{NOTHING_ASKED_FOR}</p>
      ) : (
        // Keyed by the entity, so asking about a different one starts from the first page
        // rather than from wherever the last one had got to. A cursor is a position in one
        // ordering of one filtered set, and carrying it across a change of entity means
        // paging into the middle of a result set that does not exist, which the API would
        // answer with something plausible. It is the same reset `useServerPage` performs on
        // a filter change and for the same reason. Nothing can observe it today, because
        // the route sends no cursor and the pager therefore never leaves the first page.
        <RecordRows key={entity} entity={entity} limit={limit} />
      )}
    </article>
  );
}
