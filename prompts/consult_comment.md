New comment(s) on the consultation issue {{repo}}#{{number}}:

{{comments}}

Answer on the issue (the human reads only GitHub), keeping the marker line:

  gh issue comment {{number}} --repo {{repo}} --body-file - <<'EOF'
  <your answer>

  <!-- autonomous:agent -->
  EOF

If they say the issue is ready or ask for the write-up, rewrite the issue description as your brief says
(`gh issue edit {{number}} --repo {{repo}} --body-file -`) and say so in a short comment.
Change no files in the repo.
