/**
 * The sign-in flow: what leaves the browser, what comes back, and what is kept.
 *
 * These are the properties rather than the implementation. The console could hold its
 * tokens in a closure, a class or a store and every test here would still read the same,
 * because what is asserted is that a token never reaches a browser store, that a callback
 * which did not come from a sign-in this tab started is refused, that two refreshes make
 * one request, and that a permanent failure ends in a message rather than a loop.
 *
 * Each test loads a fresh copy of the modules. `session.ts` holds the tokens, the session
 * state, the memoised callback promise and the in-flight refresh at module level, which is
 * correct for a page that signs in once and cannot be undone from outside.
 */

import { createHash } from "node:crypto";
import { describe, expect, test } from "vitest";
import {
  CONSOLE_ORIGIN,
  END_SESSION_ENDPOINT,
  ISSUER,
  TOKEN_ENDPOINT,
  everythingInStorage,
  loadConsole,
  signIn,
} from "./support/auth";
import { clientScopeNames } from "./support/realm";

/**
 * An S256 challenge computed with Node's own hash rather than with the console's.
 *
 * If the expected value came from `challengeFor`, this would compare the implementation
 * with itself and pass for any digest it produced, including the wrong one.
 */
function s256(verifier: string): string {
  return createHash("sha256")
    .update(verifier, "ascii")
    .digest("base64")
    .replace(/\+/g, "-")
    .replace(/\//g, "_")
    .replace(/=+$/, "");
}

describe("leaving for the identity provider", () => {
  test("the authorisation request carries a challenge for the verifier it stored", async () => {
    // What breaks if this is deleted: the two halves of PKCE stop being related. A console
    // that stored one verifier and sent the challenge for another fails only at the token
    // exchange, with `invalid_grant`, which is the same error an expired code gives. The
    // verifier is read out of the store as raw text and the challenge recomputed here, so
    // the code that generates and stores it is the code under test.
    const loaded = await loadConsole();
    await loaded.session.beginSignIn("/");

    const stored = JSON.parse(
      sessionStorage.getItem(loaded.constants.PENDING_SIGN_IN_KEY) ?? "null",
    ) as { verifier: string; state: string };
    const url = loaded.location.lastAssigned();

    expect(url.searchParams.get("code_challenge")).toBe(s256(stored.verifier));
    expect(url.searchParams.get("code_challenge_method")).toBe("S256");
    expect(url.searchParams.get("state")).toBe(stored.state);
    expect(url.searchParams.get("response_type")).toBe("code");
    expect(url.searchParams.get("redirect_uri")).toBe(
      `${CONSOLE_ORIGIN}${loaded.constants.CALLBACK_PATH}`,
    );
    expect(url.searchParams.get("client_id")).toBe(loaded.constants.KEYCLOAK_CLIENT_ID);
  });

  test("the authorisation request goes to the endpoint the provider published", async () => {
    // What breaks if this is deleted: the discovery document stops being the source of the
    // endpoints, and a hard-coded Keycloak URL shape starts working until an upgrade moves
    // one, at which point sign-in fails in a way that reads as "sign-in is broken" rather
    // than as "a path moved".
    const loaded = await loadConsole();
    await loaded.session.beginSignIn("/");

    expect(loaded.idp.urls[0]).toBe(`${ISSUER}/.well-known/openid-configuration`);
    expect(loaded.location.lastAssigned().origin + loaded.location.lastAssigned().pathname).toBe(
      `${ISSUER}/protocol/openid-connect/auth`,
    );
  });

  test("a provider that calls itself something else is refused", async () => {
    // What breaks if this is deleted: the one thing this console checks about the document
    // it fetched. The API compares `iss` on every token by exact string equality, so a
    // console that accepted a document from a differently named issuer would complete a
    // sign-in whose tokens the API then rejects, with nothing in the browser explaining
    // why.
    const loaded = await loadConsole();
    loaded.idp.setDiscovery({
      issuer: "https://idp.test/realms/somewhere-else",
      authorization_endpoint: `${ISSUER}/protocol/openid-connect/auth`,
      token_endpoint: TOKEN_ENDPOINT,
    });

    await loaded.session.beginSignIn("/");

    expect(loaded.location.assign).not.toHaveBeenCalled();
    expect(loaded.session.getSessionState().status).toBe("failed");
    expect(loaded.session.getSessionState().message).not.toBe("");
  });

  test("the console asks only for scopes this realm can grant", async () => {
    // What breaks if this is deleted: a scope the realm does not define. Keycloak logs
    // "Referenced client scope doesn't exist. Ignoring" and completes the sign-in, so
    // asking for `profile` or `email` here looks like it works and quietly returns a token
    // without them, which is a bug that only appears when something starts reading a claim.
    // `openid` is the exception: it is a request value rather than a client scope, and it
    // is what makes this an OIDC request at all. The realm is read for the rest.
    const loaded = await loadConsole();
    await loaded.session.beginSignIn("/");

    const requested = (loaded.location.lastAssigned().searchParams.get("scope") ?? "")
      .split(" ")
      .filter((value) => value.length > 0);
    const definable = new Set(clientScopeNames());

    // Without `openid` there is no ID token, and sign-out loses the `id_token_hint` that
    // lets Keycloak end the specific session without a confirmation page.
    expect(requested).toContain("openid");
    for (const scope of requested) {
      if (scope === "openid") {
        continue;
      }
      expect(definable.has(scope), `${scope} is not a client scope in this realm`).toBe(true);
    }
  });

  test("the discovery document is fetched without ambient credentials", async () => {
    // What breaks if this is deleted: a cookie for the identity provider's domain is sent
    // with a request that does not need one. Discovery is an unauthenticated document; the
    // only thing sending credentials can do is widen what a mistake reaches.
    const loaded = await loadConsole();
    await loaded.session.beginSignIn("/");

    const discovery = loaded.idp.calls.find((call) =>
      call.url.endsWith("/.well-known/openid-configuration"),
    );
    expect(discovery?.init?.credentials).toBe("omit");
  });

  test("starting a sign-in twice makes one redirect", async () => {
    // What breaks if this is deleted: React's strict mode runs an effect, tears it down and
    // runs it again, so the guard that starts sign-in fires twice on every mount in
    // development. Two attempts against a counter that allows three means the loop guard
    // trips on the first genuine failure instead of the third, and the console reports a
    // redirect-URI problem that does not exist.
    const loaded = await loadConsole();

    await Promise.all([loaded.session.beginSignIn("/"), loaded.session.beginSignIn("/")]);

    expect(loaded.location.assign).toHaveBeenCalledTimes(1);
    const attempts: unknown = JSON.parse(
      sessionStorage.getItem(loaded.constants.SIGN_IN_ATTEMPTS_KEY) ?? "[]",
    );
    expect(attempts).toHaveLength(1);
  });

  test("only the handshake values are stored before the browser leaves", async () => {
    // What breaks if this is deleted: the store quietly grows a third thing. The verifier
    // and `state` are there because the flow leaves the page and they must survive a full
    // navigation; nothing else in this console has that problem, and a second writer is
    // how a token ends up in storage by accident.
    const loaded = await loadConsole();
    await loaded.session.beginSignIn("/activity");

    expect(Object.keys(localStorage)).toEqual([]);
    expect(Object.keys(sessionStorage).sort()).toEqual(
      [loaded.constants.PENDING_SIGN_IN_KEY, loaded.constants.SIGN_IN_ATTEMPTS_KEY].sort(),
    );
    const pending: unknown = JSON.parse(
      sessionStorage.getItem(loaded.constants.PENDING_SIGN_IN_KEY) ?? "null",
    );
    expect(Object.keys(pending as object).sort()).toEqual(["returnTo", "state", "verifier"]);
  });
});

describe("the callback", () => {
  test("a callback that matches completes the sign-in", async () => {
    // What breaks if this is deleted: everything else here is a refusal, and a guard tested
    // only by its refusals is satisfied by a function that refuses everything. This is the
    // sibling that proves the flow still works.
    const loaded = await loadConsole();
    const landed = await signIn(loaded, { returnTo: "/activity" });

    expect(landed).toBe("/activity");
    expect(loaded.session.isSignedIn()).toBe(true);
    expect(loaded.session.getSessionState().status).toBe("authenticated");
  });

  test("a callback with a mismatched state is refused", async () => {
    // What breaks if this is deleted: the CSRF check on this flow. `state` is what proves
    // the callback belongs to a sign-in this tab started; without the comparison, anybody
    // who can get this browser to open the callback URL with a code of their choosing gets
    // that code exchanged for a token in this session.
    const loaded = await loadConsole();
    await loaded.session.beginSignIn("/");
    const requestsBefore = loaded.idp.tokenRequests.length;

    await expect(
      loaded.session.completeSignIn(
        new URLSearchParams({ code: "AUTHORISATION-CODE", state: "not-the-state" }),
      ),
    ).rejects.toThrow();

    expect(loaded.idp.tokenRequests).toHaveLength(requestsBefore);
    expect(loaded.session.isSignedIn()).toBe(false);
  });

  test("a callback with no pending sign-in is refused", async () => {
    // What breaks if this is deleted: a callback URL opened from a bookmark, a history
    // entry or somebody else's message is treated as a sign-in this tab asked for. There is
    // no verifier to redeem it with, so the exchange would fail anyway; refusing before the
    // request means the identity provider never sees a code this browser did not request.
    const loaded = await loadConsole();

    await expect(
      loaded.session.completeSignIn(
        new URLSearchParams({ code: "AUTHORISATION-CODE", state: "anything" }),
      ),
    ).rejects.toThrow();

    expect(loaded.idp.tokenRequests).toHaveLength(0);
  });

  test("a callback carrying an error is reported and nothing is exchanged", async () => {
    // What breaks if this is deleted: cancelling at the sign-in page produces a generic
    // failure instead of the provider's own reason, and the console makes a token request
    // with no code in it, which the provider refuses with a second, less useful error.
    const loaded = await loadConsole();
    await loaded.session.beginSignIn("/");

    await expect(
      loaded.session.completeSignIn(new URLSearchParams({ error: "access_denied" })),
    ).rejects.toThrow(/access_denied/);
    expect(loaded.idp.tokenRequests).toHaveLength(0);
  });

  test("the verifier is spent by the first exchange", async () => {
    // What breaks if this is deleted: single use. A verifier left in the store after its
    // own exchange can be replayed against a second code, and a `state` left behind
    // validates a later callback that this tab never started.
    const loaded = await loadConsole();
    await signIn(loaded);

    expect(sessionStorage.getItem(loaded.constants.PENDING_SIGN_IN_KEY)).toBeNull();
  });

  test("running the callback twice redeems one code", async () => {
    // What breaks if this is deleted: strict mode again. The callback effect runs twice,
    // and the first run has already consumed the stored verifier and redeemed the code. A
    // second exchange would find nothing pending, report that this callback does not belong
    // here, and overwrite a sign-in that had just worked. A page completes at most one
    // sign-in, so one promise is the whole lifetime.
    const loaded = await loadConsole();
    loaded.idp.queueToken({
      access_token: "ACCESS-TOKEN-1",
      refresh_token: "REFRESH-TOKEN-1",
      expires_in: 300,
    });
    await loaded.session.beginSignIn("/activity");
    const returnedState = loaded.location.lastAssigned().searchParams.get("state") ?? "";
    const params = new URLSearchParams({ code: "AUTHORISATION-CODE", state: returnedState });

    const [first, second] = await Promise.all([
      loaded.session.completeSignIn(params),
      loaded.session.completeSignIn(params),
    ]);

    expect(first).toBe("/activity");
    expect(second).toBe("/activity");
    expect(loaded.idp.tokenRequests).toHaveLength(1);
    expect(loaded.session.getSessionState().status).toBe("authenticated");
  });

  test("the token request authenticates with the verifier and no client secret", async () => {
    // What breaks if this is deleted: the client stops being public in practice. A secret
    // in this request would be a secret in the bundle, readable by anybody who opens the
    // console, and the realm gives this client PKCE precisely because a browser cannot
    // keep one.
    const loaded = await loadConsole();
    await signIn(loaded);

    const body = loaded.idp.tokenRequests[0] as URLSearchParams;
    expect(body.get("grant_type")).toBe("authorization_code");
    expect(body.get("client_id")).toBe(loaded.constants.KEYCLOAK_CLIENT_ID);
    expect(body.get("code_verifier")).toBeTruthy();
    expect(body.has("client_secret")).toBe(false);

    const call = loaded.idp.calls.find((entry) => entry.url === TOKEN_ENDPOINT);
    expect(call?.init?.credentials).toBe("omit");
  });

  test("a return address that leaves this origin is replaced with the root", async () => {
    // What breaks if this is deleted: an open redirect. A value beginning with two slashes
    // is protocol relative and navigates off-site, and "it came from our own storage" is
    // the reasoning that keeps bugs of this shape alive for years. React Router has had
    // this exact bug, with this exact input, in the version this console pins.
    for (const hostile of ["//evil.example/x", "https://evil.example/x", "javascript:alert(1)"]) {
      const loaded = await loadConsole();
      await loaded.session.beginSignIn(hostile);
      const state = loaded.location.lastAssigned().searchParams.get("state") ?? "";
      loaded.idp.queueToken({
        access_token: "ACCESS-TOKEN-1",
        refresh_token: "REFRESH-TOKEN-1",
        expires_in: 300,
      });

      const landed = await loaded.session.completeSignIn(
        new URLSearchParams({ code: "AUTHORISATION-CODE", state }),
      );

      expect(landed).toBe("/");
    }
  });

  test("a return address on this origin is kept", async () => {
    // What breaks if this is deleted: the guard above is satisfied by sending everybody to
    // the root, which loses the page a person asked for on every sign-in and would never
    // be noticed by a refusal test.
    const loaded = await loadConsole();
    expect(await signIn(loaded, { returnTo: "/activity?filter=today#top" })).toBe(
      "/activity?filter=today#top",
    );
  });
});

describe("what is kept", () => {
  test("no token is written to any browser storage", async () => {
    // What breaks if this is deleted: the decision this console's whole session design
    // rests on. The realm gives an SSO session ten hours, so a refresh token in a browser
    // store is a ten-hour credential readable by any script on the origin, and an XSS bug
    // stops being a session hijack and becomes a portable credential. Every key and value
    // in both stores is searched for the tokens, so persisting one under any name fails.
    const loaded = await loadConsole();
    await signIn(loaded, {
      accessToken: "ACCESS-SENTINEL-9f3a",
      refreshToken: "REFRESH-SENTINEL-2b71",
      idToken: "ID-SENTINEL-c40d",
      expiresIn: 300,
    });

    const stored = everythingInStorage();
    const haystack = [...stored.keys, ...stored.values].join("\n");
    for (const secret of ["ACCESS-SENTINEL-9f3a", "REFRESH-SENTINEL-2b71", "ID-SENTINEL-c40d"]) {
      expect(haystack).not.toContain(secret);
    }
    // And the session is genuinely established, so this is not passing because nothing
    // happened.
    expect(await loaded.session.accessToken()).toBe("ACCESS-SENTINEL-9f3a");
  });

  test("a refreshed token is not written to storage either", async () => {
    // What breaks if this is deleted: the leak that arrives later. A console can hold the
    // first token in memory and persist the refreshed one, and every test that only signs
    // in would still pass.
    const loaded = await loadConsole();
    await signIn(loaded, { expiresIn: 0 });
    loaded.idp.queueToken({
      access_token: "REFRESHED-SENTINEL-77aa",
      refresh_token: "REFRESH-SENTINEL-88bb",
      expires_in: 300,
    });

    expect(await loaded.session.accessToken()).toBe("REFRESHED-SENTINEL-77aa");

    const stored = everythingInStorage();
    const haystack = [...stored.keys, ...stored.values].join("\n");
    expect(haystack).not.toContain("REFRESHED-SENTINEL-77aa");
    expect(haystack).not.toContain("REFRESH-SENTINEL-88bb");
  });

  test("the access token is held opaquely and never parsed", async () => {
    // What breaks if this is deleted: the rule that nothing in this console reads a
    // token's contents. A value that is not a JWT at all completes a sign-in and is handed
    // back byte for byte, which is only possible if nothing decoded it, split it on dots,
    // or read an expiry out of it. The boundary check covers the obvious spellings of
    // decoding; this covers the behaviour.
    const opaque = "||| not a jwt {at all} |||";
    const loaded = await loadConsole();
    await signIn(loaded, { accessToken: opaque });

    expect(await loaded.session.accessToken()).toBe(opaque);
    expect(loaded.session.isSignedIn()).toBe(true);
  });

  test("a response with no expiry is treated as due for refresh rather than eternal", async () => {
    // What breaks if this is deleted: a provider that omits `expires_in` gives this console
    // a token it believes is valid for ever, and it keeps sending it long after the API has
    // stopped accepting it. One wasted refresh is the cheaper error.
    const loaded = await loadConsole();
    loaded.idp.queueToken({ access_token: "FIRST", refresh_token: "R1" });
    await loaded.session.beginSignIn("/");
    const state = loaded.location.lastAssigned().searchParams.get("state") ?? "";
    await loaded.session.completeSignIn(
      new URLSearchParams({ code: "AUTHORISATION-CODE", state }),
    );

    loaded.idp.queueToken({ access_token: "SECOND", refresh_token: "R2", expires_in: 300 });
    expect(await loaded.session.accessToken()).toBe("SECOND");
  });
});

describe("refresh", () => {
  test("a token inside the refresh skew is already due for refresh", async () => {
    // What breaks if this is deleted: the skew stops meaning anything, and a token is used
    // right up to the moment it expires. A request that leaves this browser valid and
    // arrives expired fails at the API, which is the case the 401 path exists to survive
    // rather than the case it should be handling routinely. The lifetime handed to the
    // provider is the skew itself, so the expected behaviour is derived from the constant
    // rather than restated beside it.
    const loaded = await loadConsole();
    await signIn(loaded, { expiresIn: loaded.constants.REFRESH_SKEW_SECONDS });
    loaded.idp.queueToken({ access_token: "REFRESHED", refresh_token: "R2", expires_in: 300 });

    expect(await loaded.session.accessToken()).toBe("REFRESHED");
  });

  test("a token well inside its life is used as it is", async () => {
    // What breaks if this is deleted: the guard above is satisfied by refreshing on every
    // request, which is a token exchange per page view and, with `refreshTokenMaxReuse` 0,
    // a much larger window in which two of them overlap.
    const loaded = await loadConsole();
    await signIn(loaded, {
      accessToken: "STILL-GOOD",
      expiresIn: loaded.constants.REFRESH_SKEW_SECONDS * 10,
    });
    const before = loaded.idp.tokenRequests.length;

    expect(await loaded.session.accessToken()).toBe("STILL-GOOD");
    expect(loaded.idp.tokenRequests).toHaveLength(before);
  });

  test("two concurrent refreshes make one request", async () => {
    // What breaks if this is deleted: the realm sets `revokeRefreshToken` with
    // `refreshTokenMaxReuse` 0, so presenting a refresh token that has already been
    // exchanged invalidates the whole chain. Two overlapping refreshes in one tab do not
    // race harmlessly; they sign the person out, and the report is "it logs me out at
    // random" from whoever had two requests in flight when the token expired.
    const loaded = await loadConsole();
    await signIn(loaded, { expiresIn: 0 });
    loaded.idp.queueToken({
      access_token: "ACCESS-TOKEN-2",
      refresh_token: "REFRESH-TOKEN-2",
      expires_in: 300,
    });

    const results = await Promise.all([
      loaded.session.accessToken(),
      loaded.session.accessToken(),
      loaded.session.accessToken(),
    ]);

    expect(results).toEqual(["ACCESS-TOKEN-2", "ACCESS-TOKEN-2", "ACCESS-TOKEN-2"]);
    const refreshes = loaded.idp.tokenRequests.filter(
      (body) => body.get("grant_type") === "refresh_token",
    );
    expect(refreshes).toHaveLength(1);
  });

  test("a later expiry refreshes again rather than reusing the first promise for ever", async () => {
    // What breaks if this is deleted: single-flight becomes single-ever. A memoised promise
    // that is never cleared returns the first refreshed token for the life of the page, so
    // the session dies quietly after two token lifetimes and the console shows refusals
    // with no explanation.
    const loaded = await loadConsole();
    await signIn(loaded, { expiresIn: 0 });
    loaded.idp.queueToken({ access_token: "SECOND", refresh_token: "R2", expires_in: 0 });
    expect(await loaded.session.accessToken()).toBe("SECOND");

    loaded.idp.queueToken({ access_token: "THIRD", refresh_token: "R3", expires_in: 300 });
    expect(await loaded.session.accessToken()).toBe("THIRD");
  });

  test("a refresh that is refused forgets the session", async () => {
    // What breaks if this is deleted: a spent or revoked refresh token leaves the console
    // believing it has a session, so it sends a dead token and renders refusals instead of
    // sending the person back to sign in.
    const loaded = await loadConsole();
    await signIn(loaded, { expiresIn: 0 });
    loaded.idp.queueTokenFailure(400, { error: "invalid_grant" });

    expect(await loaded.session.accessToken()).toBeNull();
    expect(loaded.session.isSignedIn()).toBe(false);
    expect(loaded.session.getSessionState().status).toBe("unknown");
  });

  test("a session with no refresh token does not try to refresh", async () => {
    // What breaks if this is deleted: a request to the token endpoint with an empty
    // `refresh_token`, which the provider refuses, which forgets the session. The console
    // would sign somebody out because it had nothing to renew with, rather than because
    // anything was wrong.
    const loaded = await loadConsole();
    loaded.idp.queueToken({ access_token: "ONLY", expires_in: 0 });
    await loaded.session.beginSignIn("/");
    const state = loaded.location.lastAssigned().searchParams.get("state") ?? "";
    await loaded.session.completeSignIn(
      new URLSearchParams({ code: "AUTHORISATION-CODE", state }),
    );
    const before = loaded.idp.tokenRequests.length;

    expect(await loaded.session.accessToken()).toBeNull();
    expect(loaded.idp.tokenRequests).toHaveLength(before);
  });
});

describe("the loop guard", () => {
  test("sign-in stops after the attempt limit", async () => {
    // What breaks if this is deleted: a callback that fails for a stable reason, such as a
    // redirect URI the realm does not know or a clock far enough out that every token looks
    // expired, becomes an infinite redirect between two hosts. A loop is worse than an
    // error: it burns the provider's brute-force counters, produces nothing anybody can
    // read, and the back button does not escape it.
    const loaded = await loadConsole();
    const limit = loaded.constants.MAX_SIGN_IN_ATTEMPTS;

    for (let attempt = 0; attempt < limit + 3; attempt += 1) {
      await loaded.session.beginSignIn("/");
      // What a real failure looks like from this module's side: the page came back with no
      // session, so the guard asks again.
      loaded.session.forgetSession();
    }

    expect(loaded.location.assign).toHaveBeenCalledTimes(limit);
    // And the person is told, rather than left on a spinner.
    const stateAfter = await (async () => {
      await loaded.session.beginSignIn("/");
      return loaded.session.getSessionState();
    })();
    expect(stateAfter.status).toBe("failed");
    expect(stateAfter.message).not.toBe("");
  });

  test("the limit is a loop guard rather than a lockout", async () => {
    // What breaks if this is deleted: the number stops having to mean anything. A limit of
    // one refuses the person who was mid sign-in when their laptop slept; a limit of fifty
    // is a redirect loop with extra steps. Asserted against a range rather than against
    // itself, because a test that imports the constant and counts to it is green for every
    // value the constant could hold.
    const loaded = await loadConsole();
    expect(loaded.constants.MAX_SIGN_IN_ATTEMPTS).toBeGreaterThanOrEqual(2);
    expect(loaded.constants.MAX_SIGN_IN_ATTEMPTS).toBeLessThanOrEqual(5);
  });

  test("a completed sign-in clears the attempt counter", async () => {
    // What breaks if this is deleted: attempts accumulate across successful sign-ins, so a
    // person who signed in twice in a minute is refused the third time for a failure that
    // never happened.
    const loaded = await loadConsole();
    await signIn(loaded);

    expect(sessionStorage.getItem(loaded.constants.SIGN_IN_ATTEMPTS_KEY)).toBeNull();
  });
});

describe("signing out", () => {
  test("signing out ends the session at the identity provider", async () => {
    // What breaks if this is deleted: the bug that looks like a feature. Forgetting the
    // token here while the SSO session lives on means the next visit signs straight back in
    // with no prompt, which on a shared machine is not a sign-out at all.
    const loaded = await loadConsole();
    await signIn(loaded, { idToken: "ID-TOKEN-1" });

    await loaded.session.signOut();

    const url = loaded.location.lastAssigned();
    expect(`${url.origin}${url.pathname}`).toBe(END_SESSION_ENDPOINT);
    expect(url.searchParams.get("post_logout_redirect_uri")).toBe(
      `${CONSOLE_ORIGIN}${loaded.constants.SIGNED_OUT_PATH}`,
    );
    expect(url.searchParams.get("id_token_hint")).toBe("ID-TOKEN-1");
    expect(loaded.session.isSignedIn()).toBe(false);
  });

  test("signing out without an id token still names the client", async () => {
    // What breaks if this is deleted: Keycloak 26 requires an `id_token_hint` or a
    // `client_id` before it will honour `post_logout_redirect_uri`, so without the fallback
    // the person ends their session on a Keycloak page rather than back on the console's
    // signed-out route, and reads that as the sign-out having failed.
    const loaded = await loadConsole();
    await signIn(loaded, { idToken: "" });

    await loaded.session.signOut();

    const url = loaded.location.lastAssigned();
    expect(url.searchParams.has("id_token_hint")).toBe(false);
    expect(url.searchParams.get("client_id")).toBe(loaded.constants.KEYCLOAK_CLIENT_ID);
  });
});
