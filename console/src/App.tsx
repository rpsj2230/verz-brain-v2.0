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
 */

import { createBrowserRouter, Link, RouterProvider } from "react-router-dom";
import { CALLBACK_PATH, SIGNED_OUT_PATH } from "./auth/constants";
import { RequireSession } from "./auth/RequireSession";
import { CallbackRoute, SignedOutRoute } from "./auth/routes";
import { configProblems } from "./config";
import { Shell } from "./layout/Shell";
import { Activity } from "./pages/Activity";
import { NotFound } from "./pages/NotFound";
import { Overview } from "./pages/Overview";
import { Notice } from "./ui/Notice";

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

const router = createBrowserRouter([
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
      { path: "activity", element: <Activity /> },
      { path: "*", element: <NotFound /> },
    ],
  },
]);

export function App() {
  if (configProblems.length > 0) {
    return <ConfigurationProblems />;
  }
  return <RouterProvider router={router} />;
}
