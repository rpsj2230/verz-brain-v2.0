/**
 * Where the identity provider's endpoints come from, and the one thing checked about them.
 *
 * **Discovered rather than constructed.** Keycloak's URL shapes
 * (`/realms/<realm>/protocol/openid-connect/auth` and friends) are stable but they are not
 * a contract, and a console that hard-codes them breaks on an upgrade in a way that reads
 * as "sign-in is broken" rather than "a path moved". The cost is one request before the
 * first redirect, cached for the life of the page.
 *
 * Rejected: fetching the document at build time and inlining it. The issuer differs per
 * deployment, and a build that reaches out to a client's Keycloak is a build that cannot
 * run on a laptop with no VPN.
 *
 * **The document's own `issuer` is compared to the configured one, exactly.** That check
 * is the whole of the trust this file places in the response: it says the document
 * describes the realm we think we are talking to. Exact string equality, and deliberately
 * not a normalised or host-only comparison, for the same reason
 * `brain.identity.oidc.validate_token` refuses to be forgiving there. A comparison written
 * to tolerate configuration typos tolerates `https://idp.example.com.attacker.net/` too.
 *
 * This is not a substitute for anything. The document is fetched over TLS from a host the
 * deployment named, and the API validates every token it is shown regardless of what this
 * file believed. Nothing here decides anything.
 */

import { config } from "../config";

/** What the console uses out of the discovery document, and nothing else. */
export interface Endpoints {
  readonly authorization: string;
  readonly token: string;
  /** Absent on a provider with no RP-initiated logout. Sign-out then clears locally only. */
  readonly endSession: string | null;
}

const DISCOVERY_PATH = "/.well-known/openid-configuration";

/** Loopback is allowed for local development only; see `config.ts` for the same rule. */
function isSecureEndpoint(url: string): boolean {
  return (
    url.startsWith("https://") ||
    url.startsWith("http://localhost:") ||
    url.startsWith("http://127.0.0.1:")
  );
}

function readString(document: Record<string, unknown>, key: string): string | null {
  const value = document[key];
  return typeof value === "string" && value.length > 0 ? value : null;
}

async function fetchEndpoints(): Promise<Endpoints> {
  const url = `${config.issuer}${DISCOVERY_PATH}`;
  const response = await fetch(url, { credentials: "omit" });
  if (!response.ok) {
    throw new Error(
      `The identity provider at ${config.issuer} did not return its configuration ` +
        `(${response.status}). Check VITE_KEYCLOAK_ISSUER.`,
    );
  }
  const document: unknown = await response.json();
  if (typeof document !== "object" || document === null) {
    throw new Error(`The configuration at ${url} is not an object.`);
  }
  const fields = document as Record<string, unknown>;

  const declaredIssuer = readString(fields, "issuer");
  if (declaredIssuer !== config.issuer) {
    throw new Error(
      `The provider at ${url} calls itself ${String(declaredIssuer)}, and this console is ` +
        `configured for ${config.issuer}. These must match exactly: the API compares the ` +
        "issuer on every token by exact string equality.",
    );
  }

  const authorization = readString(fields, "authorization_endpoint");
  const token = readString(fields, "token_endpoint");
  if (!authorization || !token) {
    throw new Error(`The configuration at ${url} names no authorisation or token endpoint.`);
  }
  for (const endpoint of [authorization, token]) {
    if (!isSecureEndpoint(endpoint)) {
      throw new Error(`The provider published an insecure endpoint: ${endpoint}`);
    }
  }

  return {
    authorization,
    token,
    endSession: readString(fields, "end_session_endpoint"),
  };
}

/**
 * Cached as a promise rather than as a result, so that two callers during startup share
 * one request instead of racing to make two. The cache is the page's lifetime: a reload
 * refetches, which is the right granularity for a document that changes when somebody
 * upgrades Keycloak.
 */
let inFlight: Promise<Endpoints> | null = null;

export function endpoints(): Promise<Endpoints> {
  inFlight ??= fetchEndpoints().catch((error: unknown) => {
    // A failed discovery must not be cached. Otherwise one flaky request at startup makes
    // sign-in impossible until the page is reloaded, which nobody thinks to do.
    inFlight = null;
    throw error;
  });
  return inFlight;
}
