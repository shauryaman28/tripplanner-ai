#!/usr/bin/env bash
# GitHub's CLI, as far as scripts/nightly_post.sh uses it: it writes down what it was asked, and answers
# from a directory instead of from GitHub.
#
#   FAKE_GH_DIR   where:  calls.log         every call, one a line
#                         label             there once the "nightly" label has been made
#                         issue             the open nightly issue's number, once there is one
#                         issue-body.md     what the issue was opened with
#                         comment-<n>.md    the comments on issue <n>, each after the last
set -euo pipefail
dir="${FAKE_GH_DIR:?FAKE_GH_DIR is not set}"
mkdir -p "$dir"
printf '%s\n' "$*" >> "$dir/calls.log"

value_of() { # the argument that follows a flag
  local flag="$1"; shift
  while [ "$#" -gt 0 ]; do
    if [ "$1" = "$flag" ]; then echo "$2"; return; fi
    shift
  done
}

case "${1:-} ${2:-}" in
  "label create")
    if [ -f "$dir/label" ]; then echo "label with name \"nightly\" already exists" >&2; exit 1; fi
    touch "$dir/label" ;;
  "issue list")
    if [ -f "$dir/issue" ]; then cat "$dir/issue"; fi ;;
  "issue create")
    cp "$(value_of --body-file "$@")" "$dir/issue-body.md"
    echo 7 > "$dir/issue"
    echo "https://github.com/owner/repo/issues/7" ;;
  "issue comment")
    { cat "$(value_of --body-file "$@")"; echo "-----"; } >> "$dir/comment-$3.md" ;;
  *)
    echo "fake gh: not something nightly_post.sh asks for: $*" >&2
    exit 2 ;;
esac
