GitHub issue {{repo}}#{{number}}: {{title}}
{{url}}

{{body}}

---
How to work on this:
- Read AGENTS.md in the repo first and follow its Definition of Done.
- Read the issue's comments too (`gh issue view {{number}} --repo {{repo}} --comments`): decisions may be there.
- Every acceptance criterion in the issue needs a test, or a stated manual proof in the PR description.
- Run the repo's check command before you finish. Keep the diff small and inside the issue's scope.
- If the issue is ambiguous, do not guess: ask with `herdr-projects report --state blocked --kind question --msg "<question>"`.
  The human answers in the issue and the answer is sent to you here.
- The PR description must contain the issue's acceptance criteria as a checklist, ticked, with how you verified each.
