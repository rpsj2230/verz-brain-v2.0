/**
 * A small CSS reader, so that the theme tests assert on rules rather than on text.
 *
 * **Comments are stripped first, and that is the entire reason this file exists rather
 * than a substring search.** `src/theme/tokens.css` explains the cascade in a comment
 * block that quotes `:root:not([data-theme="light"])` and `:root[data-theme="dark"]`
 * verbatim, and `src/styles/app.css` names `.lock--out-of-scope` in prose in order to
 * forbid it. A test that searched the file for either string would pass with the real rule
 * deleted and would fail on the day somebody reworded a comment. That is the failure this
 * repository has already had twice, recorded in the house style notes: a test satisfied by
 * its own explanation.
 *
 * The parser understands one level of at-rule nesting, which is all the console's
 * stylesheets contain. Anything more elaborate should make this fall over loudly rather
 * than silently return fewer rules, so an unbalanced brace throws.
 */

export interface CssRule {
  /** The selector text, whitespace collapsed. */
  readonly selector: string;
  /** The at-rule this sits inside, or the empty string at the top level. */
  readonly atRule: string;
  /** Declarations in source order, property to value. */
  readonly declarations: Readonly<Record<string, string>>;
}

export function stripComments(css: string): string {
  return css.replace(/\/\*[\s\S]*?\*\//g, "");
}

function closingBrace(css: string, open: number): number {
  let depth = 0;
  for (let i = open; i < css.length; i += 1) {
    if (css[i] === "{") {
      depth += 1;
    } else if (css[i] === "}") {
      depth -= 1;
      if (depth === 0) {
        return i;
      }
    }
  }
  throw new Error("Unbalanced braces in stylesheet; the parser cannot be trusted here.");
}

function parseDeclarations(body: string): Record<string, string> {
  const declarations: Record<string, string> = {};
  for (const chunk of body.split(";")) {
    const at = chunk.indexOf(":");
    if (at === -1) {
      continue;
    }
    const property = chunk.slice(0, at).trim();
    const value = chunk.slice(at + 1).trim().replace(/\s+/g, " ");
    if (property) {
      declarations[property] = value;
    }
  }
  return declarations;
}

function collect(css: string, from: number, to: number, atRule: string, into: CssRule[]): void {
  let index = from;
  while (index < to) {
    const open = css.indexOf("{", index);
    if (open === -1 || open >= to) {
      return;
    }
    const prelude = css.slice(index, open).trim().replace(/\s+/g, " ");
    const close = closingBrace(css, open);
    if (prelude.startsWith("@")) {
      collect(css, open + 1, close, prelude, into);
    } else {
      into.push({
        selector: prelude,
        atRule,
        declarations: parseDeclarations(css.slice(open + 1, close)),
      });
    }
    index = close + 1;
  }
}

/** Every style rule in a stylesheet, with the at-rule it sits inside. */
export function parseCss(source: string): CssRule[] {
  const css = stripComments(source);
  const rules: CssRule[] = [];
  collect(css, 0, css.length, "", rules);
  return rules;
}

/** The custom properties a rule declares, ignoring ordinary properties. */
export function customProperties(rule: CssRule): Record<string, string> {
  return Object.fromEntries(
    Object.entries(rule.declarations).filter(([property]) => property.startsWith("--")),
  );
}
