/**
 * The one place this console talks to the API, and the things it deliberately does not do.
 *
 * **Nothing here decides what may be fetched.** There is no allow-list of paths, no check
 * of anything about the caller, and no branch that asks whether this person should be
 * asking. The API decides, per request, from grants this browser never receives. A console
 * that filtered its own requests would be a second permission model, and the copy in the
 * browser is the copy an attacker edits.
 *
 * The tests below are therefore mostly about what happens rather than about what is
 * prevented: the token is attached, cookies are not sent, a refusal comes back as a value,
 * and a 401 forgets the session so the guard can start again.
 */

import { describe, expect, test } from "vitest";
import { fakeIdentityProvider, loadConsole, signIn } from "./support/auth";

interface ApiCall {
  readonly url: string;
  readonly init: RequestInit | undefined;
}

/** A stand-in API that answers anything under the versioned prefix. */
function withApi(answer: (url: string) => Response) {
  const seen: ApiCall[] = [];
  const idp = fakeIdentityProvider({
    api(url, init) {
      if (!url.startsWith("/api/")) {
        return null;
      }
      seen.push({ url, init });
      return answer(url);
    },
  });
  return { idp, seen };
}

function json(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function headersOf(call: ApiCall): Record<string, string> {
  return (call.init?.headers ?? {}) as Record<string, string>;
}

describe("a request", () => {
  test("a successful request returns the body the API sent", async () => {
    // What breaks if this is deleted: every refusal test here is satisfied by a client that
    // fails on everything. This is the sibling that proves the transport works at all.
    const { idp, seen } = withApi(() => json({ status: "ok", commit: "abc123" }));
    const loaded = await loadConsole({ idp });
    await signIn(loaded);

    const result = await loaded.client.request<{ status: string }>("/health/ready");

    expect(result.ok).toBe(true);
    expect(result.ok && result.data).toEqual({ status: "ok", commit: "abc123" });
    expect(seen[0]?.url).toBe("/api/v1/health/ready");
  });

  test("a request carries the bearer token and no cookies", async () => {
    // What breaks if this is deleted: either the API stops being told who is asking, or the
    // request starts being made meaningful by a cookie that happened to be in the browser,
    // which is the whole shape of a cross-site request forgery. The console authenticates
    // with a bearer token and with nothing else.
    const { idp, seen } = withApi(() => json({}));
    const loaded = await loadConsole({ idp });
    await signIn(loaded, { accessToken: "ACCESS-SENTINEL-4a2c" });

    await loaded.client.request("/anything");

    const call = seen[0] as ApiCall;
    expect(headersOf(call)["authorization"]).toBe("Bearer ACCESS-SENTINEL-4a2c");
    expect(call.init?.credentials).toBe("omit");
  });

  test("a request without a session still goes out and is refused by the API", async () => {
    // What breaks if this is deleted: the test of whether a client-side guard is doing
    // security work. Removing `RequireSession` must change nothing about what the server
    // returns: requests go out with no bearer token, the API refuses them, and the console
    // shows refusals instead of a sign-in prompt. A client that refused to make the request
    // itself would be a permission decision taken in the browser.
    const { idp, seen } = withApi(() => json({ message: "I could not find that." }, 404));
    const loaded = await loadConsole({ idp });

    const result = await loaded.client.request("/records/1");

    expect(seen).toHaveLength(1);
    expect(headersOf(seen[0] as ApiCall)["authorization"]).toBeUndefined();
    expect(result.ok).toBe(false);
    expect(!result.ok && result.failure.status).toBe(404);
  });

  test("no path is filtered before it is sent", async () => {
    // What breaks if this is deleted: an allow-list appears, and from then on the console
    // holds an opinion about which parts of the API exist. That opinion is a copy of the
    // routing table that nobody keeps in step, and the first thing it does is hide a new
    // endpoint from the only page that calls it.
    const { idp, seen } = withApi(() => json({}));
    const loaded = await loadConsole({ idp });
    await signIn(loaded);

    for (const path of ["/records/1", "/admin/anything", "/../elsewhere", "/a?b=c#d"]) {
      await loaded.client.request(path);
    }

    expect(seen.map((call) => call.url)).toEqual([
      "/api/v1/records/1",
      "/api/v1/admin/anything",
      "/api/v1/../elsewhere",
      "/api/v1/a?b=c#d",
    ]);
  });

  test("a refusal is a value rather than an exception", async () => {
    // What breaks if this is deleted: a component-level catch renders a red banner saying
    // "error" for the most common outcome in this system, which is a 404 that is a
    // legitimate answer to a legitimate question. A 404 here is not an exception; it is
    // what "nothing you hold reaches that" looks like from outside.
    const { idp } = withApi(() => json({ message: "I could not find that." }, 404));
    const loaded = await loadConsole({ idp });
    await signIn(loaded);

    const result = await loaded.client.request("/records/1");

    expect(result.ok).toBe(false);
    expect(!result.ok && result.failure.message).toBe("I could not find that.");
  });

  test("a request that never reaches the API is a value too", async () => {
    // What breaks if this is deleted: a dropped connection throws out of the one function
    // that is supposed to shape every failure, and the call site that forgot to catch it
    // shows a blank page.
    const { idp } = withApi(() => {
      throw new TypeError("Failed to fetch");
    });
    const loaded = await loadConsole({ idp });
    await signIn(loaded);

    const result = await loaded.client.request("/records/1");

    expect(result.ok).toBe(false);
    expect(!result.ok && result.failure.status).toBe(0);
  });

  test("a 401 forgets the session", async () => {
    // What breaks if this is deleted: the API refuses a token this console believed was
    // current and the console keeps sending it. The session may have ended elsewhere, the
    // token may be minted for an audience the API does not accept, the clock may be wrong;
    // the console does not guess which, it forgets, and the guard starts a fresh sign-in.
    const { idp } = withApi(() => json({ message: "Something went wrong." }, 401));
    const loaded = await loadConsole({ idp });
    await signIn(loaded);
    expect(loaded.session.isSignedIn()).toBe(true);

    await loaded.client.request("/records/1");

    expect(loaded.session.isSignedIn()).toBe(false);
    expect(loaded.session.getSessionState().status).toBe("unknown");
  });

  test("a 404 does not forget the session", async () => {
    // What breaks if this is deleted: every refusal signs the person out. A 404 is the
    // ordinary answer to a question about something the caller cannot reach, and treating
    // it as an authentication failure would bounce somebody to the identity provider for
    // asking a question they were entitled to ask and be refused.
    const { idp } = withApi(() => json({ message: "I could not find that." }, 404));
    const loaded = await loadConsole({ idp });
    await signIn(loaded);

    await loaded.client.request("/records/1");

    expect(loaded.session.isSignedIn()).toBe(true);
  });
});
