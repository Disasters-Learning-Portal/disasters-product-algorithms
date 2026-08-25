#!/usr/bin/env bash
#
# The roster of people a release alert must reach, and the diff that says who
# has acknowledged one. SOURCED, not executed:
#
#   source .github/scripts/release_roster.sh
#   roster_handles                                   # one handle per line
#   roster_split reacted.txt acked.txt missing.txt   # split roster by reactions
#
# Two consumers, so the parse + diff live in ONE place:
#   • .github/scripts/release_alert_body.sh — builds the @mention line
#   • .github/workflows/release-ack.yaml    — the ack tally
#
# Split out for the same reason as dps/blackmarble/naming.sh and dps/_validate.sh:
# the part two callers share sits in a sourceable file so it can be tested
# without running either caller. Pinned by tests/integration/test_release_ack.py.

# ${BASH_SOURCE[0]}, NOT $0. This file is sourced, so $0 is the *caller's* name
# and dirname "$0" would resolve against the caller's location — which happens
# to work for release_alert_body.sh (same directory) and silently would not for
# anything sourcing it from the repo root.
#
# BASH_SOURCE is a bash-ism: under zsh (this team's interactive shell) it is
# UNSET, and the failure is quiet rather than loud — dirname "" is ".", so the
# roster path would resolve against the caller's cwd instead of this repo and
# could pick up an unrelated file. Fail instead.
if [ -z "${BASH_SOURCE[0]:-}" ]; then
  echo "error: release_roster.sh must be sourced from bash (BASH_SOURCE is unset — zsh/sh?)" >&2
  return 1 2>/dev/null || exit 1
fi

_ROSTER_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Overridable so tests can point at a fixture roster.
ROSTER_FILE="${ROSTER_FILE:-$_ROSTER_SCRIPT_DIR/release-ack-roster.txt}"

# Print one bare GitHub handle per line. Comments and blank lines stripped.
# Fails loudly on a missing or empty roster — an empty roster would otherwise
# render an alert that mentions nobody and a tally that reads "0/0, all done".
roster_handles() {
  if [ ! -f "$ROSTER_FILE" ]; then
    echo "error: roster file not found: $ROSTER_FILE" >&2
    return 1
  fi

  local handles
  handles="$(sed -e 's/#.*//' -e 's/[[:space:]]//g' "$ROSTER_FILE" | grep -v '^$' || true)"

  if [ -z "$handles" ]; then
    echo "error: roster file lists no handles: $ROSTER_FILE" >&2
    return 1
  fi

  printf '%s\n' "$handles"
}

# roster_split <reacted_file> <acked_out> <missing_out>
#
# Split the roster into "has reacted" and "has not", where <reacted_file> is one
# login per line as returned by the reactions API. Also writes <acked_out>.extra
# with any reactor who is NOT on the roster — the cheap early warning that the
# roster has gone stale.
#
# grep -f, not comm: comm compares byte-wise, so it cannot fold case, and GitHub
# handles ARE case-insensitive while this roster is hand-typed. -i folds, -x
# anchors to the whole line (so `jr` can't match `jrbell1`), -F kills regex
# metacharacters (a handle may contain `-`).
#
# An EMPTY <reacted_file> is the normal starting state, and it is the one case
# worth being explicit about: POSIX says an empty -f pattern file matches
# nothing, so acked comes out empty and -v puts the WHOLE roster in missing.
# Verified against BSD grep 2.6 and ugrep 7.8. (A two-file awk `NR==FNR` idiom
# gets this exact case backwards — with an empty first file the roster itself
# populates the "seen" array and everyone reads as acknowledged.)
roster_split() {
  local reacted="$1" acked="$2" missing="$3"

  if [ ! -f "$reacted" ]; then
    echo "error: reactions file not found: $reacted" >&2
    return 1
  fi

  local roster
  roster="$(mktemp)"
  roster_handles > "$roster" || { rm -f "$roster"; return 1; }

  grep -ixFf "$reacted" "$roster" > "$acked"   || true
  grep -ivxFf "$roster" "$reacted" > "$acked.extra" || true
  grep -ivxFf "$reacted" "$roster" > "$missing" || true

  rm -f "$roster"
}

# roster_restrict <handle> <in_file>
#
# Print only <handle> from <in_file>, matched case-insensitively and reported
# with <in_file>'s casing. Used by release-ack.yaml's `only` input to aim the
# nudge at one person — testing whether a bot @mention delivers at all, without
# pinging the whole team for a mechanism that may not work.
#
# Restricts WHO IS MENTIONED, never who is counted: the tally still reads N/M.
#
# Hard-fails if <handle> isn't in <in_file>, because the alternative is a run
# that mentions nobody, goes green, and reads as "mentions don't deliver" when
# it actually just had a typo in the handle.
roster_restrict() {
  local only="$1" in_file="$2"

  if [ -z "$only" ]; then
    echo "error: roster_restrict needs a handle" >&2
    return 1
  fi

  local hit
  hit="$(grep -ixF -- "$only" "$in_file" || true)"

  if [ -z "$hit" ]; then
    echo "error: '$only' is not in $(basename "$in_file") — nothing to mention." >&2
    echo "       Candidates: $(tr '\n' ' ' < "$in_file")" >&2
    return 1
  fi

  printf '%s\n' "$hit"
}
