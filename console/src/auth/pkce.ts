/**
 * The three values that have to survive a full page navigation to Keycloak and back, and
 * the only place in this console that writes to browser storage for sign-in.
 *
 * **Why storage at all.** The authorisation code flow leaves the page. The PKCE verifier
 * is generated before the redirect and needed after it, in a fresh JavaScript context, so
 * it cannot be held in a variable. That is the whole reason this file exists.
 *
 * **Why the session store and not the local one.** Session storage is scoped to the tab
 * and cleared when the tab closes. A verifier left in the local store outlives the sign-in
 * that needed it, is readable by every tab, and survives a shared machine being handed to
 * the next person. None of that buys anything: the verifier is useful for exactly one code
 * exchange, and it is deleted the moment it is read.
 *
 * **No token is ever written to any storage, here or anywhere else.** Access and refresh
 * tokens live in memory in `session.ts` and die with the page. See the argument there;
 * `scripts/check-boundaries.mjs` refuses a storage access anywhere but this file, so a
 * later "just persist the session" change is a diff that fails a check rather than one
 * that quietly works.
 *
 * **The verifier is a secret with a lifetime measured in seconds.** It is what proves the
 * browser that redeems the code is the browser that asked for it. Nothing else in the flow
 * does: the client is public, so there is no client secret, which is exactly why the realm
 * forces `pkce.code.challenge.method` to S256.
 */

import {
  PENDING_SIGN_IN_KEY,
  SIGN_IN_ATTEMPTS_KEY,
  SIGN_IN_ATTEMPT_WINDOW_SECONDS,
} from "./constants";

/**
 * How many random bytes go into a verifier and into `state`. RFC 7636 allows a verifier
 * of 43 to 128 characters; 32 bytes encode to 43, which is the floor, and the floor here
 * is 256 bits of entropy from the platform generator. More would not make it less
 * guessable in any sense anybody can measure.
 */
const RANDOM_BYTES = 32;

/** What was in flight when the browser left for Keycloak. */
export interface PendingSignIn {
  /** The PKCE code verifier. Single use, deleted on read. */
  readonly verifier: string;
  /** Echoed by Keycloak and compared on return. An unforgeable callback marker. */
  readonly state: string;
  /** Where the person was going before they were sent to sign in. */
  readonly returnTo: string;
}

/**
 * Thrown when the browser cannot do PKCE at all, which is a configuration fact rather than
 * a bug in this code.
 *
 * `crypto.subtle` exists only in a secure context: https, or localhost. Serving this
 * console over plain http on a LAN address leaves `crypto.subtle` undefined, and the
 * failure would otherwise read as "cannot read properties of undefined", which sends the
 * next person to debug the wrong thing entirely.
 */
export class InsecureContextError extends Error {
  constructor() {
    super(
      "This console needs a secure context for sign-in. Web Crypto, and therefore PKCE, " +
        "is unavailable over plain HTTP on anything but localhost. Serve it over HTTPS.",
    );
    this.name = "InsecureContextError";
  }
}

function base64Url(bytes: Uint8Array): string {
  let binary = "";
  for (const byte of bytes) {
    binary += String.fromCharCode(byte);
  }
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

/** A fresh high-entropy value, URL safe. Used for both the verifier and `state`. */
export function randomToken(): string {
  const bytes = new Uint8Array(RANDOM_BYTES);
  crypto.getRandomValues(bytes);
  return base64Url(bytes);
}

/** The S256 challenge for a verifier: base64url of its SHA-256 digest. */
export async function challengeFor(verifier: string): Promise<string> {
  if (!globalThis.crypto?.subtle) {
    throw new InsecureContextError();
  }
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier));
  return base64Url(new Uint8Array(digest));
}

function store(): Storage | null {
  // Access itself throws in a private window with site data blocked, rather than
  // returning null. Sign-in cannot work without it, and the caller turns this into a
  // readable message instead of an exception from a getter.
  try {
    return globalThis.sessionStorage;
  } catch {
    return null;
  }
}

/** Remember what is in flight, immediately before leaving for the identity provider. */
export function putPending(pending: PendingSignIn): void {
  store()?.setItem(PENDING_SIGN_IN_KEY, JSON.stringify(pending));
}

/**
 * Read the pending sign-in and delete it in the same breath.
 *
 * Single use is the point. A verifier that survives its own exchange can be replayed
 * against a second authorisation code, and a `state` that survives is a `state` that no
 * longer proves this callback belongs to this attempt.
 */
export function takePending(): PendingSignIn | null {
  const storage = store();
  if (!storage) {
    return null;
  }
  const raw = storage.getItem(PENDING_SIGN_IN_KEY);
  storage.removeItem(PENDING_SIGN_IN_KEY);
  if (!raw) {
    return null;
  }
  try {
    const parsed: unknown = JSON.parse(raw);
    if (
      typeof parsed === "object" &&
      parsed !== null &&
      typeof (parsed as PendingSignIn).verifier === "string" &&
      typeof (parsed as PendingSignIn).state === "string" &&
      typeof (parsed as PendingSignIn).returnTo === "string"
    ) {
      return parsed as PendingSignIn;
    }
    return null;
  } catch {
    return null;
  }
}

function recentAttempts(now: number): number[] {
  const raw = store()?.getItem(SIGN_IN_ATTEMPTS_KEY);
  if (!raw) {
    return [];
  }
  try {
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) {
      return [];
    }
    const cutoff = now - SIGN_IN_ATTEMPT_WINDOW_SECONDS * 1000;
    return parsed.filter((at): at is number => typeof at === "number" && at > cutoff);
  } catch {
    return [];
  }
}

/**
 * Record that we are about to bounce to the identity provider, and say how many times we
 * have done so recently. See `MAX_SIGN_IN_ATTEMPTS` for what the count is for.
 */
export function recordSignInAttempt(now = Date.now()): number {
  const attempts = [...recentAttempts(now), now];
  store()?.setItem(SIGN_IN_ATTEMPTS_KEY, JSON.stringify(attempts));
  return attempts.length;
}

/** Called once a sign-in completes, so a later genuine failure starts from zero. */
export function clearSignInAttempts(): void {
  store()?.removeItem(SIGN_IN_ATTEMPTS_KEY);
}
