/**
 * A second route, so the shell has something to route between and the navigation has a
 * state to be in other than "current page".
 *
 * It is empty on purpose. Filling it with a placeholder table of invented rows would make
 * the console look further along than it is, and somebody would screenshot it.
 */

export function Activity() {
  return (
    <article className="page">
      <h1>Activity</h1>
      <p className="lede">Not built yet.</p>
      <p>
        Activity will show what has been asked and answered, within whatever the person
        reading it is entitled to see. Nothing about that is decided here: the console asks
        the API a question and renders the answer it is given.
      </p>
    </article>
  );
}
