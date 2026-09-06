/**
 * The rules the sign-in flow must not break, written as constants so they survive whoever
 * wrote them.
 *
 * Every value here is a fact about `ops/keycloak/realm-export.json`, which was imported
 * and read back against Keycloak 26.0 on 2026-09-06. Where this file and that file
 * disagree, that file is the fact and this one is the bug. The realm is the reason several
 * of these are constants rather than configuration: a value an operator can set is a value
 * that can be set to something the realm refuses, and the failure arrives as a Keycloak
 * error page that names none of the settings involved.
 */

/**
 * The client id, taken from the realm and deliberately not configurable.
 *
 * The realm defines exactly one browser client and this is it. Rejected: reading it from
 * `VITE_KEYCLOAK_CLIENT_ID`, which is what every scaffold does. A client id is not a
 * deployment detail, it is a reference to a specific set of flow settings: `brain-console`
 * is public, standard flow only, implicit off, the password grant off, PKCE forced to
 * S256. Pointing this console at a different client id by changing an environment variable
 * would silently move it to a client with none of that, and nothing in the browser can
 * tell the difference, because the browser is not the thing being protected.
 */
export const KEYCLOAK_CLIENT_ID = "brain-console";

/**
 * Where Keycloak sends the browser back to. This path is half of a registered value.
 *
 * The realm registers `https://brain.example.invalid/auth/callback`, and Keycloak matches
 * `redirect_uri` exactly against that list. The origin comes from wherever the console is
 * actually served; this path must equal the path in the registered URI, and the router
 * must have a route for it. Change one of those three and sign-in stops, so they are named
 * once here and read everywhere else.
 */
export const CALLBACK_PATH = "/auth/callback";

/**
 * Where Keycloak sends the browser after sign-out, from the realm's
 * `post.logout.redirect.uris`. Same rule as the callback: exact match, registered.
 */
export const SIGNED_OUT_PATH = "/signed-out";

/**
 * PKCE, and only this method. The realm sets `pkce.code.challenge.method` to S256, so a
 * request offering `plain` is refused by the server. Sending it anyway would be asking for
 * the weaker of two things and being saved by somebody else's configuration.
 */
export const PKCE_CHALLENGE_METHOD = "S256";

/**
 * The only response type this console ever asks for.
 *
 * `implicitFlowEnabled` is false on this client and on every other client in the realm.
 * The implicit flow returns the token in the URL fragment, which lands in browser history,
 * in referrer headers and in any extension reading the address bar. There is no fallback
 * path in this code that would use it, and adding one would need a realm change, which is
 * a diff somebody reviews.
 */
export const RESPONSE_TYPE = "code";

/**
 * Read together with the realm: `directAccessGrantsEnabled` is false everywhere, so there
 * is no endpoint that accepts a username and a password from this console. That is why
 * this console has no sign-in form and must never grow one: a form here would be a
 * credential prompt on a page that cannot verify it is talking to the identity provider,
 * and it would skip the second factor that `CONFIGURE_TOTP` makes mandatory.
 */
export const THERE_IS_NO_PASSWORD_FORM =
  "The realm accepts no direct grant. Sign-in is a redirect to Keycloak and nothing else. " +
  "A password field in this console would collect a credential the console cannot verify " +
  "and would bypass the second factor the realm requires.";

/**
 * The single most important sentence in this directory.
 *
 * The console gets a token and sends it. Everything about what that token may do is
 * decided by the API, per request, from grants this browser cannot see. Nothing in this
 * console reads a token's contents, and nothing in it decides what may be fetched.
 */
export const THE_CONSOLE_IS_NOT_A_TRUST_BOUNDARY =
  "A console that decides what may be fetched is a second permission model, and the two " +
  "will disagree. The one in the browser is the one an attacker edits. Hiding a control " +
  "is a courtesy; refusing a request is the API's job and only the API's job.";

/**
 * How long an access token lives, from the realm's `accessTokenLifespan`. Informational:
 * the token response carries `expires_in` and that is what the session actually uses. It
 * is written down because five minutes is short enough that refresh is a normal path
 * rather than an edge case, and anybody reasoning about this code needs that number.
 */
export const REALM_ACCESS_TOKEN_LIFESPAN_SECONDS = 300;

/**
 * How early a token is treated as expired. With a five-minute token, thirty seconds is
 * roughly a tenth of its life and comfortably longer than a slow request.
 *
 * The alternative is refreshing on a 401 after the fact, which works and costs the user a
 * visibly failed request first. Both paths exist here: this one is the normal case and the
 * 401 path in `src/api/client.ts` is the safety net for a clock that disagrees.
 */
export const REFRESH_SKEW_SECONDS = 30;

/**
 * Why refresh is single-flight, and the one Keycloak setting that makes it mandatory.
 *
 * The realm sets `revokeRefreshToken` true with `refreshTokenMaxReuse` 0. That is the right
 * setting and it makes a stolen refresh token detectable, but it means presenting a refresh
 * token that has already been exchanged invalidates the whole chain. Two overlapping
 * refreshes in one tab therefore do not race harmlessly, they sign the person out.
 */
export const REFRESH_MUST_BE_SINGLE_FLIGHT =
  "revokeRefreshToken with refreshTokenMaxReuse 0 means replaying a refresh token kills " +
  "the session. Two concurrent refreshes in one tab are a self-inflicted logout, so there " +
  "is exactly one in-flight refresh and every caller awaits the same promise.";

/**
 * How many times the console may bounce to the identity provider before it stops and shows
 * an error instead.
 *
 * Without this, a callback that fails for a stable reason (a `redirect_uri` the realm does
 * not know, a clock so far out that every token looks expired) becomes an infinite redirect
 * loop between two hosts. A loop is worse than an error: it burns the identity provider's
 * brute-force counters, it produces no message anybody can read, and the back button does
 * not escape it.
 */
export const MAX_SIGN_IN_ATTEMPTS = 3;

/**
 * How long the loop guard remembers an attempt. Long enough to catch a tight loop, short
 * enough that a person who failed once and went for coffee is not locked out of trying.
 */
export const SIGN_IN_ATTEMPT_WINDOW_SECONDS = 60;

/**
 * Keys for the values that have to survive a full page navigation to Keycloak and back.
 *
 * They live in the tab's own session storage and nowhere else: see `pkce.ts` for why that
 * is the only storage this console uses for anything to do with sign-in, and why no token
 * is ever written to any of it.
 */
export const PENDING_SIGN_IN_KEY = "brain.console.auth.pending";
export const SIGN_IN_ATTEMPTS_KEY = "brain.console.auth.attempts";
