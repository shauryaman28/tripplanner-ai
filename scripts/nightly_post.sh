#!/usr/bin/env bash
# Post the night's summary as a comment on the repository's nightly issue (Phase 28).
#
#     GH_TOKEN=… GH_REPO=owner/name bash scripts/nightly_post.sh summary.md
#
# The issue is the open one labelled "nightly". The first night there is none: the label and the issue
# are made then, and every night after that adds one comment to the same issue — a history that can be
# read from the top, and muted by whoever does not want a notification a day.
#
# `gh` is GitHub's own CLI, which every Actions runner has. GH_BIN names another program to run in its
# place: the tests hand in one that writes down what it was asked.
set -euo pipefail

# (No apostrophe in the message below: inside ${…} an old bash — macOS still ships 3.2 — takes one for
# the start of a quoted string, and the whole script then fails to parse.)
summary="${1:?usage: nightly_post.sh SUMMARY_FILE}"
gh="${GH_BIN:-gh}"
[ -s "$summary" ] || { echo "nightly_post: $summary is missing or empty — nothing to post" >&2; exit 1; }

"$gh" label create nightly --description "Results of the nightly test run" --color 0E8A16 >/dev/null 2>&1 || true

number="$("$gh" issue list --label nightly --state open --limit 1 --json number --jq '.[0].number // empty')"
if [ -z "$number" ]; then
  url="$("$gh" issue create --title "Nightly test results" --label nightly --body-file .github/nightly-issue.md)"
  number="${url##*/}"
  echo "made the nightly issue: #$number"
fi

"$gh" issue comment "$number" --body-file "$summary" >/dev/null
echo "posted the night's summary to issue #$number"
