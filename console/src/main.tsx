/**
 * The entry point: styles, theme, one root.
 *
 * The order matters in one place. `tokens.css` is imported before `app.css` because the
 * second uses the custom properties the first defines, and although the cascade does not
 * care about import order for custom property resolution, a reader does: the file that
 * defines the vocabulary comes before the file that speaks it.
 *
 * `initialiseTheme` runs before the first render, and it is deliberately a second
 * application of the same value the blocking script in `index.html` already applied. That
 * script exists to avoid a flash of the wrong theme; this call exists so that the module
 * that owns the preference is the one holding it after startup, rather than the two ending
 * up with different ideas of what is selected.
 *
 * `StrictMode` is on, which double-invokes effects in development. Both places where that
 * would have caused a real problem, starting a sign-in and redeeming a code, are guarded
 * in `src/auth/session.ts` and say so.
 */

import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import "./theme/tokens.css";
import "./styles/app.css";
import { initialiseTheme } from "./theme/theme";

initialiseTheme();

const container = document.getElementById("root");
if (!container) {
  // index.html and this file are the two halves of one contract. If the element is gone,
  // failing with a sentence beats React failing with a null reference from inside a
  // minified bundle.
  throw new Error('index.html has no element with id "root", so nothing can be rendered.');
}

createRoot(container).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
