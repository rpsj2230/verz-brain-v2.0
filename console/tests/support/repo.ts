/**
 * Reading the files this console has to agree with, so that agreement can be asserted
 * against something outside the console.
 *
 * **This exists because of a specific failure mode.** A test that asserts
 * `LOCK_TEXT === LOCK_TEXT`, importing the constant from the module it is checking,
 * compares a value with itself: change it and both sides move together, and the test is
 * green for every value the constant could hold. The repository has been bitten by exactly
 * that four times in one day. Every constant in this console that is a copy of a fact held
 * somewhere else is therefore checked against that other place: the Python source, the
 * Keycloak realm export, or the stylesheet.
 *
 * Files are read with `node:fs` rather than imported with Vite's `?raw`, for two reasons.
 * Vitest replaces the content of a CSS import with an empty string unless CSS processing
 * is switched on, which turns a real check into a vacuous one that still passes. And the
 * Python sources are outside the console directory, where the bundler's idea of the
 * project root is a thing that could change under us.
 */

import { readFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));

/** `console/`, the root of the application under test. */
export const CONSOLE_ROOT = resolve(HERE, "..", "..");

/** The repository root, one level above the console. */
export const REPO_ROOT = resolve(CONSOLE_ROOT, "..");

/** Read a file inside `console/`. Path separated with forward slashes. */
export function readConsoleFile(relativePath: string): string {
  return readFileSync(join(CONSOLE_ROOT, ...relativePath.split("/")), "utf8");
}

/** Read a file elsewhere in the repository, such as a Python module or the realm export. */
export function readRepoFile(relativePath: string): string {
  return readFileSync(join(REPO_ROOT, ...relativePath.split("/")), "utf8");
}

/**
 * Pull one value out of a source file, and fail loudly when the shape has moved.
 *
 * A missing match returning `null` would make every caller's assertion vacuous, which is
 * the same bug as the one this whole module exists to avoid: a check that quietly stops
 * checking. The pattern is anchored on the whole statement rather than on the value, so
 * renaming the thing being read fails here instead of silently matching something else.
 */
export function extractOne(source: string, pattern: RegExp, what: string): string {
  const found = pattern.exec(source);
  if (!found || found[1] === undefined) {
    throw new Error(
      `Could not find ${what} using ${String(pattern)}. The source has moved, so the ` +
        "check that depends on it is no longer checking anything. Fix the pattern.",
    );
  }
  return found[1];
}
