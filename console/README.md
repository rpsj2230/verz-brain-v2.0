# The console

The browser application. React and Vite, a typed client generated from the API's own
OpenAPI document, sign-in through Keycloak with authorisation code and PKCE, and a light
and dark theme driven by tokens.

Task ids: M32.5.1.1, M32.5.1.2, M32.5.1.3, M32.5.1.4.

**This has been installed, generated, typechecked, built and tested. It has still never
been opened in a browser.** `npm install`, `npm run api:generate`, `npm run typecheck`,
`npm run check:boundaries`, `npm run build` and `npm test` all run clean, on Node 24. What
no check in this directory can reach is the rendering itself: no page has been painted, no
colour contrast measured, and no sign-in performed against a real Keycloak. The sections at
the end say exactly what that leaves unverified and what a reviewer still has to check.

A note on spelling, so the inconsistency does not read as carelessness: prose is British,
and protocol identifiers are spelled the way the protocol spells them. `authorization_code`
and `authorization_endpoint` are names in somebody else's specification and are not ours to
anglicise.

---

## The rule this console is built around

**The console is not a trust boundary.** Every permission decision belongs to the API.

A console that hides a button is a courtesy. A console that decides what may be fetched is
a second permission model, computed from a copy of the rules that nobody keeps in step with
the real ones, in the one place an attacker can edit. When the two disagree, the browser's
copy is the one that quietly wins in a support conversation and the one that quietly loses
in an incident.

Concretely, in this codebase:

- Nothing reads the contents of a token. Not for a name, not for a role, not for an expiry.
  The console holds an opaque string, sends it, and does what the API answers.
- The navigation lists every section to everybody. Filtering it by role would mean reading
  a token to decide what exists. `src/layout/Shell.tsx` argues this at length.
- `src/api/client.ts` has no allow-list of paths and no check of anything about the caller.
- A 404 is rendered with the API's own words and no interpretation. `handle_brain_error`
  maps DENIED and ABSENT to the same status with the same body, deliberately, and the way
  a console breaks that is never by writing "access denied": it is by being helpful.
  "You may not have permission to view this" turns one status code back into two answers.

The test of whether a client-side guard is doing security work: remove it and see whether
what the server returns changes. Remove `RequireSession` and requests go out with no bearer
token, the API refuses them, and the console shows refusals instead of a sign-in prompt.
Nothing is exposed. That is the correct amount of power for a browser to have.

`scripts/check-boundaries.mjs` turns the parts of this that are mechanical into a check.

---

## Running it

```
npm install
npm run api:generate      # needs uv and this repository; no server, no network
npm run dev
```

`npm run api:generate` runs two steps. The first is Python and exports the API's internal
OpenAPI document into `src/api/generated/`; the second turns it into TypeScript types. On
Windows, `npm run` executes scripts through `cmd.exe`, so the `&&` inside the script works
even though typing the same thing into PowerShell 5.1 would be a parser error. If you run
the halves by hand, run them as two commands.

Other scripts: `npm run build` (typecheck then bundle), `npm run typecheck`,
`npm run preview`, `npm run check:boundaries`, `npm test` (`npm run test:watch` while
working).

**`npm run build` fails until you have generated the schema at least once.** The error is
`Cannot find module './generated/schema'` from `src/api/schema.ts`, and that file's comment
says what to do about it. This is deliberate: see below.

---

## The typed client

### Generation is a development-time step, and cannot be anything else

`brain.app.create_app` sets `openapi_url=None` when the environment is production. There is
no schema route on a deployed instance, so a console that generated its client by fetching
one would work against staging and fail against production. That closed route is not an
oversight to work around: `brain.openapi.A_SCHEMA_IS_A_PERMISSION_MAP` explains that the
generated document is a complete inventory of paths, operations, models and field names,
and that serving it unauthenticated in a permission-aware system tells an attacker exactly
which door is worth their time. It was served that way once.

So `scripts/export-openapi.py` builds the document from the application object in this
repository, at the commit the console is being built against. `FastAPI.openapi()` does not
consult `openapi_url`, so this works even against a production-configured `Settings`, and
`create_app` builds routes only, so no socket is opened and no database is touched.

It exports the **internal** document, not the public projection. The public one is
deliberately just the already-unauthenticated routes with its component schemas pruned;
generating a client from it would produce a typed client for the health check, for ever,
and the omission would look like an empty API rather than the wrong audience.

### What there is to be typed against

Two operations, both under `/api/v1` and both behind the gate:

- `GET /api/v1/me`, which answers who the bearer token belongs to, at what assurance, on
  which channel ceiling, and the digest of the reach the request was computed at. It
  deliberately publishes no list of capabilities: see `brain.api_routes.CallerView`.
- `GET /api/v1/records/{entity}`, which answers a page of records already through the
  redactor. `RecordPage` is `Page` plus the payload's own fields, and it never carries a
  total, because a count behind a permission predicate tells the reader how many rows they
  were not shown.

**This section used to say there was nothing at all**, and that was true until the routes
landed on 2026-09-06. It is worth knowing what is still missing rather than assuming the
surface is finished: the deployed application has no signature verifier and no entitlement
store wired, so it refuses every credential, and it registers no row tool, so every entity
answers 404. Both refusals are the correct fail-closed behaviour and neither is useful yet.

### Types, not a generated runtime client

`openapi-typescript` emits types and no runtime code. The transport is `src/api/client.ts`,
about eighty lines, hand-written.

Rejected: a generated runtime client such as `openapi-typescript-codegen` or
`@hey-api/openapi-ts`. They bring their own base URL handling, their own retries and their
own opinion about what a status code means, and the opinion this system needs about 404 is
not one any generator holds. A generated client that treats 404 as "not found" and says so
in a default error message reintroduces the distinction the error taxonomy exists to remove.

The cost is that `request<T>()` takes the response type from the caller rather than
deriving it from the path, so a call site can name a path and a type that do not go
together. That is a real hole and it is the accepted trade. If the call sites outgrow it,
`openapi-fetch` types the pair together, uses the same generated types, and is a change to
one file rather than a rewrite.

### The generated output is not committed

`src/api/generated/` is ignored. A committed copy is correct until somebody changes a
response model without rerunning the generator, and from then on the console compiles
against an API that no longer exists while every check stays green. Regenerating needs no
server and no network, so keeping a stale copy for convenience buys nothing.

The consequence is that the build fails on a fresh checkout until the generator runs. That
is the loud failure and it was chosen over the quiet one.

**If CI is ever added for this directory, the Python export step must run before the
JavaScript build.** No CI has been added here; see the last section.

### The lock text is checked rather than trusted

`brain.core.redaction.LOCK_TEXT` is the only definition of what a withheld field says. The
console renders the lock itself, so it holds a copy in `src/ui/Lock.tsx`. There is no
shared artefact between Python and a browser bundle to carry the string, so the export
script compares the two on every run and refuses to generate anything if they have drifted.
It matches the whole export statement rather than searching for the word, because a check a
comment can satisfy is not a check.

---

## Sign-in

### It matches the realm exactly

Everything below is a fact about `ops/keycloak/realm-export.json`, which was imported and
read back against Keycloak 26.0 on 2026-09-06.

| The realm says | The console does |
| --- | --- |
| `clientId: brain-console` | Hard-coded in `src/auth/constants.ts`, not configurable |
| `publicClient: true` | No client secret anywhere; PKCE instead |
| `standardFlowEnabled: true` | `response_type=code`, and no other path exists |
| `implicitFlowEnabled: false` | No fragment handling, no fallback |
| `directAccessGrantsEnabled: false` | No sign-in form, no password field, ever |
| `pkce.code.challenge.method: S256` | S256, never `plain` |
| `redirectUris: .../auth/callback` | The router's `/auth/callback`, exact match |
| `post.logout.redirect.uris: .../signed-out` | The router's `/signed-out`, exact match |
| `revokeRefreshToken` with `refreshTokenMaxReuse: 0` | Refresh is single-flight |
| `accessTokenLifespan: 300` | Refreshed thirty seconds before expiry |

The client id is a constant rather than an environment variable, and that is the decision
in this file most likely to be undone by somebody being helpful. A client id is not a
deployment detail; it is a reference to a specific set of flow settings. Pointing this
console at a different client with `VITE_KEYCLOAK_CLIENT_ID` would silently move it to a
client that may have implicit enabled or a direct grant, and nothing in the browser can
tell the difference, because the browser is not the thing being protected.

### Tokens live in memory

No token is written to any browser storage. The only things stored are the PKCE verifier
and the `state` value, in the tab's session store, because they must survive a full page
navigation, and they are deleted the moment they are read.

Rejected: a refresh token in the local store so that a reload is seamless. The realm gives
an SSO session ten hours, so that would be a ten-hour credential readable by any script on
the origin, and an XSS bug would stop being a session hijack and become a portable
credential.

Rejected: silent renewal in a hidden iframe. It is the older answer to the same problem and
browsers have blocked the third-party cookie it depends on.

The cost is a page load. Reloading the tab loses the token and the console bounces through
Keycloak again. That bounce is a top-level navigation carrying a first-party cookie, so it
is invisible while the SSO session is alive.

### Known gap: the cross-tab refresh race

Refresh is single-flight **within one tab**. Two tabs cannot await each other's promise, and
with `revokeRefreshToken` and `refreshTokenMaxReuse: 0` a refresh token presented after it
has been exchanged invalidates the chain. Two tabs whose tokens expire at about the same
time can therefore sign each other out.

This is not fixed and it is not hidden. The two ways to fix it:

1. Elect one refreshing tab with a `BroadcastChannel` and share the result. Perhaps thirty
   lines, and it narrows the window rather than closing it.
2. Move token custody to a backend-for-frontend, so the browser holds a cookie and never a
   refresh token. That closes it completely, and it changes `brain-console` from a public
   client to a confidential one, which is a realm change and somebody else's decision.

Neither was written speculatively. Option 2 in particular is a design decision about the
whole deployment, not a console detail.

### Known gap: nobody can sign in yet

`redirectUris` and `webOrigins` in the realm are `https://brain.example.invalid`, and the
file says the placeholders are deliberate, because a real hostname in a file that is copied
between environments becomes a stale redirect URI, which is an open redirect. That is the
right call and it means that **as committed, the realm cannot complete a sign-in from any
address, including localhost.** Before the console can be used at all, somebody with access
to Keycloak has to register the real origin and, for local work, the development origin.

That is why the dev server sets `strictPort`. A dev server that quietly moves from 5173 to
5174 produces a Keycloak error page that reads as a realm misconfiguration rather than as a
second copy of `npm run dev`.

### Something a reviewer should check in the realm, not here

The audience mapper that puts `brain-api` into the `aud` claim is declared inside the
`brain-api` client's own `protocolMappers`. A client's dedicated mappers apply to tokens
issued **for that client**, and `brain-api` has every flow disabled, so it never requests a
token. If that is right, tokens minted for `brain-console` will not carry `aud: brain-api`,
and `validate_token` will refuse every one of them for `WRONG_AUDIENCE` after a sign-in that
appeared to succeed.

I could not test this: it needs a running Keycloak and a running API, and I had neither.
The fix, if the reading is correct, is to move that mapper onto a client scope that
`brain-console` actually has, such as `brain-identity`, or onto `brain-console`'s own
mappers. **This is a note about the realm file, which I did not touch.**

The console does handle the symptom without hiding it: a 401 forgets the session, the guard
starts a fresh sign-in, and `MAX_SIGN_IN_ATTEMPTS` turns what would be an infinite redirect
loop into a readable message.

### Other decisions

**Hand-written, not `keycloak-js` or `oidc-client-ts`.** The dangerous part of an OIDC
client is ID token validation, and this console does not do any: it never reads the ID
token, so that whole class of bug is absent rather than handled. What remains is a redirect
with a PKCE challenge, a `state` comparison, a form post to the token endpoint and a
refresh. That is small enough to read in one sitting, and reading it is the only review
available for a dependency nobody here could install or audit either. `keycloak-js` in
particular defaults to iframe-based session checking that third-party cookie policy has
already broken.

This is a genuine trade and it can go the other way. If a reviewer would rather depend on
`oidc-client-ts`, the shape of `src/auth/session.ts` is close to its API and the swap is
contained.

**No `nonce`.** A nonce binds an ID token to a request, and it is worth what the check on
the way back is worth. This console never reads the ID token, so a nonce it never verifies
would be decoration that reads as protection. Anything that starts reading the ID token
must add both halves in the same change.

**The ID token is kept, and never parsed.** It is sent as `id_token_hint` at sign-out so
Keycloak can end the specific session without showing its confirmation page. When there is
none, `client_id` is sent instead, which Keycloak 26 also accepts as a condition for
honouring `post_logout_redirect_uri`.

**Sign-out is not local.** Forgetting the token here while the SSO session lives on is the
bug that looks like a feature: the next visit signs straight back in with no prompt, which
on a shared machine is not a sign-out at all.

---

## Routing and layout

`createBrowserRouter` with three top-level routes: the callback, the signed-out page, and
everything else behind `RequireSession` inside the shell.

The two sign-in routes sit outside the guard, and both would be bugs inside it: the
callback is where a session comes from, so guarding it is a loop, and the signed-out page
exists because there is no session, so guarding it would sign the person back in and undo
what they just did.

**Whatever serves the built files must return `index.html` for an unknown path.** Every
route here is client-side. Without that fallback a deep link 404s, and so does
`/auth/callback`, which means sign-in completes at Keycloak and lands on a page that does
not exist.

The console is assumed to be served from the same origin as the API: the realm registers
`https://brain.example.invalid/auth/callback`, and the API serves `/api/v1` on that host.
That is why `VITE_API_BASE_URL` is a path and why `BRAIN_CORS_ORIGINS` can stay empty. The
dev server proxies `/api` to reproduce the same shape locally rather than making every
laptop the only place cross-origin behaviour is ever exercised. Splitting the two in a
deployment means changing `BRAIN_CORS_ORIGINS` on the API **and** `webOrigins` in the
realm, neither of which fails loudly on its own.

---

## Theme

Tokens in `src/theme/tokens.css`; nothing else defines a colour. Three states, because a
person can choose light, choose dark, or choose nothing and follow the machine, and the
third is the default that two-position toggles have to lie about.

The stored choice is applied by a blocking script in `index.html` before the first paint,
which is the only way to avoid the flash of the wrong theme. That script cannot import a
module and still block paint, so the storage key is written twice;
`scripts/check-boundaries.mjs` asserts the two literals match.

Rejected: `light-dark()`, which would state each colour pair once instead of writing the
dark block twice. Its support is fine by now and its failure mode is not: a browser that
does not know the function treats the declaration as invalid, so the page falls back to no
colour rather than to a wrong colour and renders as unreadable text on an unpainted
background. I could not open this in a browser to find out.

**The lock is the part of the theme that is a rule rather than a preference.** A withheld
field renders through `src/ui/Lock.tsx`, which takes no props, into a single `.lock` class
with no modifiers. `brain.core.redaction.render_lock` takes no arguments for exactly this
reason: a lock that varied by field or by reason would make its own shape a side channel,
and two people comparing screens could read off which of them was refused for which reason.
`.lock--out-of-scope` would be that leak written in CSS. The lock's colours differ between
light and dark because the whole page does; they never differ between two fields on one
screen.

---

## What is NOT done

- **No browser has run any of this.** The suite below runs under jsdom, which parses CSS
  and matches selectors but does not evaluate a media query or compute a colour from a
  custom property. So "an explicit light choice wins on a dark machine" is checked as a
  fact about the rule and the selector, not as a painted page, and nothing here measures
  contrast or catches a layout that collapses.
- **No CI job, and `.github/` was not touched.** Whether this repository grows a JavaScript
  pipeline is a decision that has not been made, and making it by adding a workflow file
  would be making it quietly.
- **Not in the Dockerfile and not in any compose file.** The image copies `src`,
  `migrations`, `docs` and `alembic.ini` by name, so nothing here reaches it. How the built
  assets are served, and from what origin, is undecided, and it interacts with the realm's
  registered redirect URI.
- **No Content-Security-Policy.** A meta tag cannot express `frame-ancestors` and cannot be
  templated per deployment without failing open when the variable is missing, so the policy
  belongs on the response headers of whatever serves these files. A starting point:
  `default-src 'self'; connect-src 'self' <keycloak origin>; img-src 'self' data:;
  style-src 'self' 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'`. Note that the
  pre-paint theme script is inline and needs its hash in `script-src`, and that
  `style-src 'unsafe-inline'` is there because Vite injects styles that way in development.
- **No screens.** Two routes, one of which says it is not built yet. No grid, no forms, no
  trace graph: those are M32.5.2 and a different piece of work.
- **No display of who is signed in.** The header says nothing about the person, because the
  only way to know without asking the API is to read the token. When an endpoint exists that
  says who the caller is, that is where the name comes from.
- **No `/api/v1` calls anywhere.** `src/api/client.ts` is wired and unused. Two operations
  are now mounted and typed, so this is a gap in the console rather than in the API.
- **No error reporting, no analytics, no telemetry.**
- **`vite.config.ts` is not typechecked**, deliberately: including it would mean adding
  `@types/node` and a second tsconfig. A mistake in it surfaces when the build fails rather
  than when `npm run typecheck` does.
- **The lock sample on the overview page is temporary.** It exists so the lock can be seen
  in both themes before any record renders. Delete it when a real record renders anywhere.

---

## What is unverified

Everything that needs a browser or a live identity provider. The list is shorter than it
was; items 1, 2, 4 and 5 of the original eight are now verified and are recorded above.

1. **No sign-in has ever been performed against a real Keycloak.** The flow is exercised
   end to end in the suite against a stand-in provider that behaves the way the realm file
   describes, which checks this console's half of the conversation and nothing about
   Keycloak's. It cannot be checked for real until the redirect URIs are registered.
2. **No page has been painted.** jsdom parses the stylesheets and matches selectors, so
   the theme's rules are checked as rules, but it evaluates no media query and computes no
   colour. A stylesheet that is structurally correct and visually broken passes everything
   here.
3. **Colour contrast is estimated, not measured.** The palettes look comfortably above the
   AA threshold by inspection. Nobody has run a contrast checker over them.
4. **No accessibility audit.** There is a skip link, a labelled navigation landmark, a
   radio group with a legend, visible focus rings, `prefers-reduced-motion`, and current
   state marked by more than colour. The suite checks the skip link is first and that the
   current section is marked by `aria-current` rather than only by colour. None of it has
   been through a screen reader.
5. **The cross-tab refresh race is not fixed and is not tested.** Two tabs cannot await
   each other's promise, and a test with one module graph cannot reproduce two tabs. See
   the section above for the two ways to fix it.

---

## What a reviewer must check before this is trusted

1. `npm install`, `npm run api:generate`, `npm run typecheck`, `npm run build`, `npm test`.
   All of these pass as committed; a failure means something in your environment differs,
   most likely the Node version.
2. `npm run check:boundaries`, and read the rules while doing it. A rule nobody can justify
   is one the next person deletes to make a build pass.
3. That the client id in `src/auth/constants.ts` is still the realm's, and that
   `CALLBACK_PATH` and `SIGNED_OUT_PATH` still match the registered URIs character for
   character.
4. The audience mapper question above. It decides whether any token this console obtains is
   accepted by the API at all.
5. That a real sign-in works end to end against a real Keycloak, including a token refresh
   after five minutes and a sign-out that actually ends the SSO session (open the console
   again afterwards: if it signs in with no prompt, the sign-out did not work).
6. That nothing in `src/` reads a token's contents. The boundary check covers the obvious
   spellings and cannot cover an inventive one.
7. That the deployment serves `index.html` for unknown paths, over HTTPS, with a CSP.

---

## The tests

Vitest, `@testing-library/react` and jsdom, all pinned exactly. `npm test` runs them;
`tests/` holds them, one file per property group, and they are named as property sentences
with a docstring saying what breaks if each is deleted, in the style the Python side uses.

They are not typechecked, which matches the Python side, where `mypy` runs over `src` and
not over `tests`. `tsconfig.json` therefore still covers `src` only, and the decision
recorded in it about not pulling Node's types into the application's typecheck stands.

**The ten tests this section used to ask for all exist.** They are spread across the files
below and several grew siblings, because a guard tested only by its refusals is satisfied
by a function that refuses everything.

| File | What it holds |
| --- | --- |
| `tests/lock.test.tsx` | The lock is identical in every context, takes no props, says what the backend says, and its class has no modifiers. |
| `tests/theme.test.ts` | Three states, the `:not([data-theme="light"])` guard, every token defined on the bare `:root`, and the two dark blocks in step. |
| `tests/auth-realm.test.ts` | Every constant in `src/auth/constants.ts` against `ops/keycloak/realm-export.json`. |
| `tests/auth-pkce.test.ts` | The S256 challenge against the RFC 7636 vector, single use, and the attempt window. |
| `tests/auth-session.test.ts` | No token in any store, `state` verified, single-flight refresh, the return-address guard, the loop guard, sign-out. |
| `tests/api-errors.test.tsx` | The fallback sentences against `brain.core.errors`, and that nothing is added to a 404 on the way to the screen. |
| `tests/api-client.test.ts` | The bearer token, no cookies, no allow-list, a refusal as a value, and what a 401 does. |
| `tests/shell-navigation.test.tsx` | The navigation is identical for every session, on every page, and names nobody. |
| `tests/routing.test.tsx` | Deep links, the console's own 404, and the two routes that must stay outside the guard. |
| `tests/config.test.tsx` | The issuer rules, and that a misconfigured console names the variable on the screen. |
| `tests/startup.test.ts` | `src/main.tsx`, which nothing else reaches: the theme applied before the first render, and the contract with `index.html`. |

**Several constants are checked against the thing they are a copy of, not against
themselves.** That is the point of `tests/support/python.ts` and the realm parsing in
`tests/auth-realm.test.ts`: a test that imports `LOCK_TEXT` and compares it with
`LOCK_TEXT` is green for every value the constant could hold. The lock text and the failure
sentences come out of the Python source, the client id and the registered paths out of the
realm export, the PKCE vector out of RFC 7636, and the default API base out of
`.env.example`.

**Stylesheets are parsed rather than searched.** Both style files name the thing they
forbid in a comment in order to forbid it, so a substring search for
`:root:not([data-theme="light"])` or for `.lock--out-of-scope` would be satisfied by the
comment with the real rule gone. `tests/support/css.ts` strips comments and reads rules,
and the theme test hands the selector it found to the browser's own matcher.

**Fifty-one mutations were run against this suite and fifty were caught by a specifically
named test.** The one survivor is `end={section.to === "/"}` in `src/layout/Shell.tsx`
changed to `end={false}`: measured against react-router-dom 6.30, a prefix match on `/`
requires a `/` at the boundary, so the two spellings mark exactly the same link current at
every address this console has. The expression is defensive against a router upgrade that
changes that rule, and the test that would fail the moment it changed is
`the current section is marked by more than a colour`. Changing `end` for the *other*
sections is not equivalent and is caught.
