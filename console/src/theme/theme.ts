/**
 * The viewer's theme preference: read it, store it, apply it.
 *
 * Three states rather than two. "System" is a real choice and it is the default, so the
 * control has three positions and none of them lies. A two-position toggle has to pretend
 * the machine has no opinion, and the person who set their laptop to switch at sunset
 * notices immediately.
 *
 * **Stored per browser, and deliberately not per account.** A theme is a property of the
 * screen somebody is looking at, not of who they are: the same person wants dark on the
 * laptop at night and light on the projector. Putting it on the server would also mean a
 * request before the first paint, which is the flash this is arranged to avoid. It is the
 * one thing this console keeps in the browser's local store, and the boundary check allows
 * it here and nowhere else.
 *
 * **The key is duplicated in index.html**, where a blocking script reads it before the
 * first paint. That script cannot import this module and still run early enough to matter,
 * so the literal appears twice. `scripts/check-boundaries.mjs` asserts they match, because
 * the failure is a flash of the wrong theme on load: irritating, invisible in review, and
 * exactly the sort of thing nobody files.
 */

/** Duplicated in index.html. Change both, or the pre-paint script reads the wrong key. */
export const THEME_STORAGE_KEY = "brain.console.theme";

export type Theme = "system" | "light" | "dark";

const THEMES: readonly Theme[] = ["system", "light", "dark"];

function isTheme(value: unknown): value is Theme {
  return typeof value === "string" && (THEMES as readonly string[]).includes(value);
}

function store(): Storage | null {
  // Access itself throws in a private window with site data blocked. A missing preference
  // is not an error: it means "system", which is what the absence of the attribute
  // already means.
  try {
    return globalThis.localStorage;
  } catch {
    return null;
  }
}

let current: Theme = readStoredTheme();
const listeners = new Set<() => void>();

function readStoredTheme(): Theme {
  const stored = store()?.getItem(THEME_STORAGE_KEY);
  return isTheme(stored) ? stored : "system";
}

/**
 * Put the choice on the root element, or take it off.
 *
 * Removing the attribute is what "system" means: with nothing set, only the
 * `prefers-color-scheme` block in `tokens.css` applies, so the page follows the machine
 * and keeps following it when the machine changes at sunset. Setting `data-theme="system"`
 * instead would match neither of the theme selectors and leave the page in light mode for
 * ever, which is the same bug with a friendlier attribute value.
 */
function apply(theme: Theme): void {
  const root = globalThis.document.documentElement;
  if (theme === "system") {
    root.removeAttribute("data-theme");
  } else {
    root.setAttribute("data-theme", theme);
  }
}

export function getTheme(): Theme {
  return current;
}

export function setTheme(theme: Theme): void {
  current = theme;
  apply(theme);
  try {
    store()?.setItem(THEME_STORAGE_KEY, theme);
  } catch {
    // A browser that refuses to store the preference still honours it for this page. The
    // choice not persisting is a worse experience, not a broken one.
  }
  for (const listener of listeners) {
    listener();
  }
}

export function subscribeToTheme(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

/**
 * Called once at startup. The blocking script in index.html has already applied a stored
 * light or dark choice to avoid the flash; this reasserts it from the same value so that
 * the two can never disagree, and costs nothing when they already agree.
 */
export function initialiseTheme(): void {
  apply(current);
}
