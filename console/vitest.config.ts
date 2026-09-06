/**
 * How the tests run, and the three settings that are not defaults.
 *
 * **A separate file from `vite.config.ts` rather than a `test` block inside it.** That file
 * configures a dev server: a pinned port that refuses to move because the whole sign-in
 * flow is tied to an origin, and a proxy that reproduces the deployment's same-origin
 * shape. None of it applies to a test run, and a test run should not be able to fail
 * because port 5173 is busy. The one duplicated line is the React plugin.
 *
 * **`globals: false`.** Every helper a test uses is imported at the top of the file it is
 * used in, so a reader can see where `expect` comes from and a test file is an ordinary
 * module. The cost is that `@testing-library/react` no longer registers its own cleanup,
 * which `tests/setup.ts` therefore does by hand.
 *
 * **`server.fs.allow` reaches the repository root.** The tests read Python sources and the
 * Keycloak realm export, because several constants in this console are copies of facts
 * held there and the only honest way to check a copy is against the original.
 *
 * The tests are not typechecked, and that matches the Python side, where `mypy` runs over
 * `src` and not over `tests`. `tsconfig.json` still covers `src` only, so the decision
 * recorded there about not pulling Node's types into the application's typecheck stands.
 */

import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

const CONSOLE_ROOT = dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = resolve(CONSOLE_ROOT, "..");

export default defineConfig({
  plugins: [react()],
  server: {
    fs: {
      allow: [REPO_ROOT],
    },
  },
  test: {
    environment: "jsdom",
    globals: false,
    include: ["tests/**/*.test.ts", "tests/**/*.test.tsx"],
    setupFiles: ["tests/setup.ts"],
    restoreMocks: true,
    unstubGlobals: true,
    unstubEnvs: true,
  },
});
