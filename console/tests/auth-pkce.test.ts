/**
 * PKCE: the challenge, and the two values that have to survive leaving the page.
 *
 * The client is public, so there is no client secret. The verifier is the only thing that
 * proves the browser redeeming the authorisation code is the browser that asked for it,
 * and `state` is the only thing that proves a callback belongs to a sign-in this tab
 * started. Both are single use, both live in the tab's session store because the flow
 * leaves the page, and neither is a token.
 */

import { describe, expect, test, vi } from "vitest";
import {
  InsecureContextError,
  challengeFor,
  putPending,
  randomToken,
  recordSignInAttempt,
  takePending,
} from "../src/auth/pkce";
import {
  PENDING_SIGN_IN_KEY,
  SIGN_IN_ATTEMPTS_KEY,
  SIGN_IN_ATTEMPT_WINDOW_SECONDS,
} from "../src/auth/constants";

/**
 * RFC 7636 appendix B, verbatim. An answer computed by somebody else, published years
 * before this console existed, is the only kind of expected value that cannot move when
 * the code under test moves.
 */
const RFC_7636_VERIFIER = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk";
const RFC_7636_CHALLENGE = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM";

describe("the PKCE challenge", () => {
  test("the challenge is the S256 digest of the verifier", async () => {
    // What breaks if this is deleted: the challenge stops being the thing the realm checks
    // the verifier against, and every sign-in fails at the token exchange with
    // `invalid_grant`, which reads as an expired code. Worse quietly: a base64 that is not
    // URL-safe, or a digest of the wrong bytes, produces a value of the right length and
    // shape. This is a published test vector, so no part of the expected answer comes from
    // the module being tested.
    expect(await challengeFor(RFC_7636_VERIFIER)).toBe(RFC_7636_CHALLENGE);
  });

  test("the challenge is base64url with no padding", async () => {
    // What breaks if this is deleted: a `+`, a `/` or a `=` in a query parameter, which
    // survives being sent and fails only at the identity provider, with a message about
    // the code verifier rather than about its encoding.
    const challenge = await challengeFor(randomToken());
    expect(challenge).toMatch(/^[A-Za-z0-9_-]+$/);
  });

  test("a browser with no web crypto is told what is wrong", async () => {
    // What breaks if this is deleted: serving the console over plain HTTP on a LAN address
    // leaves `crypto.subtle` undefined, and the failure arrives as "cannot read properties
    // of undefined", which sends the next person to debug the sign-in code rather than the
    // scheme in the address bar.
    vi.stubGlobal("crypto", { getRandomValues: globalThis.crypto.getRandomValues });

    await expect(challengeFor("anything")).rejects.toBeInstanceOf(InsecureContextError);
  });

  test("a fresh token is unguessable and URL safe", async () => {
    // What breaks if this is deleted: `state` stops being an unforgeable marker and the
    // verifier stops being a secret. A short or a predictable value fails no assertion
    // anywhere else, because the flow works perfectly well with a guessable one.
    const first = randomToken();
    const second = randomToken();

    expect(first).not.toBe(second);
    expect(first).toMatch(/^[A-Za-z0-9_-]+$/);
    // RFC 7636 requires a verifier of at least 43 characters, which is 32 bytes of entropy
    // encoded. Anything shorter is a weaker secret than the specification allows.
    expect(first.length).toBeGreaterThanOrEqual(43);
  });
});

describe("the pending sign-in", () => {
  test("the pending sign-in is deleted when it is read", () => {
    // What breaks if this is deleted: single use. A verifier that survives its own
    // exchange can be replayed against a second authorisation code, and a `state` that
    // survives no longer proves that this callback belongs to this attempt: it would
    // validate any later callback, which is the CSRF check quietly switched off.
    putPending({ verifier: "V", state: "S", returnTo: "/activity" });
    expect(sessionStorage.getItem(PENDING_SIGN_IN_KEY)).not.toBeNull();

    expect(takePending()).toEqual({ verifier: "V", state: "S", returnTo: "/activity" });

    expect(sessionStorage.getItem(PENDING_SIGN_IN_KEY)).toBeNull();
    expect(takePending()).toBeNull();
  });

  test("a stored value of the wrong shape is ignored rather than trusted", () => {
    // What breaks if this is deleted: a half-written or hand-edited record is read back as
    // a pending sign-in with undefined fields, and the `state` comparison then compares
    // undefined with undefined, which succeeds. The check that is meant to refuse a
    // callback would accept every callback.
    for (const raw of ["not json", "null", "[]", '{"verifier":"V"}', '{"verifier":1,"state":"S","returnTo":"/"}']) {
      sessionStorage.setItem(PENDING_SIGN_IN_KEY, raw);
      expect(takePending()).toBeNull();
    }
  });

  test("reading a malformed record still clears it", () => {
    // What breaks if this is deleted: a record that cannot be parsed stays in the store and
    // is re-read on every attempt, so a single corrupted value makes sign-in impossible
    // until somebody clears site data.
    sessionStorage.setItem(PENDING_SIGN_IN_KEY, "not json");
    takePending();
    expect(sessionStorage.getItem(PENDING_SIGN_IN_KEY)).toBeNull();
  });
});

describe("the sign-in attempt counter", () => {
  test("an attempt is counted", () => {
    // What breaks if this is deleted: the loop guard has nothing to count, and a callback
    // that fails for a stable reason bounces between two hosts for ever. This is the
    // positive case; the limit itself is exercised in the session tests.
    const now = Date.UTC(2026, 8, 6, 12, 0, 0);
    expect(recordSignInAttempt(now)).toBe(1);
    expect(recordSignInAttempt(now + 1000)).toBe(2);
  });

  test("attempts older than the window are forgotten", () => {
    // What breaks if this is deleted: the counter never resets, so somebody who failed to
    // sign in this morning and comes back after lunch is refused with a message about
    // several attempts. A loop guard that becomes a lockout is worse than the loop, because
    // the loop at least ends when the cause is fixed.
    const now = Date.UTC(2026, 8, 6, 12, 0, 0);
    const window = SIGN_IN_ATTEMPT_WINDOW_SECONDS * 1000;
    sessionStorage.setItem(
      SIGN_IN_ATTEMPTS_KEY,
      JSON.stringify([now - window - 1000, now - window - 500, now - 1000]),
    );

    // The two stale entries are dropped; the recent one and this attempt remain.
    expect(recordSignInAttempt(now)).toBe(2);
  });

  test("the window is short enough to be a loop guard and not a lockout", () => {
    // What breaks if this is deleted: the number stops having to mean anything. A window of
    // a day would turn three failed attempts into a day without a console, and a window of
    // a second would never catch a redirect loop, which completes in well under one.
    expect(SIGN_IN_ATTEMPT_WINDOW_SECONDS).toBeGreaterThanOrEqual(10);
    expect(SIGN_IN_ATTEMPT_WINDOW_SECONDS).toBeLessThanOrEqual(600);
  });
});
