/** Attributes that keep browsers and password managers away from an input
 *  that holds an exchange credential (API key, secret, passphrase). Chrome
 *  ignores autocomplete="off" on fields it has classified as a login, so the
 *  value is a token it does not recognise; the data-* attributes are what
 *  1Password, LastPass and Bitwarden honour; autocorrect/autocapitalize/
 *  spellcheck stop mobile keyboards rewriting the value. Pair it with an id
 *  and name that contain no credential words (email, user, login, password,
 *  secret) so the browser's heuristics have nothing to match. */
export const NO_AUTOFILL = {
  autoComplete: "one-time-code",
  autoCorrect: "off",
  autoCapitalize: "off",
  spellCheck: false,
  "data-1p-ignore": "",
  "data-lpignore": "true",
  "data-bwignore": "",
  "data-form-type": "other",
} as const;
