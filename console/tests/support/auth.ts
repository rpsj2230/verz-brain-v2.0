/**
 * A stand-in identity provider, and a way to get a clean copy of the sign-in modules.
 *
 * **Why modules are reloaded rather than reset.** `src/auth/session.ts` holds the tokens,
 * the session state, the memoised callback promise and the in-flight refresh in
 * module-level variables, and `src/auth/discovery.ts` caches its promise the same way.
 * That is the right shape for the application: a page completes at most one sign-in and
 * makes at most one discovery request. It means a test cannot put the module back to how
 * it started, so each test that touches it calls `vi.resetModules()` and imports again,
 * which is what `loadConsole` does.
 *
 * **Why sign-in is performed rather than faked.** `signIn` drives the real
 * `beginSignIn` and `completeSignIn`, reading the `state` back out of the authorisation
 * URL the module built. Writing a pending record by hand and handing it to
 * `completeSignIn` would be a test of the consumer only: the code that generates the
 * verifier, stores it and puts the challenge in the URL would never run, and it could
 * return a constant with the suite green. A producer and a consumer either side of a value
 * need the producer's test written from the raw input, and here the raw input is "a person
 * clicked sign in".
 */

import { vi } from "vitest";

export const ISSUER = "https://idp.test/realms/brain";
export const AUTHORIZATION_ENDPOINT = `${ISSUER}/protocol/openid-connect/auth`;
export const TOKEN_ENDPOINT = `${ISSUER}/protocol/openid-connect/token`;
export const END_SESSION_ENDPOINT = `${ISSUER}/protocol/openid-connect/logout`;
export const CONSOLE_ORIGIN = "https://console.test";

function json(payload: unknown, status: number, headers: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "content-type": "application/json", ...headers },
  });
}

export interface TokenGrant {
  readonly access_token?: unknown;
  readonly refresh_token?: unknown;
  readonly id_token?: unknown;
  readonly expires_in?: unknown;
}

export interface FakeIdpOptions {
  /** Answer requests that are not the identity provider's, for example `/api/v1/...`. */
  readonly api?: (url: string, init: RequestInit | undefined) => Response | null;
}

export interface RecordedCall {
  readonly url: string;
  readonly init: RequestInit | undefined;
}

export interface FakeIdp {
  readonly fetch: ReturnType<typeof vi.fn>;
  /** Every URL fetched, in order. */
  readonly urls: string[];
  /** Every request, with the options it was made with. */
  readonly calls: RecordedCall[];
  /** The form body of every request to the token endpoint, in order. */
  readonly tokenRequests: URLSearchParams[];
  queueToken(grant: TokenGrant): void;
  queueTokenFailure(status: number, payload?: unknown): void;
  /** Replace the discovery document, or set it to null to make discovery fail. */
  setDiscovery(document: Record<string, unknown> | null): void;
}

export function fakeIdentityProvider(options: FakeIdpOptions = {}): FakeIdp {
  const urls: string[] = [];
  const calls: RecordedCall[] = [];
  const tokenRequests: URLSearchParams[] = [];
  const queued: { status: number; payload: unknown }[] = [];
  let discovery: Record<string, unknown> | null = {
    issuer: ISSUER,
    authorization_endpoint: AUTHORIZATION_ENDPOINT,
    token_endpoint: TOKEN_ENDPOINT,
    end_session_endpoint: END_SESSION_ENDPOINT,
  };

  const handler = vi.fn(async (input: unknown, init?: RequestInit) => {
    const url = String(input);
    urls.push(url);
    calls.push({ url, init });

    if (url.endsWith("/.well-known/openid-configuration")) {
      if (discovery === null) {
        return json({ error: "unavailable" }, 503);
      }
      return json(discovery, 200);
    }

    if (url === TOKEN_ENDPOINT) {
      tokenRequests.push(new URLSearchParams(String(init?.body ?? "")));
      const next = queued.shift();
      if (!next) {
        throw new Error(
          "The token endpoint was called with nothing queued. Queue a grant so the test " +
            "says what the identity provider returned instead of relying on a default.",
        );
      }
      return json(next.payload, next.status);
    }

    const answered = options.api?.(url, init) ?? null;
    if (answered) {
      return answered;
    }
    return json({ message: "Nothing here." }, 404);
  });

  return {
    fetch: handler,
    urls,
    calls,
    tokenRequests,
    queueToken(grant) {
      queued.push({ status: 200, payload: grant });
    },
    queueTokenFailure(status, payload = { error: "invalid_grant" }) {
      queued.push({ status, payload });
    },
    setDiscovery(document) {
      discovery = document;
    },
  };
}

export interface StubbedLocation {
  readonly assign: ReturnType<typeof vi.fn>;
  readonly replace: ReturnType<typeof vi.fn>;
  /** The URL passed to the most recent `assign`, parsed. Throws when there was none. */
  lastAssigned(): URL;
}

/**
 * Replace `globalThis.location` with something that records navigation instead of
 * performing it. jsdom throws "not implemented" on a real `assign`, and the sign-in flow
 * is a sequence of navigations, so recording them is how the flow becomes observable.
 */
export function stubLocation(path = "/"): StubbedLocation {
  const url = new URL(path, CONSOLE_ORIGIN);
  const assign = vi.fn();
  const replace = vi.fn();
  vi.stubGlobal("location", {
    href: url.href,
    origin: url.origin,
    protocol: url.protocol,
    host: url.host,
    hostname: url.hostname,
    port: url.port,
    pathname: url.pathname,
    search: url.search,
    hash: url.hash,
    assign,
    replace,
    reload: vi.fn(),
    toString: () => url.href,
  });
  return {
    assign,
    replace,
    lastAssigned() {
      const call = assign.mock.calls.at(-1);
      if (!call) {
        throw new Error("Nothing was assigned to location, so there is no URL to read.");
      }
      return new URL(String(call[0]), CONSOLE_ORIGIN);
    },
  };
}

export interface LoadedConsole {
  readonly session: typeof import("../../src/auth/session");
  readonly pkce: typeof import("../../src/auth/pkce");
  readonly constants: typeof import("../../src/auth/constants");
  readonly client: typeof import("../../src/api/client");
  readonly location: StubbedLocation;
  readonly idp: FakeIdp;
}

export interface LoadOptions {
  readonly path?: string;
  readonly issuer?: string;
  readonly idp?: FakeIdp;
}

/** A fresh copy of the sign-in modules, wired to a stand-in provider and location. */
export async function loadConsole(options: LoadOptions = {}): Promise<LoadedConsole> {
  vi.resetModules();
  vi.stubEnv("VITE_KEYCLOAK_ISSUER", options.issuer ?? ISSUER);
  const location = stubLocation(options.path ?? "/");
  const idp = options.idp ?? fakeIdentityProvider();
  vi.stubGlobal("fetch", idp.fetch);

  const session = await import("../../src/auth/session");
  const pkce = await import("../../src/auth/pkce");
  const constants = await import("../../src/auth/constants");
  const client = await import("../../src/api/client");
  return { session, pkce, constants, client, location, idp };
}

export interface SignInOptions {
  readonly accessToken?: string;
  readonly refreshToken?: string;
  readonly idToken?: string;
  readonly expiresIn?: number;
  readonly returnTo?: string;
}

/**
 * Perform a whole sign-in through the module's own code, and return where it lands.
 *
 * The `state` is read back out of the authorisation URL rather than invented, so the value
 * the callback is checked against is the value the redirect actually carried.
 */
export async function signIn(
  loaded: LoadedConsole,
  options: SignInOptions = {},
): Promise<string> {
  loaded.idp.queueToken({
    access_token: options.accessToken ?? "ACCESS-TOKEN-1",
    refresh_token: options.refreshToken ?? "REFRESH-TOKEN-1",
    id_token: options.idToken ?? "ID-TOKEN-1",
    expires_in: options.expiresIn ?? 300,
  });
  await loaded.session.beginSignIn(options.returnTo ?? "/");
  const returnedState = loaded.location.lastAssigned().searchParams.get("state") ?? "";
  return await loaded.session.completeSignIn(
    new URLSearchParams({ code: "AUTHORISATION-CODE", state: returnedState }),
  );
}

/** Every key and value currently held in both browser stores, for a leak check. */
export function everythingInStorage(): { keys: string[]; values: string[] } {
  const keys: string[] = [];
  const values: string[] = [];
  for (const store of [globalThis.localStorage, globalThis.sessionStorage]) {
    for (let index = 0; index < store.length; index += 1) {
      const key = store.key(index);
      if (key === null) {
        continue;
      }
      keys.push(key);
      values.push(store.getItem(key) ?? "");
    }
  }
  return { keys, values };
}
