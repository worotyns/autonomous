#!/usr/bin/env bash
# Open a PR that adds the repo-kit to a repo. Existing files are never overwritten.
#   repo-kit/apply.sh owner/repo [owner/repo ...]
set -euo pipefail
KIT=$(cd "$(dirname "$0")" && pwd)
for REPO in "$@"; do
  tmp=$(mktemp -d)
  gh repo clone "$REPO" "$tmp" -- --depth 1 -q
  cd "$tmp"; git switch -q -c chore/agent-kit
  added=()
  while IFS= read -r f; do
    rel=${f#"$KIT"/}; [ "$rel" = apply.sh ] && continue
    [ -e "$rel" ] && { echo "$REPO: keep existing $rel"; continue; }
    mkdir -p "$(dirname "$rel")"; cp "$f" "$rel"; added+=("$rel")
  done < <(find "$KIT" -type f)
  if [ ${#added[@]} -eq 0 ]; then echo "$REPO: nothing to add"; cd - >/dev/null; rm -rf "$tmp"; continue; fi
  git add "${added[@]}"; git commit -q -m "chore: add agent kit (templates, AGENTS.md, check + release workflows)"
  git push -q -u origin chore/agent-kit
  gh pr create --repo "$REPO" --fill --body "Adds: $(printf '`%s` ' "${added[@]}")

Fill in \`Makefile\` and AGENTS.md (commands, repo map) and \`.herdr-projects.json\` (check command) before merging.
If the repo already had an AGENTS.md or Makefile, merge these sections into it: apply.sh never overwrites them."
  cd - >/dev/null; rm -rf "$tmp"
done
