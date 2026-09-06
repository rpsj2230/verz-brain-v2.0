/**
 * The sign-in flow, and the only place a token exists in this console.
 *
 * Authorisation code with PKCE, against the `brain-console` client exactly as
 * `ops/keycloak/realm-export.json` defines it: public client, standard flow only, implicit
 * off, no direct grant, S256 forced. There is no other path in this file and no fallback
 * that would use one.
 *
 * **Tokens live in memory and nowhere else.** No storage, no cookie set by this code. The
 * cost is real and is paid deliberately: reloading the page loses the token, so the
 * console bounces through the identity provider again. That bounce is invisible when the
 * Keycloak SSO session is alive, because it is a top-level navigation carrying a
 * first-party cookie, and it costs a page load when it is not.
 *
 * Rejected: keeping the refresh token in the browser's local store so a reload is
 * seamless. The realm gives an SSO session ten hours, so that would be a ten-hour
 * credential sitting in a place any script on the origin can read, and an XSS bug would
 * stop being a session hijack and start being a portable credential. Rejected also:
 * silent renewal through a hidden iframe, which is the older answer to the same problem
 * and no longer works: browsers block the third-party cookie it depends on.
 *
 * **The token is never read.** Not for a name, not for a role, not for an expiry. The
 * console holds an opaque string, sends it, and does what the API answers.
 * `expires_in` from the token response is used to decide when to refresh, and that is a
 * fact about the response rather than about the token's contents. Reading claims in a
 * browser is how a console acquires a second permission model: a one-line role check
 * against a claim works, and it is a rule the API never agreed to.
 *
 * **Refresh is single-flight, and that is mandatory rather than tidy.** See
 * `REFRESH_MUST_BE_SINGLE_FLIGHT`. What this file does not solve is the same race across
 * two tabs, which have separate memory and cannot await each other's promise. That gap is
 * written down in the README rather than papered over here.
 */

import {
  CALLBACK_PATH,
  KEYCLOAK_CLIENT_ID,
  MAX_SIGN_IN_ATTEMPTS,
  PKCE_CHALLENGE_METHOD,
  REFRESH_SKEW_SECONDS,
  RESPONSE_TYPE,
  SIGNED_OUT_PATH,
} from "./constants";
import { endpoints } from "./discovery";
import {
  challengeFor,
  clearSignInAttempts,
  putPending,
  randomToken,
  recordSignInAttempt,
  takePending,
} from "./pkce";

/**
 * What the console asks for, and why it is only this.
 *
 * `openid` is a scope value a client asks for in a request, not a client scope an
 * administrator defines, which is exactly the distinction the realm file records after a
 * real import logged "Referenced client scope openid doesn't exist. Ignoring". Asking for
 * it is what makes this an OIDC request and produces the ID token used as a logout hint.
 *
 * `brain-identity` is not listed because it is a default client scope on `brain-console`
 * and is therefore applied whether or not it is requested. `profile` and `email` are not
 * listed because this realm defines neither, and nothing in this system reads a name or an
 * email claim: `brain.identity.oidc.ClaimMapping` reads groups and department.
 */
const REQUESTED_SCOPE = "openid";

/** The shape Keycloak returns from the token endpoint. Only these four fields are read. */
interface TokenResponse {
  access_token?: unknown;
  refresh_token?: unknown;
  id_token?: unknown;
  expires_in?: unknown;
}

interface Tokens {
  readonly accessToken: string;
  readonly refreshToken: string;
  /** Held opaquely, used only as `id_token_hint` at sign-out, never parsed. */
  readonly idToken: string;
  /** Epoch milliseconds, already reduced by `REFRESH_SKEW_SECONDS`. */
  readonly refreshAfter: number;
}

export type SessionStatus = "unknown" | "authenticating" | "authenticated" | "failed";

export interface SessionState {
  readonly status: SessionStatus;
  /** Empty unless the status is `failed`. Safe to show; never explains a refusal. */
  readonly message: string;
}

let tokens: Tokens | null = null;
let state: SessionState = Object.freeze({ status: "unknown", message: "" });
const listeners = new Set<() => void>();

function setState(next: SessionState): void {
  // Replaced rather than mutated, and only on a real change. `useSyncExternalStore`
  // compares snapshots by identity, so a new object for an unchanged value is an infinite
  // render loop and a mutated object is a change React never sees.
  if (next.status === state.status && next.message === state.message) {
    return;
  }
  state = Object.freeze(next);
  for (const listener of listeners) {
    listener();
  }
}

export function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

export function getSessionState(): SessionState {
  return state;
}

function redirectUri(): string {
  return `${globalThis.location.origin}${CALLBACK_PATH}`;
}

/**
 * Where to send the person after sign-in, made safe.
 *
 * Only a path on this origin. A value beginning with two slashes is protocol relative and
 * navigates off-site, which is an open redirect however unlikely the route into it, and
 * "it came from our own storage" is the reasoning that makes those bugs live for years.
 */
function safeReturnTo(candidate: string): string {
  return candidate.startsWith("/") && !candidate.startsWith("//") ? candidate : "/";
}

function currentLocation(): string {
  const { pathname, search, hash } = globalThis.location;
  return safeReturnTo(`${pathname}${search}${hash}`);
}

/**
 * Send the browser to Keycloak. Nothing after this call runs in this page.
 *
 * The attempt counter is incremented immediately before leaving, so a callback that fails
 * for a stable reason stops after `MAX_SIGN_IN_ATTEMPTS` instead of ping-ponging between
 * two hosts for ever.
 */
export async function beginSignIn(returnTo = currentLocation()): Promise<void> {
  // Already leaving. React's strict mode runs an effect, tears it down and runs it again,
  // so the guard that starts sign-in fires twice on every mount in development. Without
  // this line that is two attempts against a counter that allows three, and the loop guard
  // trips on the first genuine failure instead of the third.
  if (state.status === "authenticating") {
    return;
  }
  if (recordSignInAttempt() > MAX_SIGN_IN_ATTEMPTS) {
    setState({
      status: "failed",
      message:
        "Sign-in did not complete after several attempts, so the console stopped trying. " +
        "Reload to try again, and if it keeps happening the redirect URI registered for " +
        "this console probably does not match the address you are using.",
    });
    return;
  }

  setState({ status: "authenticating", message: "" });
  try {
    const { authorization } = await endpoints();
    const verifier = randomToken();
    const signInState = randomToken();
    putPending({ verifier, state: signInState, returnTo });

    const url = new URL(authorization);
    url.searchParams.set("client_id", KEYCLOAK_CLIENT_ID);
    url.searchParams.set("response_type", RESPONSE_TYPE);
    url.searchParams.set("redirect_uri", redirectUri());
    url.searchParams.set("scope", REQUESTED_SCOPE);
    url.searchParams.set("state", signInState);
    url.searchParams.set("code_challenge", await challengeFor(verifier));
    url.searchParams.set("code_challenge_method", PKCE_CHALLENGE_METHOD);
    // No `nonce`. A nonce binds an ID token to this request, and it is worth exactly what
    // the check on the way back is worth. This console never reads the ID token, so a
    // nonce it never verifies would be decoration that reads as protection. Anything that
    // starts reading the ID token must add both halves in the same change.
    globalThis.location.assign(url.toString());
  } catch (error) {
    setState({ status: "failed", message: describe(error) });
  }
}

function describe(error: unknown): string {
  return error instanceof Error ? error.message : "Sign-in failed for an unknown reason.";
}

function readTokens(payload: TokenResponse, now: number): Tokens {
  const accessToken = typeof payload.access_token === "string" ? payload.access_token : "";
  if (!accessToken) {
    throw new Error("The identity provider returned no access token.");
  }
  const lifetime = typeof payload.expires_in === "number" ? payload.expires_in : 0;
  return {
    accessToken,
    refreshToken: typeof payload.refresh_token === "string" ? payload.refresh_token : "",
    idToken: typeof payload.id_token === "string" ? payload.id_token : "",
    // A response with no `expires_in` is treated as already due for refresh rather than as
    // valid for ever. Erring towards one wasted refresh is cheaper than erring towards a
    // dead token being sent until the API rejects it.
    refreshAfter: now + Math.max(0, lifetime - REFRESH_SKEW_SECONDS) * 1000,
  };
}

async function postToTokenEndpoint(body: URLSearchParams): Promise<Tokens> {
  const { token } = await endpoints();
  const response = await fetch(token, {
    method: "POST",
    headers: { "content-type": "application/x-www-form-urlencoded" },
    // No cookies. The token endpoint authenticates this request with the PKCE verifier or
    // the refresh token in the body; sending ambient credentials to a cross-origin
    // endpoint adds nothing and widens what a mistake could do.
    credentials: "omit",
    body,
  });
  const payload: unknown = await response.json().catch(() => null);
  if (!response.ok) {
    const detail =
      typeof payload === "object" && payload !== null && "error" in payload
        ? String((payload as { error: unknown }).error)
        : String(response.status);
    // The OAuth error code is about this console's own request and says nothing about the
    // company's data, so quoting it is safe and saves an afternoon. `invalid_grant` here
    // is almost always an expired code, a reused one, or a clock that disagrees.
    throw new Error(`The identity provider refused the token request (${detail}).`);
  }
  return readTokens((payload ?? {}) as TokenResponse, Date.now());
}

let completing: Promise<string> | null = null;

/**
 * Finish the flow on the callback route. Returns where the person was going.
 *
 * The `state` comparison is the CSRF check for this flow: it proves the callback belongs
 * to a sign-in this tab started. `takePending` deletes the stored values as it reads them,
 * so a code cannot be redeemed twice and a stale `state` cannot validate a later callback.
 */
export function completeSignIn(params: URLSearchParams): Promise<string> {
  // Memoised and never reset, for the same reason `beginSignIn` guards itself: strict mode
  // runs the callback effect twice, and the first run has already consumed the stored
  // verifier and redeemed the code. The second run would find nothing pending and report
  // that this callback does not belong here, overwriting a sign-in that had just worked.
  // A page completes at most one sign-in, so one promise is the whole lifetime.
  completing ??= exchangeCode(params);
  return completing;
}

async function exchangeCode(params: URLSearchParams): Promise<string> {
  const pending = takePending();
  const error = params.get("error");
  if (error) {
    // Keycloak's own message, for example `access_denied` when somebody cancels. It
    // describes the sign-in and never the data behind it.
    throw new Error(`Sign-in did not complete (${error}).`);
  }
  const code = params.get("code");
  const returnedState = params.get("state");
  if (!pending) {
    throw new Error(
      "This callback does not match a sign-in started in this tab. Start again from the " +
        "console's address.",
    );
  }
  if (!code || returnedState !== pending.state) {
    throw new Error("The sign-in response did not match the request that started it.");
  }

  const body = new URLSearchParams({
    grant_type: "authorization_code",
    code,
    redirect_uri: redirectUri(),
    client_id: KEYCLOAK_CLIENT_ID,
    code_verifier: pending.verifier,
  });
  tokens = await postToTokenEndpoint(body);
  clearSignInAttempts();
  setState({ status: "authenticated", message: "" });
  return safeReturnTo(pending.returnTo);
}

/**
 * The one in-flight refresh. Every caller awaits this same promise, because presenting a
 * refresh token twice against a realm with `refreshTokenMaxReuse` 0 ends the session.
 */
let refreshing: Promise<Tokens> | null = null;

async function refresh(current: Tokens): Promise<Tokens> {
  const body = new URLSearchParams({
    grant_type: "refresh_token",
    refresh_token: current.refreshToken,
    client_id: KEYCLOAK_CLIENT_ID,
  });
  const next = await postToTokenEndpoint(body);
  tokens = next;
  return next;
}

/**
 * A usable access token, refreshed if it is close enough to expiry to matter.
 *
 * Returns null when there is no session, rather than redirecting from inside a data path.
 * A fetch helper that navigates away is a fetch helper that discards a half-written
 * question, so the decision to leave the page belongs to the caller.
 */
export async function accessToken(): Promise<string | null> {
  if (!tokens) {
    return null;
  }
  if (Date.now() < tokens.refreshAfter) {
    return tokens.accessToken;
  }
  if (!tokens.refreshToken) {
    return null;
  }
  refreshing ??= refresh(tokens).finally(() => {
    refreshing = null;
  });
  try {
    return (await refreshing).accessToken;
  } catch {
    // The refresh token is spent, revoked, or the session ended elsewhere. Forget
    // everything and let the caller decide whether to send the person back to sign in.
    tokens = null;
    setState({ status: "unknown", message: "" });
    return null;
  }
}

/** Whether a token is currently held. Not a permission check and not close to one. */
export function isSignedIn(): boolean {
  return tokens !== null;
}

/**
 * Drop the held tokens without leaving the page.
 *
 * Called when the API refuses a token we believed was good, which is the case a client
 * cannot reason about on its own: the session may have been ended elsewhere, the token may
 * be minted for the wrong audience, the clock may be wrong. The console does not guess
 * which. It forgets, the guard notices the status change and starts a fresh sign-in, and
 * the attempt counter stops that becoming a loop if the cause is permanent.
 */
export function forgetSession(): void {
  tokens = null;
  setState({ status: "unknown", message: "" });
}

/**
 * Leave, and tell Keycloak to end the session rather than only forgetting it here.
 *
 * A local-only sign-out is the bug that looks like a feature: the console forgets the
 * token, the SSO session at the identity provider is untouched, and the next visit signs
 * straight back in without a prompt. On a shared machine that is not a sign-out at all.
 *
 * `id_token_hint` is sent when there is one, because it identifies the session being ended
 * and lets Keycloak skip its confirmation page. When there is not, `client_id` is sent
 * instead: Keycloak 26 requires one or the other before it will honour
 * `post_logout_redirect_uri`, and without either the person lands on a Keycloak page
 * rather than back here.
 */
export async function signOut(): Promise<void> {
  const held = tokens;
  tokens = null;
  setState({ status: "unknown", message: "" });

  const { endSession } = await endpoints();
  if (!endSession) {
    globalThis.location.assign(SIGNED_OUT_PATH);
    return;
  }
  const url = new URL(endSession);
  url.searchParams.set(
    "post_logout_redirect_uri",
    `${globalThis.location.origin}${SIGNED_OUT_PATH}`,
  );
  if (held?.idToken) {
    url.searchParams.set("id_token_hint", held.idToken);
  } else {
    url.searchParams.set("client_id", KEYCLOAK_CLIENT_ID);
  }
  globalThis.location.assign(url.toString());
}
