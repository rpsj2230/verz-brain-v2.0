/**
 * The sign-in constants, checked against the realm they claim to describe.
 *
 * **Every value in `src/auth/constants.ts` is a copy of a fact held in
 * `ops/keycloak/realm-export.json`**, and its own docstring says so: "where this file and
 * that file disagree, that file is the fact and this one is the bug". A test that imported
 * a constant and compared it with itself would be green for every value it could hold,
 * which is the failure this repository has recorded four times in one day. So the realm
 * export is parsed and the constants are compared against it.
 *
 * Getting one of these wrong is not a visible failure. Pointing the console at a different
 * client id, or letting a registered path drift by one character, produces a Keycloak
 * error page that names none of the settings involved, after a redirect that looked like
 * it was working.
 *
 * Task ids: M32.5.1.3
 */

import { describe, expect, test } from "vitest";
import {
  CALLBACK_PATH,
  KEYCLOAK_CLIENT_ID,
  PKCE_CHALLENGE_METHOD,
  REALM_ACCESS_TOKEN_LIFESPAN_SECONDS,
  REFRESH_SKEW_SECONDS,
  RESPONSE_TYPE,
  SIGNED_OUT_PATH,
} from "../src/auth/constants";
import { browserClient, pathOf, realm } from "./support/realm";

describe("the console's sign-in constants", () => {
  test("the client id is the realm's own browser client", () => {
    // What breaks if this is deleted: the console signs in against a client whose flow
    // settings nobody checked. A client id is a reference to a set of flow settings, not a
    // label, and the browser cannot tell that it has been pointed at a client with the
    // implicit flow or a direct grant enabled, because the browser is not the thing being
    // protected. Compared against the realm rather than against itself.
    expect(KEYCLOAK_CLIENT_ID).toBe(browserClient().clientId);
  });

  test("the callback path is the one the realm has registered", () => {
    // What breaks if this is deleted: sign-in stops, at Keycloak, with a message about an
    // invalid redirect URI that names nothing a reader can act on. Keycloak matches
    // `redirect_uri` exactly against its registered list, so this path and that path are
    // one value written in two systems.
    const registered = browserClient().redirectUris ?? [];
    expect(registered.length).toBeGreaterThan(0);
    expect([...new Set(registered.map(pathOf))]).toEqual([CALLBACK_PATH]);
  });

  test("the signed-out path is the one the realm has registered", () => {
    // What breaks if this is deleted: sign-out completes at Keycloak and leaves the person
    // on a Keycloak page, because it will not honour a `post_logout_redirect_uri` it does
    // not recognise. The symptom looks like the console being slow rather than like a
    // configuration mismatch.
    const declared = browserClient().attributes?.["post.logout.redirect.uris"] ?? "";
    expect(declared).not.toBe("");
    // Keycloak separates multiple values with "##".
    const paths = declared.split("##").map((value) => pathOf(value.trim()));
    expect([...new Set(paths)]).toEqual([SIGNED_OUT_PATH]);
  });

  test("the challenge method is the one the realm forces", () => {
    // What breaks if this is deleted: the console could offer `plain`, be refused by the
    // server, and the refusal would look like a broken sign-in. Asking for the weaker of
    // two things and being saved by somebody else's configuration is not a design.
    expect(PKCE_CHALLENGE_METHOD).toBe(
      browserClient().attributes?.["pkce.code.challenge.method"],
    );
  });

  test("the console asks for the only flow the realm allows", () => {
    // What breaks if this is deleted: a fallback to the implicit flow could be added
    // without anything noticing that the realm has it switched off, and the implicit flow
    // returns the token in a URL fragment, where browser history and referrer headers keep
    // it. The realm is what makes `response_type=code` the only correct answer.
    const client = browserClient();
    expect(client.standardFlowEnabled).toBe(true);
    expect(client.implicitFlowEnabled).toBe(false);
    expect(RESPONSE_TYPE).toBe("code");
  });

  test("no client in the realm accepts a username and a password", () => {
    // What breaks if this is deleted: the justification for this console having no sign-in
    // form. A direct grant enabled anywhere in the realm is an endpoint that takes a
    // password and skips the browser flow, and therefore the second factor the realm makes
    // mandatory. The absence of a password field here is only sound while this is true.
    const enabled = (realm().clients ?? []).filter(
      (client) => client.directAccessGrantsEnabled === true,
    );
    expect(enabled.map((client) => client.clientId)).toEqual([]);
  });

  test("the refresh skew is a fraction of the realm's token lifespan", () => {
    // What breaks if this is deleted: two numbers that only make sense together drift
    // apart. A skew longer than the lifespan refreshes on every request; a skew of nothing
    // sends a token that expires in transit. Both constants are compared with the realm
    // and with each other rather than with themselves.
    expect(REALM_ACCESS_TOKEN_LIFESPAN_SECONDS).toBe(realm().accessTokenLifespan);
    expect(REFRESH_SKEW_SECONDS).toBeGreaterThan(0);
    expect(REFRESH_SKEW_SECONDS).toBeLessThan(REALM_ACCESS_TOKEN_LIFESPAN_SECONDS / 2);
  });

  test("the realm still makes a replayed refresh token fatal", () => {
    // What breaks if this is deleted: the reason refresh is single-flight stops being
    // checked. `revokeRefreshToken` with `refreshTokenMaxReuse` 0 is what turns two
    // overlapping refreshes from a harmless race into a logout. If the realm ever relaxes
    // that, the single-flight test below is still correct but its argument has changed,
    // and somebody should have to look at both.
    const settings = realm();
    expect(settings.revokeRefreshToken).toBe(true);
    expect(settings.refreshTokenMaxReuse).toBe(0);
  });
});
