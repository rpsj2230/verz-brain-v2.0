/**
 * An address this console has no page for.
 *
 * **This is not the API's 404 and must not borrow its words.** This one is about the
 * console's own routes, which are the same list in every deployment and are not a fact
 * about anybody's data, so saying plainly that there is no such page discloses nothing.
 *
 * The API's 404 is a different thing entirely: it means either that the record does not
 * exist or that nothing the asker holds reaches it, deliberately indistinguishable. That
 * message is `NOT_FOUND_MESSAGE` in `src/api/errors.ts` and it belongs to responses, not
 * to routes. Using one where the other belongs would eventually teach somebody that the
 * two mean the same thing.
 */

import { Link } from "react-router-dom";

export function NotFound() {
  return (
    <article className="page">
      <h1>No such page</h1>
      <p className="lede">This console has no page at that address.</p>
      <p>
        <Link to="/">Back to the overview</Link>
      </p>
    </article>
  );
}
