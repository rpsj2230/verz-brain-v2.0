/**
 * The first page after sign-in, and today an honest statement that there is nothing here
 * yet.
 *
 * It fetches nothing. The API has no route mounted under its versioned prefix, so a page
 * that showed a spinner and an empty list would be inventing a state the system cannot be
 * in. An empty console that says it is empty is easier to work with than one that looks
 * broken.
 *
 * The lock sample is deliberate and temporary. `Lock` is the component that renders a
 * withheld field, it has to be legible in both themes, and until records render there is
 * nowhere else to see it. It is labelled as an example so nobody reads it as data.
 * **Delete this panel when a real record renders anywhere.**
 */

import { Lock } from "../ui/Lock";

export function Overview() {
  return (
    <article className="page">
      <h1>Overview</h1>
      <p className="lede">
        The console shell is in place. The screens that go inside it are not built yet.
      </p>

      <section className="card">
        <h2>What works</h2>
        <ul>
          <li>Sign-in through your organisation&rsquo;s identity provider.</li>
          <li>A light and dark theme that follows this machine unless you say otherwise.</li>
          <li>
            A typed client for the API, generated from the API&rsquo;s own description at
            build time.
          </li>
        </ul>
      </section>

      <section className="card">
        <h2>Example: a withheld field</h2>
        <p>
          When you may see a record but not one of its fields, the field renders like this,
          identically for everybody:
        </p>
        <dl className="fields">
          <div className="fields__row">
            <dt>Contract value</dt>
            <dd>
              <Lock />
            </dd>
          </div>
        </dl>
        <p className="note">
          An example, not a record. Nothing on this page was fetched from anywhere.
        </p>
      </section>
    </article>
  );
}
