#!/usr/bin/env bash
# End-to-end calculator scenarios against two private sandbox repos.
#
#   tests/scenario.sh work        # issue + start -> PR -> agent:ready -> /approve -> merged + release
#   tests/scenario.sh consult     # consult -> agent answers -> "write it up" -> start -> PR -> merged
#   tests/scenario.sh pr-comment  # ready PR -> comment on the PR -> new commit -> ready -> merged
#   tests/scenario.sh parallel    # two repos at once, both tasks run in parallel
#   tests/scenario.sh all         # reset, then work, consult, parallel, pr-comment
#   tests/scenario.sh reset       # close stale issues/PRs, delete branches, reset main to test-base
#
# The sandbox repos and their base tag (default `test-base`) are described in tests/README.md.
# Point it at other repos with REPO_A/REPO_B.
set -euo pipefail

REPO_A=${REPO_A:-worotyns/autonomous-sandbox}
REPO_B=${REPO_B:-worotyns/autonomous-sandbox-2}
BASE_TAG=${BASE_TAG:-test-base}
POLL=${POLL:-15}
READY_TIMEOUT=${READY_TIMEOUT:-2400}   # a TCG VM can be slow
MERGE_TIMEOUT=${MERGE_TIMEOUT:-900}
RELEASE_TIMEOUT=${RELEASE_TIMEOUT:-900}

log() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
die() { printf 'scenario: %s\n' "$*" >&2; exit 1; }
now() { date +%s; }
elapsed() { echo $(( $(now) - $1 )); }

labels() { gh issue view "$2" --repo "$1" --json labels --jq '.labels[].name' 2>/dev/null || true; }
has_label() { labels "$1" "$2" | grep -qx "$3"; }

wait_label() {
  local repo=$1 n=$2 want=$3 timeout=${4:-$READY_TIMEOUT} start; start=$(now)
  while :; do
    if has_label "$repo" "$n" "$want"; then
      log "$repo#$n is $want ($(elapsed "$start")s)"
      return 0
    fi
    # A fast task can pass through working/ready between two polls; do not miss it.
    if [ "$want" = agent:working ] && { has_label "$repo" "$n" agent:ready || has_label "$repo" "$n" agent:done; }; then
      log "$repo#$n already moved past $want ($(elapsed "$start")s)"
      return 0
    fi
    if [ "$want" = agent:ready ] && has_label "$repo" "$n" agent:done; then
      log "$repo#$n already moved past $want ($(elapsed "$start")s)"
      return 0
    fi
    if [ "$want" != agent:failed ] && has_label "$repo" "$n" agent:failed; then
      die "$repo#$n failed; see the issue: https://github.com/$repo/issues/$n"
    fi
    if [ "$want" != agent:needs-input ] && has_label "$repo" "$n" agent:needs-input; then
      die "$repo#$n needs input; answer in the issue: https://github.com/$repo/issues/$n"
    fi
    [ "$(elapsed "$start")" -lt "$timeout" ] || die "timeout waiting for $want on $repo#$n"
    sleep "$POLL"
  done
}

create_issue() {
  local repo=$1 title=$2 body=$3 url n
  url=$(gh issue create --repo "$repo" --title "$title" --body "$body")
  n=${url##*/}
  log "$repo#$n created: $url" >&2
  printf '%s\n' "$n"
}

add_label() { gh issue edit "$2" --repo "$1" --add-label "$3" >/dev/null; }

get_pr() {
  local repo=$1 n=$2 pr
  pr=$(gh api "repos/$repo/issues/$n/timeline?per_page=100" \
    --jq '.[] | select(.event == "cross-referenced" and .source.issue.pull_request != null) | .source.issue.number' \
    2>/dev/null | tail -1)
  if [ -z "$pr" ]; then
    pr=$(gh issue view "$n" --repo "$repo" --comments --json comments --jq '.comments[].body' \
      | grep -oE "https://github.com/$repo/pull/[0-9]+" | tail -1 | sed 's#.*/##')
  fi
  [ -n "$pr" ] || die "no PR found for $repo#$n"
  printf '%s\n' "$pr"
}

wait_agent_comment() {
  local repo=$1 n=$2 timeout=${3:-$READY_TIMEOUT} start; start=$(now)
  while :; do
    if gh issue view "$n" --repo "$repo" --comments --json comments --jq '.comments[].body' \
      | grep -q '<!-- autonomous:agent -->'; then
      log "$repo#$n has an agent answer ($(elapsed "$start")s)"
      return 0
    fi
    [ "$(elapsed "$start")" -lt "$timeout" ] || die "timeout waiting for the consult answer on $repo#$n"
    sleep "$POLL"
  done
}

wait_body() {
  local repo=$1 n=$2 needle=$3 timeout=${4:-$READY_TIMEOUT} start; start=$(now)
  while :; do
    if gh issue view "$n" --repo "$repo" --json body --jq '.body' | grep -qF "$needle"; then
      log "$repo#$n body contains: $needle ($(elapsed "$start")s)"
      return 0
    fi
    [ "$(elapsed "$start")" -lt "$timeout" ] || die "timeout waiting for '$needle' in $repo#$n"
    sleep "$POLL"
  done
}

wait_pr_change() {
  local repo=$1 pr=$2 before=$3 timeout=${4:-$READY_TIMEOUT} start sha; start=$(now)
  while :; do
    sha=$(gh pr view "$pr" --repo "$repo" --json headRefOid --jq '.headRefOid')
    if [ "$sha" != "$before" ]; then
      log "$repo PR #$pr new commit ${sha:0:8} ($(elapsed "$start")s)"
      return 0
    fi
    [ "$(elapsed "$start")" -lt "$timeout" ] || die "timeout waiting for a new commit on $repo PR #$pr"
    sleep "$POLL"
  done
}

latest_tag() { gh release list --repo "$1" --limit 1 --json tagName --jq '.[0].tagName // ""' 2>/dev/null || true; }

wait_release() {
  local repo=$1 before=$2 timeout=${3:-$RELEASE_TIMEOUT} start tag; start=$(now)
  while :; do
    tag=$(latest_tag "$repo")
    if [ -n "$tag" ] && [ "$tag" != "$before" ]; then
      log "$repo released $tag ($(elapsed "$start")s)"
      return 0
    fi
    [ "$(elapsed "$start")" -lt "$timeout" ] || die "timeout waiting for a release on $repo (latest: ${tag:-none})"
    sleep "$POLL"
  done
}

wait_closed() {
  local repo=$1 n=$2 timeout=${3:-$MERGE_TIMEOUT} start state; start=$(now)
  while :; do
    state=$(gh issue view "$n" --repo "$repo" --json state --jq '.state')
    [ "$state" = CLOSED ] && { log "$repo#$n is closed"; return 0; }
    [ "$(elapsed "$start")" -lt "$timeout" ] || die "timeout waiting for $repo#$n to close"
    sleep "$POLL"
  done
}

approve() {
  local repo=$1 n=$2 before
  before=$(latest_tag "$repo")
  gh issue comment "$n" --repo "$repo" --body "/approve" >/dev/null
  log "$repo#$n: /approve sent"
  wait_label "$repo" "$n" agent:done "$MERGE_TIMEOUT"
  wait_closed "$repo" "$n"
  wait_release "$repo" "$before"
}

verify_file() {
  local repo=$1 path=$2 needle=$3
  gh api "repos/$repo/contents/$path?ref=main" --jq '.content' | base64 -d | grep -qF "$needle" \
    || die "$repo: $path on main does not contain '$needle'"
  log "$repo: $path on main contains '$needle'"
}

reset_repos() {
  local repo sha
  for repo in "$REPO_A" "$REPO_B"; do
    sha=$(gh api "repos/$repo/git/ref/tags/$BASE_TAG" --jq '.object.sha' 2>/dev/null || true)
    [ -n "$sha" ] || die "$repo has no tag $BASE_TAG (see tests/README.md)"
    log "resetting $repo to $BASE_TAG ($sha)"
    gh issue list --repo "$repo" --state open --json number --jq '.[].number' 2>/dev/null | while read -r n; do
      gh issue close "$n" --repo "$repo" --comment "Reset for a test run." >/dev/null 2>&1 || true
    done
    gh pr list --repo "$repo" --state open --json number --jq '.[].number' 2>/dev/null | while read -r n; do
      gh pr close "$n" --repo "$repo" --delete-branch >/dev/null 2>&1 || true
    done
    gh api "repos/$repo/branches?per_page=100" --jq '.[].name' 2>/dev/null | while read -r b; do
      [ "$b" = main ] && continue
      gh api -X DELETE "repos/$repo/git/refs/heads/$b" >/dev/null 2>&1 || true
    done
    gh api -X PATCH "repos/$repo/git/refs/heads/main" -f sha="$sha" -F force=true >/dev/null
  done
  log "sandbox repos reset to $BASE_TAG"
}

work_issue_body='## Context
The calculator only supports `add` and `sub`. Add multiplication and division so the sandbox covers the basic operations.

## Acceptance criteria
- [ ] `mul(2, 3)` returns `6`
- [ ] `div(6, 3)` returns `2`
- [ ] `div(1, 0)` raises `ZeroDivisionError` with the message `cannot divide by zero`
- [ ] every behaviour has a unittest test and `make check` passes

## Out of scope
Do not change `add`, `sub`, the Makefile or the repo settings.

## How to test / hints
`make check`; start from `calc.py` and `test_calc.py`.'

consult_issue_body='## Context
Should the calculator keep a history of results, or stay a set of pure functions? If it keeps history,
we need to decide what is stored, how many entries, and whether the pure functions in `calc.py` stay untouched.

## Questions
1. Is a history useful for the sandbox, or does it add noise?
2. In memory or on disk?
3. How many entries?

## Out of scope
Do not write code yet: this is a consultation. Add `start` when the issue is ready to build.'

do_work() {
  local n pr
  n=$(create_issue "$REPO_A" "Add multiplication and division" "$work_issue_body")
  add_label "$REPO_A" "$n" start
  wait_label "$REPO_A" "$n" agent:working
  wait_label "$REPO_A" "$n" agent:ready
  pr=$(get_pr "$REPO_A" "$n")
  log "PR: https://github.com/$REPO_A/pull/$pr"
  approve "$REPO_A" "$n"
  verify_file "$REPO_A" calc.py "def mul"
  verify_file "$REPO_A" calc.py "def div"
}

do_consult() {
  local n pr
  n=$(create_issue "$REPO_A" "Should the calculator keep a history of results?" "$consult_issue_body")
  add_label "$REPO_A" "$n" consult
  wait_label "$REPO_A" "$n" agent:consulting
  wait_agent_comment "$REPO_A" "$n"
  gh issue comment "$n" --repo "$REPO_A" --body "Write it up: keep the last 10 results in memory, no disk persistence, and leave calc.py untouched. Use a HistoryCalculator wrapper with tests." >/dev/null
  wait_body "$REPO_A" "$n" "## Acceptance criteria"
  add_label "$REPO_A" "$n" start
  wait_label "$REPO_A" "$n" agent:working
  wait_label "$REPO_A" "$n" agent:ready
  pr=$(get_pr "$REPO_A" "$n")
  log "PR: https://github.com/$REPO_A/pull/$pr"
  approve "$REPO_A" "$n"
  verify_file "$REPO_A" history_calc.py "class HistoryCalculator"
}

do_parallel() {
  local na nb pa pb
  na=$(create_issue "$REPO_A" "Add a power function" '## Context
Add `power(a, b)` to the calculator.

## Acceptance criteria
- [ ] `power(2, 3)` returns `8`
- [ ] `power(2, 0)` returns `1`
- [ ] `power(2, -1)` returns `0.5`
- [ ] there is a unittest test and `make check` passes

## Out of scope
Nothing else.')
  nb=$(create_issue "$REPO_B" "Add a neg function" '## Context
Add `neg(a)` to the calculator.

## Acceptance criteria
- [ ] `neg(5)` returns `-5`
- [ ] `neg(-3)` returns `3`
- [ ] there is a unittest test and `make check` passes

## Out of scope
Nothing else.')
  add_label "$REPO_A" "$na" start
  add_label "$REPO_B" "$nb" start
  wait_label "$REPO_A" "$na" agent:working
  wait_label "$REPO_B" "$nb" agent:working
  log "both repos are working at once"
  wait_label "$REPO_A" "$na" agent:ready
  wait_label "$REPO_B" "$nb" agent:ready
  pa=$(get_pr "$REPO_A" "$na"); pb=$(get_pr "$REPO_B" "$nb")
  log "PRs: https://github.com/$REPO_A/pull/$pa https://github.com/$REPO_B/pull/$pb"
  approve "$REPO_A" "$na"
  approve "$REPO_B" "$nb"
  verify_file "$REPO_A" calc.py "def power"
  verify_file "$REPO_B" calc.py "def neg"
}

do_pr_comment() {
  local n pr before
  n=$(create_issue "$REPO_A" "Add a modulo function" '## Context
Add `mod(a, b)` to the calculator.

## Acceptance criteria
- [ ] `mod(7, 3)` returns `1`
- [ ] `mod(-7, 3)` returns `2`
- [ ] `mod(1, 0)` raises `ZeroDivisionError`
- [ ] there is a unittest test and `make check` passes

## Out of scope
Nothing else.')
  add_label "$REPO_A" "$n" start
  wait_label "$REPO_A" "$n" agent:working
  wait_label "$REPO_A" "$n" agent:ready
  pr=$(get_pr "$REPO_A" "$n")
  before=$(gh pr view "$pr" --repo "$REPO_A" --json headRefOid --jq '.headRefOid')
  log "PR: https://github.com/$REPO_A/pull/$pr (head ${before:0:8})"
  gh pr comment "$pr" --repo "$REPO_A" --body "Please add a one-line docstring to mod and a test for a negative dividend." >/dev/null
  wait_pr_change "$REPO_A" "$pr" "$before"
  wait_label "$REPO_A" "$n" agent:ready
  approve "$REPO_A" "$n"
  verify_file "$REPO_A" calc.py "def mod"
}

case "${1:-help}" in
  reset) reset_repos ;;
  work) reset_repos; do_work ;;
  consult) reset_repos; do_consult ;;
  parallel) reset_repos; do_parallel ;;
  pr-comment) reset_repos; do_pr_comment ;;
  all)
    reset_repos
    do_work
    do_consult
    do_parallel
    do_pr_comment
    log "all scenarios passed"
    ;;
  help|--help|-h)
    sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'
    ;;
  *) die "usage: $0 reset|work|consult|parallel|pr-comment|all" ;;
esac
