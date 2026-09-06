/**
 * The rules in this console that are worth more than a paragraph, checked mechanically.
 *
 * **This script has never been run.** There is no Node toolchain on the machine it was
 * written on, so it has been reasoned about and not executed. Treat a first run as part of
 * reviewing it, and treat a failure on first run as a bug in the script until proven
 * otherwise. It has no dependencies, so `node scripts/check-boundaries.mjs` is the whole
 * of what it needs.
 *
 * **Why a grep and not a linter rule.** ESLint would express most of this better and would
 * be another toolchain to pin, configure and keep working. Every rule here is a rule about
 * a literal appearing in a file, which is the one thing a grep is genuinely good at, and
 * the cost of being blunt is a false positive that a written reason can wave through.
 *
 * Each rule says what breaks if it is deleted. That is not decoration either: a rule
 * nobody can justify is a rule the next person removes to make a build pass, and they will
 * be right to.
 *
 * A line may opt out by carrying the marker `boundary-ok:` followed by a reason. The
 * requirement to write the reason is the point, in the same spirit as the repository's
 * rule about explaining a `cast`.
 */

import { readFile, readdir } from "node:fs/promises";
import { dirname, join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const CONSOLE_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const SRC = join(CONSOLE_ROOT, "src");
const INDEX_HTML = join(CONSOLE_ROOT, "index.html");
const THEME_MODULE = join(SRC, "theme", "theme.ts");

const OPT_OUT = "boundary-ok:";

/**
 * Each rule: a name, a pattern, the files allowed to match it, and the sentence that
 * explains why anybody should care. Paths are relative to the console directory and use
 * forward slashes.
 */
const RULES = [
  {
    name: "no token parsing",
    pattern: /\batob\(|jwt-decode|jwtDecode|parseJwt|decodeJwt/,
    allow: [],
    why:
      "The console must never read a token's contents. It holds an opaque string, sends " +
      "it, and does what the API answers. Decoding one is how a browser acquires a second " +
      "permission model: the first role check written against a claim is a rule the API " +
      "never agreed to, and the copy in the browser is the copy an attacker edits.",
  },
  {
    name: "no authorisation decisions in the client",
    pattern: /\b(hasRole|hasPermission|hasCapability|requireRole|isAdmin|canRead)\b/,
    allow: [],
    why:
      "Every permission decision belongs to the API, computed per request from grants " +
      "this browser never receives. A function with one of these names in this codebase " +
      "is a second permission model, and two models disagree eventually and silently.",
  },
  {
    name: "tokens are never stored",
    pattern: /\blocalStorage\b/,
    allow: ["src/theme/theme.ts"],
    why:
      "Access and refresh tokens live in memory and die with the page. The realm gives an " +
      "SSO session ten hours, so a refresh token in the local store would be a ten-hour " +
      "credential readable by any script on this origin. The theme preference is allowed " +
      "there because it is a property of the screen, not of the person.",
  },
  {
    name: "session storage is for the sign-in handshake only",
    pattern: /\bsessionStorage\b/,
    allow: ["src/auth/pkce.ts"],
    why:
      "The PKCE verifier and the state value have to survive a full page navigation, so " +
      "they cannot be held in a variable. Nothing else in this console has that problem, " +
      "and a second writer is how a token ends up in storage by accident.",
  },
  {
    name: "one place talks to the network",
    pattern: /\bfetch\(/,
    allow: ["src/api/client.ts", "src/auth/discovery.ts", "src/auth/session.ts"],
    why:
      "The token is attached in one place and failures are shaped in one place, so a " +
      "reviewer asking what this console can reach reads one file. The second call site " +
      "is always the one that forgets the failure handling.",
  },
  {
    name: "no implicit flow and no password grant",
    pattern: /response_type=token|id_token token|grant_type=password|type="password"/,
    allow: [],
    why:
      "The realm has implicitFlowEnabled false and directAccessGrantsEnabled false on " +
      "every client. The implicit flow puts a token in a URL fragment, which lands in " +
      "browser history and referrer headers; the password grant skips the browser flow " +
      "and therefore the second factor the realm makes mandatory. A password field here " +
      "would collect a credential this page cannot verify it is entitled to see.",
  },
  {
    name: "no raw HTML from a payload",
    pattern: /dangerouslySetInnerHTML/,
    allow: [],
    why:
      "Everything this console renders came from a system of record through the gate. " +
      "Rendering any of it as HTML is script injection with the company's own data as the " +
      "vector, and it also breaks the lock: markup in a field would render as markup.",
  },
];

async function sourceFiles(directory) {
  const found = [];
  for (const entry of await readdir(directory, { withFileTypes: true })) {
    const full = join(directory, entry.name);
    if (entry.isDirectory()) {
      if (entry.name === "generated") {
        // Generated from the API's own document. It is not hand-written code and holding
        // it to these rules would mean editing a file that is rewritten on every run.
        continue;
      }
      found.push(...(await sourceFiles(full)));
    } else if (entry.name.endsWith(".ts") || entry.name.endsWith(".tsx")) {
      found.push(full);
    }
  }
  return found;
}

function relativePath(full) {
  return relative(CONSOLE_ROOT, full).split("\\").join("/");
}

async function checkRules(failures) {
  for (const file of await sourceFiles(SRC)) {
    const shown = relativePath(file);
    const lines = (await readFile(file, "utf8")).split("\n");
    lines.forEach((line, index) => {
      if (line.includes(OPT_OUT)) {
        return;
      }
      for (const rule of RULES) {
        if (rule.pattern.test(line) && !rule.allow.includes(shown)) {
          failures.push({
            where: `${shown}:${index + 1}`,
            rule: rule.name,
            why: rule.why,
            line: line.trim(),
          });
        }
      }
    });
  }
}

/**
 * The theme storage key is written twice: once in a blocking script in index.html that
 * runs before the first paint, and once in the module that owns the preference. The script
 * cannot import the module and still block paint, so the literal is duplicated and this is
 * the check that keeps the two honest. If they drift, the page loads in the wrong theme
 * and then corrects itself, which is a flash nobody files a bug about.
 */
async function checkThemeKey(failures) {
  const html = await readFile(INDEX_HTML, "utf8");
  const module = await readFile(THEME_MODULE, "utf8");
  const declared = /export const THEME_STORAGE_KEY = "([^"]+)";/.exec(module);
  if (!declared) {
    failures.push({
      where: relativePath(THEME_MODULE),
      rule: "theme key is declared once",
      why: "No THEME_STORAGE_KEY export of the expected shape, so nothing can be compared.",
      line: "",
    });
    return;
  }
  if (!html.includes(`"${declared[1]}"`)) {
    failures.push({
      where: "index.html",
      rule: "theme key matches the pre-paint script",
      why:
        `The module stores the theme under "${declared[1]}" and index.html does not read ` +
        "that key. The stored preference is ignored until React starts, which is the " +
        "flash of the wrong theme the blocking script exists to prevent.",
      line: "",
    });
  }
}

const failures = [];
await checkRules(failures);
await checkThemeKey(failures);

if (failures.length === 0) {
  console.log("check-boundaries: clean");
  process.exit(0);
}

for (const failure of failures) {
  console.error(`\n${failure.where}  [${failure.rule}]`);
  if (failure.line) {
    console.error(`  ${failure.line}`);
  }
  console.error(`  ${failure.why}`);
}
console.error(
  `\ncheck-boundaries: ${failures.length} problem(s). If one of them is genuinely ` +
    `correct, end the line with "${OPT_OUT} <reason>" and say why.`,
);
process.exit(1);
