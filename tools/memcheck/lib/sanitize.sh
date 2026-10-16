#!/usr/bin/env bash
# sanitize.sh — shared sanitize_field(), used by memcheck so any other tool that writes memory text can reuse the same
# stripping instead of duplicating it. Source only — not directly executable.

# sanitize text before interpolation into a MEMORY.md index line or a memory file body (both
# auto-loaded session context — unsanitized text is a persistent injection channel). Translates
# control chars (incl. newline/CR/tab) to a space rather than deleting them, so multi-line input
# (e.g. memory-promote's multi-line `body:` field) keeps word boundaries instead of merging
# across line breaks into one word — the subsequent whitespace-collapse step below then folds
# any resulting runs of spaces down to one. Strips zero-width/bidi-override Unicode chars
# (U+200B-U+200F, U+202A-U+202E, U+2028-U+2029, U+FEFF), strips markdown-link-breakout chars
# ] [ ( ) `, collapses whitespace, caps length (warning to stderr if the cap actually truncates
# — stdout contract is unchanged, callers still just capture the sanitized text).
# text maxlen [label] -> sanitized text on stdout.
sanitize_field() {
  local s="$1" maxlen="$2" label="${3:-field}" ascii_stripped rc
  s="$(printf '%s' "$s" | tr '\000-\037\177' ' ')"
  ascii_stripped="$s"
  s="$(printf '%s' "$s" | perl -CSD -pe 's/[\x{200B}-\x{200F}\x{202A}-\x{202E}\x{2028}\x{2029}\x{FEFF}]//g')"
  rc=$?
  if (( rc != 0 )); then
    echo "WARNING: sanitize_field: perl bidi-strip failed (rc=$rc), falling back to ASCII-stripped value" >&2
    s="$ascii_stripped"
  fi
  s="${s//]/}"; s="${s//[/}"; s="${s//(/}"; s="${s//)/}"; s="${s//\`/}"
  s="$(printf '%s' "$s" | tr -s '[:space:]' ' ')"
  s="$(printf '%s' "$s" | sed -E 's/^ +//; s/ +$//')"
  # character-safe truncation (perl -CSD decodes/encodes UTF-8 explicitly) — a byte-wise
  # ${s:0:maxlen} can split a multi-byte codepoint under a non-UTF-8 locale (e.g. systemd's C).
  local charlen
  charlen="$(printf '%s' "$s" | perl -CSD -ne 'print length($_)')"
  if (( charlen > maxlen )); then
    echo "WARNING: $label truncated ${charlen}→${maxlen} chars" >&2
  fi
  printf '%s' "$s" | perl -CSD -ne "print substr(\$_, 0, $maxlen)"
}
