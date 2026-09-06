/**
 * The route table, and the two things that sit outside it.
 *
 * **Configuration is checked before anything renders.** A console pointed at no identity
 * provider cannot sign anybody in, and the useful thing to do about that is say so on the
 * screen, naming the variable, to the person who deployed it. The alternative is a
 * redirect to `undefined/.well-known/openid-configuration` and a browser error nobody can
 * act on.
 *
 * **The sign-in routes sit outside the guard.** `/auth/callback` is where a session comes
 * from, so guarding it would be a loop; `/signed-out` exists because there is no session,
 * so guarding it would sign the person back in and undo what they just did. Their paths
 * are registered in Keycloak and are not free choices; see `src/auth/constants.ts`.
 *
 * **Client-side routing needs the server to co-operate.** Every path here is served by the
 * same `index.html`, so whatever hosts the built files must return `index.html` for an
 * unknown path rather than a 404. Without that, a deep link fails and, worse, so does
 * `/auth/callback`, which means sign-in completes at the identity provider and then lands
 * on a page that does not exist. The README says this again where a deployer will see it.
 *
 * **The records route is loaded on demand, and this is the one place that decision is
 * expressed.** It is the only route that mounts the table library and the form library, and
 * those weigh 608 kB against an application of 267 kB. Loaded eagerly they are in the first
 * response for everybody, including a person who only ever opens the overview, and the
 * download happens before the sign-in redirect has even been decided. A static import here
 * is therefore the change that undoes the split: `tests/bundle-split.test.ts` walks the
 * static import graph from `main.tsx` and fails when either library is reachable without a
 * dynamic import. The measurement is in the README.
 *
 * Rejected: splitting every route. `Overview` and `NotFound` reach nothing the shell does
 * not already reach, so a chunk for either buys a round trip and saves no bytes. A split is
 * worth what it removes from the entry, and these remove nothing.
 */

import { lazy } from "react";
import {
  createBrowserRouter,
  Link,
  RouterProvider,
  type RouteObject,
} from "react-router-dom";
import { CALLBACK_PATH, SIGNED_OUT_PATH } from "./auth/constants";
import { RequireSession } from "./auth/RequireSession";
import { CallbackRoute, SignedOutRoute } from "./auth/routes";
import { configProblems } from "./config";
import { Shell } from "./layout/Shell";
import { NotFound } from "./pages/NotFound";
import { Overview } from "./pages/Overview";
import { Notice } from "./ui/Notice";

/**
 * The records screen, fetched when somebody asks for it.
 *
 * Written as a dynamic import with a named export rather than a default one, because every
 * module in this console exports by name and a single default export here would be the one
 * exception a reader has to notice. `Shell` supplies the boundary this suspends against, so
 * the frame and the navigation paint before the chunk arrives: a menu that waited for a
 * page's code would be a menu whose contents depended on a network request, which is the
 * shape the navigation is not allowed to have.
 */
const Records = lazy(async () => ({ default: (await import("./pages/Records")).Records }));

/**
 * The routing matrix, fetched when somebody asks for it, for the same reason and by the same
 * measurement.
 *
 * It mounts the same two libraries the records screen does, so an eager import here would
 * undo the split whatever `Records` did: the chunk would simply arrive through this module
 * instead. `tests/bundle-split.test.ts` walks the static graph from `main.tsx` and does not
 * care which route reached the library.
 */
const Matrix = lazy(async () => ({ default: (await import("./pages/Matrix")).Matrix }));

/**
 * Shown when a page throws while rendering.
 *
 * It deliberately does not print the error. A rendering failure is a bug in this console,
 * and the details belong in the browser's own console where a developer will look, not on
 * a page in front of somebody who cannot act on them and might screenshot them into a
 * chat. The reference a person needs for a support conversation is the trace id on a
 * failed request, which is a different thing and is shown where it exists.
 */
function RouteError() {
  return (
    <div className="centred-panel">
      <Notice title="Something went wrong on this page">
        <p>Reloading may help. If it keeps happening, this is a bug in the console.</p>
        <p>
          <Link to="/">Back to the overview</Link>
        </p>
      </Notice>
    </div>
  );
}

function ConfigurationProblems() {
  return (
    <div className="centred-panel">
      <Notice title="This console is not configured">
        <ul>
          {configProblems.map((problem) => (
            <li key={problem}>{problem}</li>
          ))}
        </ul>
        <p className="note">
          These are build-time settings. See <code>console/.env.example</code>.
        </p>
      </Notice>
    </div>
  );
}

/**
 * The routes themselves, separated from the router that mounts them.
 *
 * The table is data and the browser router is one binding of it. Exporting the data means
 * a test can mount the same table on a memory router and ask what a given address renders,
 * which is the only way to check that a deep link resolves and that an unknown one reaches
 * the console's own not-found page. A test that declared its own copy of this table would
 * be testing the copy.
 */
export const routes: RouteObject[] = [
  { path: CALLBACK_PATH, element: <CallbackRoute />, errorElement: <RouteError /> },
  { path: SIGNED_OUT_PATH, element: <SignedOutRoute />, errorElement: <RouteError /> },
  {
    path: "/",
    element: (
      <RequireSession>
        <Shell />
      </RequireSession>
    ),
    errorElement: <RouteError />,
    children: [
      { index: true, element: <Overview /> },
      // Two paths and one component. The entity is a path segment rather than a query
      // parameter because it is what the screen is about, and the same screen with none
      // named is where somebody arrives from the menu: it has the form and no grid, because
      // there is no question to ask yet.
      { path: "records", element: <Records /> },
      { path: "records/:entity", element: <Records /> },
      // Two paths and one component again. The rung being edited is a path segment because
      // it is what the screen is about, and the same screen with none named is the matrix on
      // its own: it has the grid and no form, because no rung has been opened.
      { path: "routing", element: <Matrix /> },
      { path: "routing/:rungId", element: <Matrix /> },
      { path: "*", element: <NotFound /> },
    ],
  },
];

const router = createBrowserRouter(routes);

export function App() {
  if (configProblems.length > 0) {
    return <ConfigurationProblems />;
  }
  return <RouterProvider router={router} />;
}
