/// <reference types="vite/client" />

/**
 * The environment variables this console reads, declared so that the set is written down
 * somewhere a reader can find it and so that each one has a type at the point of use.
 *
 * **This does not catch a typo, and it would be comfortable to pretend otherwise.** Vite's
 * own `ImportMetaEnv` carries a string index signature, so `import.meta.env.VITE_ISUER`
 * type-checks as `any` no matter what is declared here. The thing that actually contains
 * the risk is that both names are read in exactly one file, `src/config.ts`, which
 * validates them and refuses to start when they are wrong.
 *
 * Only `VITE_`-prefixed names appear, because only those exist in the browser. Anything
 * else read through `import.meta.env` is undefined however carefully it was set in the
 * shell, and the resulting bug looks like a deployment problem rather than a naming one.
 */
interface ImportMetaEnv {
  readonly VITE_API_BASE_URL?: string;
  readonly VITE_KEYCLOAK_ISSUER?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
