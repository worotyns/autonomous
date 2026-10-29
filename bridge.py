#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""gh-bridge: GitHub issues <-> herdr-projects board, for many repos.

  label `consult` on an issue    -> herdr-projects add ... --chat     (talks about the code, changes nothing)
  label `start` on an issue      -> herdr-projects add ... --merge.closes N --delegate  (ends a running consult)
  comment on that issue          -> herdr-projects tell <task> "..."   (/approve, /stop handled as commands)
  board state (notify + polling) -> agent:* labels and status comments on the issue

PR comments, review, CI, merge and cleanup are herdr-projects' own job; the bridge does not touch PRs.

Usage: bridge.py run | once | notify | status | sync-repos | apply-settings | doctor
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import tomllib
import traceback
from datetime import datetime, timezone
from pathlib import Path

CONFIG_PATH = Path(os.environ.get("AUTONOMOUS_CONFIG", "/etc/autonomous/autonomous.toml"))
STATE_DIR = Path(os.environ.get("AUTONOMOUS_STATE", "/var/lib/autonomous"))
ROOT = Path(__file__).resolve().parent
MARK = "<!-- autonomous -->"            # every comment the bridge writes carries this
MARK_PREFIX = "<!-- autonomous"         # ...and the agent is told to add <!-- autonomous:agent -->

LABELS = {
    "agent:consulting": ("c5def5", "An agent discusses this issue with you, no code yet"),
    "agent:working": ("fbca04", "An agent is working on this"),
    "agent:needs-input": ("d93f0b", "The agent asked a question: answer in a comment"),
    "agent:ready": ("0e8a16", "Reviewed PR waits for approval"),
    "agent:failed": ("b60205", "The agent or the board hit an error"),
    "agent:done": ("5319e7", "Merged and cleaned up"),
}
EVENT_TO_LABEL = {"needs you": "agent:needs-input", "ready to approve": "agent:ready", "failed": "agent:failed"}
STATE_TO_LABEL = {"working": "agent:working", "blocked": "agent:needs-input", "error": "agent:failed", "done": "agent:done"}
MAX_AUTO_RECOVER = 2  # without config: a failed task is re-driven this many times, then left for the human


def log(*a):
    print(datetime.now().strftime("%H:%M:%S"), *a, flush=True)


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def age_s(iso: str) -> float:
    return time.time() - datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


# ---------------------------------------------------------------- config

def load_config() -> dict:
    cfg = tomllib.loads(CONFIG_PATH.read_text())
    b = cfg.setdefault("bridge", {})
    b.setdefault("poll_interval", 60)
    b.setdefault("poll_interval_max", 300)
    b.setdefault("poll_backoff_factor", 1.5)
    b.setdefault("quiet_window", 90)
    b.setdefault("auto_recover_max", MAX_AUTO_RECOVER)
    b.setdefault("repos_dir", str(Path.home() / "repos"))
    b.setdefault("herdr_projects", str(Path.home() / ".herdr-projects/data/bin/herdr-projects"))
    g = cfg.setdefault("github", {})
    g.setdefault("start_label", "start")
    g.setdefault("allow_authors", [])
    k = cfg.setdefault("consult", {})
    k.setdefault("label", "consult")
    # the consult agent; empty = the board's worker.agent. AUTONOMOUS_CONSULT_AGENT overrides the config.
    k["agent"] = os.environ.get("AUTONOMOUS_CONSULT_AGENT") or k.get("agent", "")
    for r in cfg.setdefault("repo", []):
        r.setdefault("path", str(Path(b["repos_dir"]) / r["name"]))
    return cfg


# ---------------------------------------------------------------- shell helpers

def run(args: list[str], check=True, input: str | None = None) -> str:
    p = subprocess.run(args, capture_output=True, text=True, input=input)
    if check and p.returncode != 0:
        raise RuntimeError(f"{' '.join(args[:4])}... exit {p.returncode}: {p.stderr.strip() or p.stdout.strip()}")
    return p.stdout


def gh_api(path: str, *extra: str, paginate=False):
    args = ["gh", "api", path, *extra]
    if paginate:
        args.insert(2, "--paginate")
    out = run(args)
    return json.loads(out) if out.strip() and not extra else out


def hp(cfg: dict, *args: str, check=True):
    out = run([cfg["bridge"]["herdr_projects"], *args, "--json"], check=check)
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return {"raw": out.strip()}


_me: str | None = None


def gh_user() -> str:
    global _me
    if _me is None:
        _me = run(["gh", "api", "user", "--jq", ".login"]).strip()
    return _me


def allowed(cfg: dict) -> set[str]:
    return {a.lower() for a in cfg["github"]["allow_authors"]} | {gh_user().lower()}


def said(out, cmd: str) -> str:
    """One readable line from a herdr-projects --json answer (a task object, or text it printed)."""
    if isinstance(out, dict) and out.get("raw"):
        return out["raw"]
    if isinstance(out, dict) and out.get("id"):
        return {"approve": "approved, the board merges it once checks pass", "stop": "stopped"}[cmd] + f" (now {out.get('state', '?')})"
    return f"{cmd}: {out}"


def comment(repo: str, n: int, body: str):
    run(["gh", "issue", "comment", str(n), "--repo", repo, "--body", f"{body}\n\n{MARK}"])


def set_label(repo: str, n: int, label: str | None, also_remove: tuple[str, ...] = ()):
    args = ["gh", "issue", "edit", str(n), "--repo", repo]
    for l in [*LABELS, *also_remove]:
        if l != label:
            args += ["--remove-label", l]
    if label:
        args += ["--add-label", label]
    run(args, check=False)  # removing a label the issue lacks is not an error worth failing on


def render(name: str, **kw) -> str:
    text = (ROOT / "prompts" / name).read_text()
    for k, v in kw.items():
        text = text.replace("{{" + k + "}}", str(v))
    return text


# ---------------------------------------------------------------- state

def db() -> sqlite3.Connection:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(STATE_DIR / "bridge.db", timeout=30, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("""create table if not exists tasks(
        repo text, issue integer, task_id text unique, state text, label text,
        cursor text, created_at text, updated_at text, board_state text, kind text default 'work',
        primary key(repo, issue))""")
    if "kind" not in [r[1] for r in c.execute("pragma table_info(tasks)")]:  # a db from before consult mode
        c.execute("alter table tasks add column kind text default 'work'")
    if "fixes" not in [r[1] for r in c.execute("pragma table_info(tasks)")]:  # a db from before auto-recovery
        c.execute("alter table tasks add column fixes integer default 0")
    return c


def active(c, repo: str, n: int):
    return c.execute("select * from tasks where repo=? and issue=? and state not in ('done','closed')", (repo, n)).fetchone()


def apply_state(c, task_id: str, label: str | None, note: str = "", state: str | None = None):
    """Single place that moves an issue's label. Comments only on a real change, so notify + polling never double-post."""
    c.execute("begin immediate")
    try:
        row = c.execute("select * from tasks where task_id=?", (task_id,)).fetchone()
        if not row or (row["label"] == label and (state is None or row["state"] == state)):
            c.execute("commit")
            return False
        c.execute("update tasks set label=?, state=coalesce(?, state), updated_at=? where task_id=?",
                  (label, state, now_iso(), task_id))
        c.execute("commit")
    except Exception:
        c.execute("rollback")
        raise
    if row["label"] == label:
        return False
    set_label(row["repo"], row["issue"], label)
    if note:
        comment(row["repo"], row["issue"], note)
    log(f"{row['repo']}#{row['issue']} ({task_id}) -> {label}")
    return True


def one_line(text, limit: int = 300) -> str:
    s = " ".join(str(text).split())
    return s[:limit] + ("…" if len(s) > limit else "")


def failure_text(task: dict, failure: dict | None) -> str:
    """The board's own words about what went wrong: its title, the details (the command and the error) and the next step."""
    f = failure if isinstance(failure, dict) else {}
    parts = [str(f.get("title") or task.get("msg") or "the task failed").strip(),
             str(f.get("detail") or "").strip(),
             str(f.get("next") or "").strip()]
    text = "\n\n".join(p for p in parts if p)
    return text[:1800] + ("…" if len(text) > 1800 else "")


def notes_for(label: str, task: dict, repo: str, task_id: str, failure: dict | None = None) -> str:
    msg = (task.get("msg") or "").strip()
    pr = task.get("pr")
    pr_ref = f"https://github.com/{repo}/pull/{pr}" if isinstance(pr, int) or str(pr).isdigit() else (pr or "")
    return {
        "agent:needs-input": f"🤖 The agent on `{task_id}` needs an answer:\n\n> {msg or '(see the board)'}\n\nReply in a comment here.",
        "agent:ready": f"✅ `{task_id}` passed checks and review. PR: {pr_ref}\n\nComment `/approve` to merge, or leave review comments on the PR.",
        "agent:failed": f"❌ `{task_id}` failed:\n\n{failure_text(task, failure)}\n\nComment to give the agent a hint, or `/stop`.",
        "agent:done": f"🚀 `{task_id}` is merged. {pr_ref}",
    }.get(label, "")


def board_failure(cfg: dict, task_id: str) -> dict | None:
    """The board's failure object for a task: the command and error the compact list rows do not carry."""
    d = hp(cfg, "details", task_id, check=False)
    return d.get("failure") if isinstance(d, dict) else None


def claim_fix(c, task_id: str, limit: int) -> bool:
    """Reserve one auto-fix attempt atomically, so two bridge processes cannot send the same failure twice."""
    c.execute("begin immediate")
    try:
        row = c.execute("select coalesce(fixes, 0) as fixes from tasks where task_id=?", (task_id,)).fetchone()
        if not row or row["fixes"] >= limit:
            c.execute("commit")
            return False
        c.execute("update tasks set fixes=coalesce(fixes,0)+1 where task_id=?", (task_id,))
        c.execute("commit")
        return True
    except Exception:
        c.execute("rollback")
        raise


def hp_result(cfg: dict, *args: str):
    """Like hp(), but keeps the exit code, so a best-effort command (fix, retry) can tell its own failure."""
    p = subprocess.run([cfg["bridge"]["herdr_projects"], *args, "--json"], capture_output=True, text=True)
    try:
        out = json.loads(p.stdout)
    except json.JSONDecodeError:
        out = {"raw": (p.stdout or p.stderr).strip()}
    return p.returncode == 0, out


def auto_recover(cfg: dict, c, task_id: str, failure: dict | None) -> bool:
    """Re-drive a failed task, so the issue starts instead of staying stuck on agent:failed.

    The board's own next step decides how: `fix` sends the details to an agent that can act on them, else `retry`
    repeats the step that failed (a setup that was transient, an agent that never took its instructions).
    """
    f = failure if isinstance(failure, dict) else {}
    limit = int(cfg["bridge"].get("auto_recover_max", MAX_AUTO_RECOVER))
    if limit <= 0 or f.get("auth") or f.get("sentAt") or not claim_fix(c, task_id, limit):
        return False
    cmd = "fix" if f.get("agentCanFix") else "retry"
    ok, out = hp_result(cfg, cmd, task_id)
    detail = out.get("raw") if isinstance(out, dict) and out.get("raw") else ("sent back" if ok else "no answer")
    if not ok:
        log(f"{task_id}: {cmd} failed: {one_line(detail)}")
        return False
    log(f"{task_id}: {cmd} ({one_line(detail)})")
    # The re-drive put the task back to work: clear agent:failed now, not on some later poll.
    apply_state(c, task_id, "agent:working", state="working")
    return True


# ---------------------------------------------------------------- the three jobs

def labeled_by(repo: str, n: int, label: str) -> str:
    who = run(["gh", "api", "--paginate", f"repos/{repo}/issues/{n}/events",
               "--jq", f'.[] | select(.event=="labeled" and .label.name=="{label}") | .actor.login']).split()
    return who[-1] if who else ""


def start_task(cfg: dict, c, r: dict, i: dict, kind: str):
    repo, n = r["name"], i["number"]
    start, consult = cfg["github"]["start_label"], cfg["consult"]["label"]
    body = dict(repo=repo, number=n, title=i["title"], url=i["html_url"], body=i.get("body") or "(no description)")
    settings = [a for k, v in r.get("settings", {}).items() for a in (f"--{k}", str(v))]
    if kind == "consult":
        if cfg["consult"]["agent"]:
            settings += ["--worker.agent", cfg["consult"]["agent"]]
        args = ["--desc", render("consult_from_issue.md", **body), "--chat", "--delegate"]
        label, hello = "agent:consulting", ("🧠 Consulting on this: board task `{id}`. The agent reads the code and answers "
                                            f"here; it changes nothing. Comment to discuss. Add `{start}` when the issue is ready to build.")
    else:
        args = ["--desc", render("task_from_issue.md", **body), "--merge.closes", str(n), "--delegate"]
        label, hello = "agent:working", ("🤖 On it: board task `{id}`. Comment here to talk to the agent; "
                                         "PR review comments go to it too.")
    out = hp(cfg, "add", f"{i['title']} (#{n})", "--repo", r["path"], *args, *settings)
    task_id = out.get("id") if isinstance(out, dict) else None
    if not task_id:
        m = re.match(r"(\S+):", out.get("raw", "")) if isinstance(out, dict) else None
        task_id = m.group(1) if m else None
    if not task_id:
        raise RuntimeError(f"herdr-projects add gave no id: {out}")
    c.execute("insert or replace into tasks(repo, issue, task_id, state, label, cursor, created_at, updated_at, board_state, kind)"
              " values(?,?,?,?,?,?,?,?,?,?)", (repo, n, task_id, "working", label, now_iso(), now_iso(), now_iso(), "working/", kind))
    set_label(repo, n, label, also_remove=(start,) if kind == "work" else (consult,))
    comment(repo, n, hello.format(id=task_id))
    log(f"{repo}#{n}: started {kind} {task_id}")


def end_consult(cfg: dict, c, row):
    hp(cfg, "report", row["task_id"], "--state", "done", "--msg", "Handed over to a work task (label start)", check=False)
    c.execute("update tasks set state='closed', updated_at=? where task_id=?", (now_iso(), row["task_id"]))
    log(f"{row['repo']}#{row['issue']}: consult {row['task_id']} ended")


def pick_up_issues(cfg: dict, c):
    start, consult = cfg["github"]["start_label"], cfg["consult"]["label"]
    ok = allowed(cfg)
    started = False
    for r in cfg["repo"]:
        repo = r["name"]
        for label, kind in ((consult, "consult"), (start, "work")):
            for i in gh_api(f"repos/{repo}/issues?labels={label}&state=open&per_page=100"):
                n, names = i["number"], {l["name"] for l in i.get("labels", [])}
                if "pull_request" in i or (kind == "consult" and start in names):
                    continue  # both labels: go straight to work
                row = active(c, repo, n)
                if row and (row["kind"] == kind or kind == "consult"):
                    continue  # already running; a consult label never interrupts work
                who = labeled_by(repo, n, label)
                if who.lower() not in ok:
                    set_label(repo, n, row["label"] if row else None, also_remove=(label,))
                    comment(repo, n, f"🤖 `{label}` was added by @{who}, who is not on the allowlist. Ignored.")
                    log(f"{repo}#{n}: rejected {label} from {who}")
                    continue
                if row:  # a consult is running and `start` arrived: the issue is ready to build
                    end_consult(cfg, c, row)
                    run(["gh", "issue", "edit", str(n), "--repo", repo, "--remove-label", consult], check=False)
                    i = gh_api(f"repos/{repo}/issues/{n}")  # the body the consult may have rewritten
                start_task(cfg, c, r, i, kind)
                started = True
    return started


def deliver_comments(cfg: dict, c):
    ok = allowed(cfg)
    quiet = cfg["bridge"]["quiet_window"]
    delivered = False
    for row in c.execute("select * from tasks where state not in ('done','closed')").fetchall():
        repo, n, tid, cursor = row["repo"], row["issue"], row["task_id"], row["cursor"]
        new = [x for x in gh_api(f"repos/{repo}/issues/{n}/comments?since={cursor}&per_page=100")
               if x["created_at"] > cursor and x["user"]["login"].lower() in ok and MARK_PREFIX not in (x["body"] or "")]
        if not new or age_s(new[-1]["created_at"]) < quiet:  # wait until the human stops typing
            continue
        texts = []
        for x in new:
            body = x["body"].strip()
            if body.startswith(("/approve", "/stop")):
                cmd = "approve" if body.startswith("/approve") else "stop"
                comment(repo, n, f"🤖 `{tid}`: {said(hp(cfg, cmd, tid, check=False), cmd)}")
                delivered = True
            else:
                texts.append(f"@{x['user']['login']}: {body}")
        if texts:
            prompt = "consult_comment.md" if row["kind"] == "consult" else "issue_comment.md"
            hp(cfg, "tell", tid, render(prompt, repo=repo, number=n, comments="\n\n".join(texts)))
            log(f"{repo}#{n}: told {tid} {len(texts)} comment(s)")
            delivered = True
        c.execute("update tasks set cursor=? where task_id=?", (new[-1]["created_at"], tid))
    return delivered


def sync_states(cfg: dict, c):
    tasks = hp(cfg, "list", "--all")
    by_id = {t["id"]: t for t in tasks if isinstance(t, dict) and "id" in t} if isinstance(tasks, list) else {}
    changed = False
    for row in c.execute("select * from tasks where state not in ('done','closed')").fetchall():
        t = by_id.get(row["task_id"])
        if t is None:
            continue
        state, stage = t.get("state", ""), t.get("stage") or ""
        # Act only when the board's own state changed since the last look, so a label set by notify is not
        # flipped back by a poll. "Ready to approve" is state `blocked` at stage `approval`, not a question.
        seen = f"{state}/{stage}"
        if seen == row["board_state"]:
            continue
        c.execute("update tasks set board_state=? where task_id=?", (seen, row["task_id"]))
        if row["kind"] == "consult":  # every turn end is `blocked` there: that is the conversation, not a question
            if state == "done":
                changed = apply_state(c, row["task_id"], None, state="closed") or changed
            else:
                label = "agent:failed" if state == "error" else "agent:consulting"
                failure = board_failure(cfg, row["task_id"]) if state == "error" else None
                changed = apply_state(c, row["task_id"], label,
                                      notes_for("agent:failed", t, row["repo"], row["task_id"], failure) if state == "error" else "") or changed
                if state == "error":
                    auto_recover(cfg, c, row["task_id"], failure)
            continue
        label = "agent:ready" if (state, stage) == ("blocked", "approval") else STATE_TO_LABEL.get(state)
        if label is None:
            continue
        failure = board_failure(cfg, row["task_id"]) if label == "agent:failed" else None
        changed = apply_state(c, row["task_id"], label, notes_for(label, t, row["repo"], row["task_id"], failure) if label != "agent:working" else "",
                              state="done" if state == "done" else state) or changed
        if label == "agent:failed":
            auto_recover(cfg, c, row["task_id"], failure)
    return changed


# ---------------------------------------------------------------- commands

def cmd_once(cfg) -> bool:
    c = db()
    did_work = False
    for job in (pick_up_issues, deliver_comments, sync_states):
        try:
            did_work = job(cfg, c) or did_work
        except Exception as e:  # one failing job (rate limit, a repo gone) must not stop the others
            log(f"{job.__name__}: {e}\n{traceback.format_exc().rstrip()}")  # the full traceback, so a crash is explainable
    return did_work


def cmd_run(cfg):
    base = cfg["bridge"]["poll_interval"]
    longest = cfg["bridge"]["poll_interval_max"]
    factor = cfg["bridge"]["poll_backoff_factor"]
    log(f"gh-bridge: {len(cfg['repo'])} repos, every {base}-{longest}s, as {gh_user()}")
    current = base
    while True:
        if cmd_once(cfg):
            current = base  # activity: back to the fast interval
        else:
            current = min(current * factor, longest)  # quiet: wait longer, up to the cap
        time.sleep(current)


def cmd_notify(cfg):
    """board.notify hook: herdr-projects runs it with HERDR_PROJECTS_ID / _EVENT on needs you / ready / failed."""
    tid, event = os.environ.get("HERDR_PROJECTS_ID", ""), os.environ.get("HERDR_PROJECTS_EVENT", "")
    label = EVENT_TO_LABEL.get(event)
    c = db()
    try:
        row = c.execute("select * from tasks where task_id=?", (tid,)).fetchone()
        if not label or not row or (row["kind"] == "consult" and event != "failed"):
            return  # a task not started from GitHub, or a consult's turn end (the agent answers on the issue itself)
        t = next((x for x in hp(cfg, "list", "--all") if isinstance(x, dict) and x.get("id") == tid), {})
        if t.get("id") == tid:  # record what this hook acted on, exactly as a poll does
            c.execute("update tasks set board_state=? where task_id=?", (f"{t.get('state', '')}/{t.get('stage') or ''}", tid))
        failure = board_failure(cfg, tid) if label == "agent:failed" else None
        if label == "agent:failed":
            log(f"{tid}: board {event}: {one_line(failure_text(t, failure))}")  # the why, in the journal: the list row has only "Setup failed"
        apply_state(c, tid, label, notes_for(label, t, row["repo"], tid, failure))
        if label == "agent:failed":
            auto_recover(cfg, c, tid, failure)
    finally:
        c.close()


def cmd_status(cfg):
    for r in db().execute("select * from tasks order by updated_at desc"):
        print(f"{r['task_id']:<14} {r['repo']}#{r['issue']:<6} {r['state']:<9} {r['label'] or '':<18} {r['updated_at']}")


def cmd_sync_repos(cfg):
    for r in cfg["repo"]:
        p = Path(r["path"])
        if not (p / ".git").exists():
            p.parent.mkdir(parents=True, exist_ok=True)
            run(["gh", "repo", "clone", r["name"], str(p)])
            log(f"cloned {r['name']} -> {p}")
        else:
            run(["git", "-C", str(p), "fetch", "--prune", "--quiet"], check=False)
        extra = {cfg["github"]["start_label"]: ("1d76db", "Start an agent on this issue"),
                 cfg["consult"]["label"]: ("bfd4f2", "Discuss this issue with an agent before building it")}
        for name, (color, desc) in {**LABELS, **extra}.items():
            run(["gh", "label", "create", name, "--repo", r["name"], "--color", color, "--description", desc, "--force"], check=False)
    log(f"{len(cfg['repo'])} repos in place, labels ensured")


def cmd_apply_settings(cfg):
    for k, v in cfg.get("herdr_projects", {}).items():
        out = hp(cfg, "set", k, str(v).lower() if isinstance(v, bool) else str(v), check=False)
        log(f"set {k} = {v}: {out.get('raw') if isinstance(out, dict) and 'raw' in out else 'ok'}")
    # `autonomous logs` tails this file next to the journal: the detached hook cannot share the bridge's journal unit.
    notify = f"{ROOT / 'bridge.py'} notify >> {STATE_DIR / 'notify.log'} 2>&1"
    hp(cfg, "set", "board.notify", notify, check=False)
    log(f"set board.notify = {notify}")
    for r in cfg["repo"]:
        for k, v in r.get("repo_settings", {}).items():
            hp(cfg, "set", k, str(v), "--repo", r["path"], check=False)


def cmd_doctor(cfg):
    checks = {
        "gh auth": ["gh", "auth", "status"],
        "herdr server": ["herdr", "workspace", "list"],
        "herdr-projects": [cfg["bridge"]["herdr_projects"], "doctor"],
        "pi": ["pi", "--version"],
    }
    bad = 0
    for name, args in checks.items():
        try:
            p = subprocess.run(args, capture_output=True, text=True, timeout=60)
            ok = p.returncode == 0
            print(f"{'✓' if ok else '✗'} {name}" + ("" if ok else f": {(p.stderr or p.stdout).strip()[:300]}"))
        except Exception as e:
            ok = False
            print(f"✗ {name}: {e}")
        bad += not ok
    print(f"{'✓' if os.environ.get('OPENROUTER_API_KEY') else '✗'} OPENROUTER_API_KEY in env")
    missing = [r["name"] for r in cfg["repo"] if not (Path(r["path"]) / ".git").exists()]
    print(f"{'✓' if not missing else '✗'} repos cloned" + (f" (missing: {', '.join(missing)})" if missing else ""))
    sys.exit(1 if bad or missing else 0)


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    fn = {"run": cmd_run, "once": cmd_once, "notify": cmd_notify, "status": cmd_status, "sync-repos": cmd_sync_repos,
          "apply-settings": cmd_apply_settings, "doctor": cmd_doctor}.get(cmd)
    if not fn:
        sys.exit(__doc__)
    fn(load_config())


if __name__ == "__main__":
    main()
