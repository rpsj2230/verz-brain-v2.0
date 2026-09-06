/**
 * The three-position theme control.
 *
 * A radio group rather than a toggle button, because there are three states and a button
 * that cycles through three states cannot show you the one you are in without also
 * pretending to be the one you would get next. A group of radios reads correctly to a
 * screen reader with no ARIA attributes of its own: the fieldset names it, each input says
 * which is chosen, and the arrow keys move between them because that is what radios do.
 *
 * `useSyncExternalStore` rather than `useState`, so that the preference has one home. Two
 * copies of it in two components is the version where the header and a settings page
 * disagree about which theme is selected.
 */

import { useSyncExternalStore } from "react";
import { getTheme, setTheme, subscribeToTheme, type Theme } from "./theme";

const OPTIONS: readonly { value: Theme; label: string }[] = [
  { value: "system", label: "System" },
  { value: "light", label: "Light" },
  { value: "dark", label: "Dark" },
];

// The return type is inferred rather than annotated. `JSX.Element` moved between the
// global namespace and `React.JSX` across type-package versions, and an annotation that
// resolves differently per version is a compile error nobody chose.
export function ThemeControl() {
  const theme = useSyncExternalStore(subscribeToTheme, getTheme, getTheme);

  return (
    <fieldset className="theme-control">
      <legend className="visually-hidden">Theme</legend>
      {OPTIONS.map((option) => (
        <label key={option.value} className="theme-control__option">
          <input
            type="radio"
            name="theme"
            value={option.value}
            checked={theme === option.value}
            onChange={() => {
              setTheme(option.value);
            }}
          />
          <span>{option.label}</span>
        </label>
      ))}
    </fieldset>
  );
}
