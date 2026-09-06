/**
 * What this deployment was built with, and the refusal to start when it is wrong.
 *
 * **Problems are collected and shown rather than thrown.** Throwing at module load in a
 * Vite application produces a blank page and a stack trace in a console nobody has open,
 * and the reported symptom is "the site is down". Naming the variable on the screen puts
 * the message in front of the person who set it.
 *
 * The trailing-slash rule is the one that looks pedantic and is not.
 * `brain.identity.oidc.validate_token` compares `iss` by exact string equality and
 * explicitly refuses to normalise, because a forgiving comparison is how
 * `https://idp.example.com.attacker.net/` gets accepted. A console configured with a
 * trailing slash therefore signs somebody in successfully and then has every token refused
 * by the API, for a reason no browser error mentions.
 *
 * Task ids: M32.5.1.3
 */

import { render } from "@testing-library/react";
import { describe, expect, test, vi } from "vitest";
import { readConsoleFile, extractOne } from "./support/repo";
import { stubLocation } from "./support/auth";

async function configuredWith(
  issuer: string | undefined,
  apiBaseUrl?: string,
): Promise<typeof import("../src/config")> {
  vi.resetModules();
  vi.stubEnv("VITE_KEYCLOAK_ISSUER", issuer ?? "");
  if (apiBaseUrl !== undefined) {
    vi.stubEnv("VITE_API_BASE_URL", apiBaseUrl);
  }
  return await import("../src/config");
}

describe("the issuer", () => {
  test("a properly configured console reports no problems", async () => {
    // What breaks if this is deleted: every refusal below is satisfied by a validator that
    // refuses everything, and the console would never start anywhere. This is the sibling
    // that proves a correct value passes.
    const config = await configuredWith("https://keycloak.example.com/realms/brain");

    expect(config.configProblems).toEqual([]);
    expect(config.config.issuer).toBe("https://keycloak.example.com/realms/brain");
  });

  test("an issuer with a trailing slash is refused", async () => {
    // What breaks if this is deleted: the console builds a discovery URL with a doubled
    // slash, some servers answer it, sign-in appears to work, and then the API refuses
    // every token because it compares the issuer by exact string equality. Nothing in the
    // browser mentions a slash.
    const config = await configuredWith("https://keycloak.example.com/realms/brain/");

    expect(config.configProblems).toHaveLength(1);
    expect(config.configProblems[0]).toContain("VITE_KEYCLOAK_ISSUER");
  });

  test("an issuer that is not https is refused", async () => {
    // What breaks if this is deleted: an access token travels over plain HTTP and is
    // readable by everything between this browser and the identity provider. A deployment
    // that did this would work perfectly, which is why nothing else would catch it.
    const config = await configuredWith("http://keycloak.example.com/realms/brain");

    expect(config.configProblems).toHaveLength(1);
    expect(config.config.issuer).toBe("");
  });

  test("a loopback issuer is allowed for local work", async () => {
    // What breaks if this is deleted: a developer running Keycloak on their own machine,
    // where there is no certificate to have, cannot start the console at all, and the
    // pressure to relax the https rule for everybody arrives immediately.
    for (const issuer of [
      "http://localhost:8080/realms/brain",
      "http://127.0.0.1:8080/realms/brain",
    ]) {
      const config = await configuredWith(issuer);
      expect(config.configProblems).toEqual([]);
    }
  });

  test("a missing issuer is refused rather than guessed", async () => {
    // What breaks if this is deleted: a default identity provider. There is no sensible
    // guess, and a console pointed at the wrong one redirects somebody's browser to a host
    // nobody chose.
    const config = await configuredWith(undefined);

    expect(config.configProblems).toHaveLength(1);
    expect(config.configProblems[0]).toContain("VITE_KEYCLOAK_ISSUER");
  });
});

describe("the API base", () => {
  test("the default API base is the one the example environment documents", async () => {
    // What breaks if this is deleted: the code's default and the documented default drift
    // apart, and a deployment that copies `.env.example` verbatim gets a different shape
    // from one that sets nothing. The expected value is read out of `.env.example` rather
    // than restated here, so this is not a constant compared with itself.
    const documented = extractOne(
      readConsoleFile(".env.example"),
      /^VITE_API_BASE_URL=(.+)$/m,
      "VITE_API_BASE_URL in .env.example",
    ).trim();

    const config = await configuredWith("https://keycloak.example.com/realms/brain");
    expect(config.config.apiBaseUrl).toBe(documented);
  });
});

describe("a console that cannot work", () => {
  test("a misconfigured console names the variable on the screen", async () => {
    // What breaks if this is deleted: the failure becomes a redirect to
    // `undefined/.well-known/openid-configuration` and a browser error nobody can act on.
    // The useful thing to do about a console pointed at no identity provider is to say so,
    // naming the setting, to the person who deployed it.
    stubLocation("/");
    vi.resetModules();
    vi.stubEnv("VITE_KEYCLOAK_ISSUER", "");
    const { App } = await import("../src/App");

    const { container } = render(<App />);

    expect(container.textContent).toContain("VITE_KEYCLOAK_ISSUER");
    expect(container.textContent).toContain("not configured");
  });
});
