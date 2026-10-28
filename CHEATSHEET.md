# autonomous: cheatsheet

**What it does:** you write an issue on GitHub and add a label; an agent on the server does the rest: code, tests, PR and review. You only answer questions and approve.

**Glossary:** *issue* = a task on GitHub, *label* = a tag that steers the agent (`start`, `consult`), *PR* = the agent's proposed code change, *agent* (pi) = the program writing code on the server.

## Handing over a task (every day)

1. **New issue** → template **Agent task**.
2. Fill in the **Acceptance criteria**: each line is one thing that can be checked.
   - ✅ `div(a, 0) raises ZeroDivisionError`
   - ❌ `it should work well`
3. Add the label **`start`**. Within ~1 min you'll see `agent:working`.
4. Wait for `agent:ready` and a link to the PR → review the PR.
5. Write **`/approve`** on the issue. Merge, tag and release happen by themselves.

**Not sure yet?** Use **`consult`** instead of `start`: the agent reads the code and suggests options in comments. Then write “write it up” and add **`start`**.

## Comments

| Where | What happens |
|---|---|
| on the **issue** | the agent gets your comment after ~90 s of quiet |
| on the **PR** | the agent fixes the code and pushes |
| `/approve` on the issue | merge (only when it's `agent:ready`) |
| `/stop` on the issue | stops the agent |

The agent listens only to you (the `GH_TOKEN` account) and to people in `allow_authors`.

## Labels: what they mean

| Label | Means | You |
|---|---|---|
| `agent:consulting` | a discussion, no code | reply in comments |
| `agent:working` | the agent is working | nothing, wait |
| `agent:needs-input` | the agent asks something | answer in a comment |
| `agent:ready` | PR ready, reviewed | review it → `/approve` |
| `agent:failed` | something broke | a comment with a hint, or `/stop` |
| `agent:done` | merged | 🎉 |

Release: label the **PR** `release:minor` / `release:major` / `release:none`; no label = patch.

## First setup (once)

```bash
git clone https://github.com/worotyns/autonomous && cd autonomous
cp config/autonomous.example.toml config/autonomous.toml   # put your repos in
cp config/env.example config/env                           # OPENROUTER_API_KEY, GH_TOKEN
./deploy.sh root@<server-ip>
# once on the server: sudo -iu agent; cd ~/repos/<owner>/<repo>; pi; ctrl+c
repo-kit/apply.sh <owner>/<repo>     # PR with templates; merge it
# dogfooding: [[repo]] name = "worotyns/autonomous" in autonomous.toml, then sudo autonomous update
```

## On the server (`ssh root@<server-ip>`)

| I want to | Type |
|---|---|
| check that everything works | `sudo autonomous doctor` |
| see the tasks | `sudo autonomous status` |
| watch an agent live | `sudo autonomous attach` (`ctrl+b a` board, `ctrl+b q` leave) |
| logs (e.g. `start` does nothing) | `sudo autonomous logs` |
| after a config change / a new repo | `sudo autonomous update` |
| something hangs | `sudo autonomous restart` |

New repo: add `[[repo]]` to `/etc/autonomous/autonomous.toml`, then `update`. Don't type in the agent's window unless you mean to take over.

## 3 rules

1. **Concrete acceptance criteria** give good results. Vague ones make the agent guess.
2. **No production secrets on the server**: the agent has a full shell.
3. **Nothing merges without `/approve`.** Always review the PR.
