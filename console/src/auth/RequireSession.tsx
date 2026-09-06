/**
 * The gate in front of every page that is not part of the sign-in flow.
 *
 * **It is a gate on rendering, not on data.** All it does is make sure a token exists
 * before a page tries to use one. It does not know what the person may see, it cannot
 * know, and nothing about what it renders depends on who they are. Every question about
 * access is answered by the API, per request, from grants this browser never receives.
 * See `THE_CONSOLE_IS_NOT_A_TRUST_BOUNDARY`.
 *
 * Deleting this component would not expose anything. Requests would go out without a
 * bearer token, the API would refuse them, and the console would show refusals instead of
 * a sign-in prompt. That is the test of whether a client-side guard is doing security
 * work: if removing it changes what the server returns, it was never a guard.
 *
 * The effect starts a sign-in whenever the session goes back to "unknown", which happens
 * on a cold load and again if the API ever refuses a token this console believed was
 * current. That second path is what turns a session ended somewhere else into a redirect
 * rather than a screen full of failures.
 */

import { useEffect, useSyncExternalStore, type ReactNode } from "react";
import { Notice } from "../ui/Notice";
import { clearSignInAttempts } from "./pkce";
import { beginSignIn, getSessionState, subscribe } from "./session";

export function RequireSession({ children }: { readonly children: ReactNode }) {
  const session = useSyncExternalStore(subscribe, getSessionState, getSessionState);

  useEffect(() => {
    if (session.status === "unknown") {
      void beginSignIn();
    }
  }, [session.status]);

  if (session.status === "failed") {
    return (
      <div className="centred-panel">
        <Notice title="Could not sign you in">
          <p>{session.message}</p>
          <button
            type="button"
            className="button"
            onClick={() => {
              // A person choosing to retry is not a redirect loop, so the counter that
              // stops the loop is cleared first. Without this the button would appear to
              // do nothing at all, which is worse than the loop it prevents.
              clearSignInAttempts();
              void beginSignIn();
            }}
          >
            Try again
          </button>
        </Notice>
      </div>
    );
  }

  if (session.status !== "authenticated") {
    return (
      <div className="centred-panel">
        <Notice title="Signing you in">
          <p>Taking you to your organisation&rsquo;s sign-in page.</p>
        </Notice>
      </div>
    );
  }

  return <>{children}</>;
}
