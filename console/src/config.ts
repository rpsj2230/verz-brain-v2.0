/**
 * What this deployment was built with, and a refusal to start when it is wrong.
 *
 * **Everything here is public.** Vite inlines every `VITE_`-prefixed value into the bundle
 * as plain text, so this file is a list of things anybody who can open the console can
 * read. That is not a compromise: `brain-console` is a public client precisely because a
 * browser cannot keep a secret, which is why the realm gives it PKCE instead of one.
 *
 * **Problems are collected rather than thrown.** A misconfigured console should say which
 * variable is wrong, on the screen, to the person who set it. Throwing at module load in a
 * Vite application produces a blank page and a stack trace in a console nobody has open,
 * and the reported symptom is "the site is down". `configProblems` is checked once, in
 * `App`, before anything that would use these values renders.
 *
 * The one validation that looks pedantic is the trailing slash on the issuer, and it is
 * the one that has a cost attached. `brain.identity.oidc.validate_token` compares `iss` by
 * exact string equality, and explicitly rejects normalising trailing slashes, because a
 * forgiving comparison is how `https://idp.example.com.attacker.net/` gets accepted. So a
 * console configured with a trailing slash builds a discovery URL with a doubled slash,
 * some servers answer it, and the tokens that come back are then refused by the API for a
 * reason no browser error mentions. Refusing it here costs one line and saves that.
 */

/** The default, and the shape a deployment is expected to have. See `.env.example`. */
const DEFAULT_API_BASE_URL = "/api/v1";

/**
 * Hosts where an insecure issuer is allowed. Only a loopback address, and only because a
 * developer running Keycloak locally has no certificate for it. Any other host over plain
 * HTTP means the token is readable in transit by everyone between here and there.
 */
const LOOPBACK_ISSUER_PREFIXES = ["http://localhost:", "http://127.0.0.1:"];

export interface Config {
  /** Where the API is. A path when the console and the API share an origin, which is the
   * intended shape; a full origin only if a deployment splits them. */
  readonly apiBaseUrl: string;
  /** The realm's issuer, with no trailing slash. Discovery is appended to it. */
  readonly issuer: string;
}

function readIssuer(raw: string, problems: string[]): string {
  if (!raw) {
    problems.push(
      "VITE_KEYCLOAK_ISSUER is not set. It is the realm's issuer URL, for example " +
        "https://keycloak.example.com/realms/brain. There is no default, because a " +
        "guessed identity provider is worse than a stopped console.",
    );
    return "";
  }
  if (raw.endsWith("/")) {
    problems.push(
      `VITE_KEYCLOAK_ISSUER ends with a slash (${raw}). The API compares the issuer by ` +
        "exact string equality and will refuse every token minted under a different " +
        "spelling of the same URL. Remove the trailing slash.",
    );
    return "";
  }
  const secure =
    raw.startsWith("https://") ||
    LOOPBACK_ISSUER_PREFIXES.some((prefix) => raw.startsWith(prefix));
  if (!secure) {
    problems.push(
      `VITE_KEYCLOAK_ISSUER is not https (${raw}). An access token sent over plain HTTP ` +
        "is readable by everything between this browser and the identity provider.",
    );
    return "";
  }
  return raw;
}

const problems: string[] = [];

export const config: Config = Object.freeze({
  apiBaseUrl: import.meta.env.VITE_API_BASE_URL || DEFAULT_API_BASE_URL,
  issuer: readIssuer(import.meta.env.VITE_KEYCLOAK_ISSUER ?? "", problems),
});

/** Empty when the console is configured. Rendered as the whole page when it is not. */
export const configProblems: readonly string[] = Object.freeze(problems);
