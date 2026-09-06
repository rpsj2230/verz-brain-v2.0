/**
 * Reading the console's own source as a syntax tree, so that a rule about code can be
 * asserted on the code rather than on the characters in the file.
 *
 * **This exists for the same reason `support/css.ts` does, one level up.** That module
 * strips comments before reading rules, because both stylesheets name the selector they
 * exist to forbid in order to forbid it, and a substring search would be satisfied by the
 * explanation with the real rule deleted. Every component in `src/ui` now does the same
 * thing in prose: `Badge.tsx` writes out `tone={ok ? "positive" : "critical"}` as the shape
 * to refuse, and `Status.tsx` writes out `denied: "critical"`. A test that searched for
 * either string would find it in the docstring of the file that forbids it and pass for
 * ever, including after somebody wrote exactly that line in the code below.
 *
 * TypeScript's own parser is used rather than a regular expression because it is already a
 * pinned dependency of this project, it is the thing that decides what these files mean,
 * and a hand-rolled parser that disagreed with it would fail in the direction that reads
 * as a pass.
 *
 * Every function here throws when it finds nothing. A helper that returned an empty array
 * for a component that has been renamed would make every assertion built on it vacuous,
 * which is the failure this whole directory is arranged to avoid.
 */

import { existsSync, readdirSync, readFileSync } from "node:fs";
import { dirname, join, relative, resolve } from "node:path";
import ts from "typescript";
import { CONSOLE_ROOT } from "./repo";

/** Anything with a colour in it, in the spelling `tests/theme.test.ts` uses for CSS. */
const COLOUR = /#[0-9a-fA-F]{3,8}\b|\brgba?\(|\bhsla?\(|\bcolor-mix\(/;

/** Parse one file under `console/`. Path separated with forward slashes. */
export function parseConsoleSource(relativePath: string): ts.SourceFile {
  const full = join(CONSOLE_ROOT, ...relativePath.split("/"));
  return ts.createSourceFile(
    relativePath,
    readFileSync(full, "utf8"),
    ts.ScriptTarget.ES2022,
    true,
    relativePath.endsWith(".tsx") ? ts.ScriptKind.TSX : ts.ScriptKind.TS,
  );
}

/**
 * Every hand-written TypeScript file under a directory in `console/`, as forward-slashed
 * relative paths. `src/api/generated` is skipped for the reason `check-boundaries.mjs`
 * skips it: it is rewritten by a generator and is not anybody's code.
 */
export function consoleSourcePaths(directory: string): string[] {
  const root = join(CONSOLE_ROOT, ...directory.split("/"));
  const found: string[] = [];
  const walk = (current: string): void => {
    for (const entry of readdirSync(current, { withFileTypes: true })) {
      const full = join(current, entry.name);
      if (entry.isDirectory()) {
        if (entry.name !== "generated") {
          walk(full);
        }
      } else if (entry.name.endsWith(".ts") || entry.name.endsWith(".tsx")) {
        found.push(relative(CONSOLE_ROOT, full).split("\\").join("/"));
      }
    }
  };
  walk(root);
  if (found.length === 0) {
    throw new Error(`No TypeScript sources under ${directory}; the path has moved.`);
  }
  return found;
}

function everyNode(source: ts.SourceFile): ts.Node[] {
  const nodes: ts.Node[] = [];
  const visit = (node: ts.Node): void => {
    nodes.push(node);
    node.forEachChild(visit);
  };
  source.forEachChild(visit);
  return nodes;
}

function findFunction(source: ts.SourceFile, name: string): ts.FunctionDeclaration {
  for (const node of everyNode(source)) {
    if (ts.isFunctionDeclaration(node) && node.name?.text === name) {
      return node;
    }
  }
  throw new Error(
    `No function declaration named ${name} in ${source.fileName}. It has been renamed or ` +
      "turned into another kind of expression, so the property this checks is unchecked.",
  );
}

/**
 * The prop names a component accepts, read from its signature.
 *
 * A component that takes nothing returns an empty array, which is the shape `Lock` has and
 * the reason its own test can assert the property by reading the signature. Anything other
 * than "no parameters" or "one destructured object" throws rather than returning a guess:
 * `function Badge(props: BadgeProps)` would hide every prop behind one name, and silently
 * reporting `["props"]` would turn this check off while leaving it looking on.
 */
export function propNamesOf(source: ts.SourceFile, component: string): string[] {
  const declaration = findFunction(source, component);
  if (declaration.parameters.length === 0) {
    return [];
  }
  const [parameter, ...rest] = declaration.parameters;
  if (rest.length > 0 || !parameter || !ts.isObjectBindingPattern(parameter.name)) {
    throw new Error(
      `${component} in ${source.fileName} does not take exactly one destructured object. ` +
        "Its props cannot be read from the signature, so this check would pass for any " +
        "set of them.",
    );
  }
  return parameter.name.elements.map((element) =>
    ts.isIdentifier(element.propertyName ?? element.name)
      ? (element.propertyName ?? element.name).getText(source)
      : (element.propertyName ?? element.name).getText(source),
  );
}

/**
 * The member names of one interface, from any parsed file including a `.d.ts`.
 *
 * Used to read a dependency's own inventory of features rather than keeping a copy of it
 * here. A list of feature names written into a test is correct until the library adds one,
 * and the test that was supposed to notice is the thing that stopped noticing.
 */
export function interfaceMemberNames(source: ts.SourceFile, name: string): string[] {
  for (const node of everyNode(source)) {
    if (!ts.isInterfaceDeclaration(node) || node.name.text !== name) {
      continue;
    }
    const members = node.members
      .map((member) => member.name?.getText(source))
      .filter((member): member is string => member !== undefined);
    if (members.length === 0) {
      throw new Error(`Interface ${name} in ${source.fileName} has no members to read.`);
    }
    return members;
  }
  throw new Error(
    `No interface named ${name} in ${source.fileName}. The dependency has changed shape, ` +
      "so anything checked against it is no longer being checked.",
  );
}

/** The names imported from one module, across every import statement naming it. */
export function namedImportsFrom(source: ts.SourceFile, moduleName: string): string[] {
  const names: string[] = [];
  for (const statement of source.statements) {
    if (
      !ts.isImportDeclaration(statement) ||
      !ts.isStringLiteral(statement.moduleSpecifier) ||
      statement.moduleSpecifier.text !== moduleName
    ) {
      continue;
    }
    const bindings = statement.importClause?.namedBindings;
    if (bindings && ts.isNamedImports(bindings)) {
      for (const element of bindings.elements) {
        names.push(element.propertyName?.text ?? element.name.text);
      }
    }
    if (statement.importClause?.name) {
      names.push(statement.importClause.name.text);
    }
  }
  return names;
}

export interface JsxAttributeUse {
  /** The file it was written in, forward-slashed and relative to `console/`. */
  readonly file: string;
  /** The source text of the value, exactly as written. */
  readonly text: string;
  /** True when the value is a plain string, either `x="a"` or `x={"a"}`. */
  readonly isStringLiteral: boolean;
}

/** Every use of one JSX attribute in a file, with what was written on the right of it. */
export function jsxAttributeUses(
  source: ts.SourceFile,
  attribute: string,
): JsxAttributeUse[] {
  const uses: JsxAttributeUse[] = [];
  for (const node of everyNode(source)) {
    if (!ts.isJsxAttribute(node) || node.name.getText(source) !== attribute) {
      continue;
    }
    const initialiser = node.initializer;
    const inner =
      initialiser && ts.isJsxExpression(initialiser) ? initialiser.expression : initialiser;
    uses.push({
      file: source.fileName,
      text: initialiser ? initialiser.getText(source) : "",
      isStringLiteral: inner !== undefined && ts.isStringLiteral(inner),
    });
  }
  return uses;
}

/**
 * The properties of the object literal passed to a named call, as written.
 *
 * Used to read what `useTable({ ... })` was configured with. A call whose argument is a
 * variable rather than a literal throws, because the configuration would then be somewhere
 * this cannot see and the check would be reporting on an empty object.
 */
export function callOptionsOf(
  source: ts.SourceFile,
  callee: string,
): Record<string, string> {
  for (const node of everyNode(source)) {
    if (!ts.isCallExpression(node) || node.expression.getText(source) !== callee) {
      continue;
    }
    const [first] = node.arguments;
    if (!first || !ts.isObjectLiteralExpression(first)) {
      throw new Error(
        `${callee} in ${source.fileName} is not called with an object literal, so its ` +
          "options cannot be read here.",
      );
    }
    const options: Record<string, string> = {};
    for (const property of first.properties) {
      const name = property.name?.getText(source);
      if (name === undefined) {
        continue;
      }
      options[name] = ts.isPropertyAssignment(property)
        ? property.initializer.getText(source)
        : name;
    }
    return options;
  }
  throw new Error(`No call to ${callee} in ${source.fileName}; the check has gone stale.`);
}

/** The package a module specifier belongs to, scope included. */
export function packageOf(specifier: string): string {
  const parts = specifier.split("/");
  if (specifier.startsWith("@")) {
    return parts.slice(0, 2).join("/");
  }
  return parts[0] ?? specifier;
}

/**
 * Where a relative specifier resolves to, in the spellings a bundler tries.
 *
 * Throws when nothing matches, and that is the important half. A resolver that returned
 * null for a module it could not find would silently prune the graph at exactly the point a
 * refactor moved a file, and the check built on it would report a smaller graph and pass.
 */
function resolveRelative(fromFile: string, specifier: string): string {
  const base = resolve(CONSOLE_ROOT, dirname(fromFile), specifier);
  const candidates = [base, `${base}.ts`, `${base}.tsx`, join(base, "index.ts"), join(base, "index.tsx")];
  for (const candidate of candidates) {
    if (existsSync(candidate) && !isDirectory(candidate)) {
      return relative(CONSOLE_ROOT, candidate).split("\\").join("/");
    }
  }
  throw new Error(
    `${fromFile} imports ${specifier}, which resolves to nothing on disk. The import graph ` +
      "cannot be walked past this point, so anything checked against it is incomplete.",
  );
}

/** Whether a path is a directory. `existsSync` alone would accept the folder itself. */
function isDirectory(path: string): boolean {
  try {
    readdirSync(path);
    return true;
  } catch {
    return false;
  }
}

/** What one module reaches without a dynamic import: other files, and packages. */
export interface ImportGraph {
  /** Every hand-written module reachable by static import, forward-slashed. */
  readonly files: readonly string[];
  /** Every bare specifier reached, as its package name. */
  readonly packages: readonly string[];
}

/**
 * What an entry module pulls in through static imports only.
 *
 * **`import()` expressions are deliberately not followed**, because they are the split
 * points: a module reached only through one lands in its own chunk and is not in the entry.
 * This is the only way to state "that library is not in the first response" as a property
 * of the source rather than as a number in a build log that nobody reads twice.
 *
 * Type-only imports are skipped, both the `import type` form and individual `type` members
 * inside a named import list. `verbatimModuleSyntax` is on in this project, so a type import
 * is written as one and is erased: counting it would mean the graph reported a runtime
 * dependency on a module that contributes no runtime code at all, which is exactly what
 * `src/api/schema.ts` does with the generated file.
 */
export function staticImportGraph(entry: string): ImportGraph {
  const files: string[] = [];
  const packages = new Set<string>();
  const seen = new Set<string>();

  const visit = (path: string): void => {
    if (seen.has(path)) {
      return;
    }
    seen.add(path);
    files.push(path);
    if (!path.endsWith(".ts") && !path.endsWith(".tsx")) {
      // A stylesheet or an asset. It is part of the graph and has no imports to read.
      return;
    }
    const source = parseConsoleSource(path);
    for (const statement of source.statements) {
      let specifier: string | undefined;
      if (ts.isImportDeclaration(statement) && !statement.importClause?.isTypeOnly) {
        const bindings = statement.importClause?.namedBindings;
        const everyMemberIsAType =
          bindings !== undefined &&
          ts.isNamedImports(bindings) &&
          bindings.elements.length > 0 &&
          bindings.elements.every((element) => element.isTypeOnly);
        if (!everyMemberIsAType && ts.isStringLiteral(statement.moduleSpecifier)) {
          specifier = statement.moduleSpecifier.text;
        }
      } else if (
        ts.isExportDeclaration(statement) &&
        !statement.isTypeOnly &&
        statement.moduleSpecifier &&
        ts.isStringLiteral(statement.moduleSpecifier)
      ) {
        specifier = statement.moduleSpecifier.text;
      }
      if (specifier === undefined) {
        continue;
      }
      if (specifier.startsWith(".")) {
        visit(resolveRelative(path, specifier));
      } else {
        packages.add(packageOf(specifier));
      }
    }
  };

  visit(entry);
  if (files.length < 2) {
    throw new Error(
      `${entry} reaches nothing. Either the entry moved or the walk stopped immediately, ` +
        "and a graph of one file satisfies every check written against it.",
    );
  }
  return { files, packages: [...packages] };
}

/**
 * Colours written into code rather than taken from a token.
 *
 * Only string contents are read, never comments, so a docstring that explains why
 * `#1a56db` may not appear in a component does not itself count as one appearing.
 */
export function colourLiteralsIn(source: ts.SourceFile): string[] {
  const found: string[] = [];
  for (const node of everyNode(source)) {
    if (ts.isStringLiteral(node) || ts.isNoSubstitutionTemplateLiteral(node)) {
      if (COLOUR.test(node.text)) {
        found.push(`${source.fileName}: ${node.text}`);
      }
    } else if (ts.isTemplateExpression(node)) {
      const parts = [node.head.text, ...node.templateSpans.map((span) => span.literal.text)];
      for (const part of parts) {
        if (COLOUR.test(part)) {
          found.push(`${source.fileName}: ${part}`);
        }
      }
    }
  }
  return found;
}
