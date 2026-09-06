/**
 * Build and dev-server configuration, and three decisions that are not defaults.
 *
 * **The dev server holds its port rather than moving.** `strictPort` is on because the
 * whole sign-in flow is pinned to an origin: Keycloak matches `redirect_uri` exactly
 * against the registered list, and `webOrigins` decides which origin may call the token
 * endpoint at all. A dev server that quietly moves from 5173 to 5174 when the port is busy
 * produces an invalid_redirect_uri page from Keycloak, which reads as a realm
 * misconfiguration rather than as a second copy of `npm run dev` in another terminal.
 * Failing to start is the honest failure.
 *
 * **The API is proxied in development rather than called cross-origin.** In production the
 * console is served from the same origin as the API: the realm registers
 * `https://brain.example.invalid/auth/callback` as the console's redirect URI, and the API
 * lives at `/api/v1` on that same host. The proxy reproduces that shape locally, so
 * development exercises the same-origin path the deployment uses, and `BRAIN_CORS_ORIGINS`
 * stays empty. Rejected: pointing the browser straight at `http://127.0.0.1:8000`. It
 * works, and it makes every developer's laptop the only place CORS is ever exercised, so
 * the first cross-origin bug appears in production.
 *
 * **No source maps in the production build.** Marginal either way: the bundle already
 * names every endpoint it calls, so a source map discloses no new fact. It doubles the
 * static surface a host serves, and hosts serve `.map` files unauthenticated by default.
 * If a debugging session needs them, turn them on for that build and turn them off again.
 */

import react from "@vitejs/plugin-react";
import { defineConfig, loadEnv } from "vite";

/**
 * Where `npm run dev` sends `/api`. The application's own default port; see
 * `src/brain/serve.py` and `docker-compose.yml`.
 */
const DEFAULT_DEV_API_TARGET = "http://127.0.0.1:8000";

/**
 * The dev origin. It has to be registered in the realm before sign-in works locally, and
 * as committed the realm registers only a placeholder host, so this number is part of a
 * conversation with whoever administers Keycloak rather than a free choice. See README.
 */
const DEV_PORT = 5173;

export default defineConfig(({ mode }) => {
  // Only VITE_-prefixed values reach the client bundle. This call reads the wider set with
  // an empty prefix, and everything it returns is used here, in the Node process that runs
  // the dev server, and is never inlined into anything the browser receives.
  const env = loadEnv(mode, ".", "");
  const apiTarget = env.DEV_API_PROXY_TARGET || DEFAULT_DEV_API_TARGET;

  return {
    plugins: [react()],
    server: {
      port: DEV_PORT,
      strictPort: true,
      proxy: {
        "/api": {
          target: apiTarget,
          changeOrigin: false,
        },
      },
    },
    build: {
      // Explicit rather than inherited, so an upgrade of the toolchain cannot silently
      // change what the emitted bundle assumes. ES2022 covers `crypto.subtle`, top-level
      // await and class fields, which is everything `src/auth` needs.
      target: "es2022",
      sourcemap: false,
      outDir: "dist",
    },
  };
});
