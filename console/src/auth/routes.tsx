/**
 * The two routes whose paths are registered in Keycloak, and are therefore not free
 * choices: `/auth/callback` and `/signed-out`. See `constants.ts` for the registered
 * values and what happens when a path stops matching one.
 *
 * Both sit outside `RequireSession`. The callback is where a session is created, so
 * guarding it would be a loop; the signed-out page exists precisely because there is no
 * session, so guarding it would sign the person straight back in and undo the sign-out
 * they just performed.
 *
 * **The callback URL is replaced rather than pushed.** The authorisation code is in the
 * query string, it is single use, and it must not sit in the browser's history where the
 * back button will replay it. Replacing the entry also means "back" from the first page
 * goes wherever the person came from rather than to a spent callback.
 */

import { useEffect, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { Notice } from "../ui/Notice";
import { completeSignIn } from "./session";

export function CallbackRoute() {
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const [problem, setProblem] = useState("");

  useEffect(() => {
    let cancelled = false;
    completeSignIn(params).then(
      (returnTo) => {
        if (!cancelled) {
          navigate(returnTo, { replace: true });
        }
      },
      (error: unknown) => {
        if (!cancelled) {
          setProblem(
            error instanceof Error ? error.message : "Sign-in did not complete.",
          );
        }
      },
    );
    return () => {
      cancelled = true;
    };
  }, [params, navigate]);

  if (problem) {
    return (
      <div className="centred-panel">
        <Notice title="Could not sign you in">
          <p>{problem}</p>
          <p>
            Go back to the console&rsquo;s address and start again. If this keeps
            happening, the console&rsquo;s redirect address may not be registered for the
            address you are using.
          </p>
        </Notice>
      </div>
    );
  }

  return (
    <div className="centred-panel">
      <Notice title="Finishing sign-in">
        <p>One moment.</p>
      </Notice>
    </div>
  );
}

/**
 * Where Keycloak sends the browser after ending the session.
 *
 * It offers a way back in and nothing else. In particular it does not start a sign-in
 * automatically: somebody who has just signed out on a shared machine and is immediately
 * signed back in has not signed out, and the SSO cookie is gone by now anyway, so the
 * automatic version would put them on a login form they did not ask for.
 */
export function SignedOutRoute() {
  return (
    <div className="centred-panel">
      <Notice title="Signed out">
        <p>Your session has ended.</p>
        <a className="button" href="/">
          Sign in again
        </a>
      </Notice>
    </div>
  );
}
