#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""gh-bridge: GitHub issues <-> herdr-projects board, for many repos.

  label `start` on an issue      -> herdr-projects add ... --merge.closes N --delegate
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
from datetime import datetime, timezone
from pathlib import Path

CONFIG_PATH = Path(os.environ.get("AUTONOMOUS_CONFIG", "/etc/autonomous/autonomous.toml"))
STATE_DIR = Path(os.environ.get("AUTONOMOUS_STATE", "/var/lib/autonomous"))
ROOT = Path(__file__).resolve().parent
MARK = "<!-- autonomous -->"            # every comment the bridge writes carries this
MARK_PREFIX = "<!-- autonomous"         # ...and the agent is told to add <!-- autonomous:agent -->

LABELS = {
    "agent:working": ("fbca04", "An agent is working on this"),
    "agent:needs-input": ("d93f0b", "The agent asked a question: answer in a comment"),
    "agent:ready": ("0e8a16", "Reviewed PR waits for approval"),
    "agent:failed": ("b60205", "The agent or the board hit an error"),
    "agent:done": ("5319e7", "Merged and cleaned up"),
}
EVENT_TO_LABEL = {"needs you": "agent:needs-input", "ready to approve": "agent:ready", "failed": "agent:failed"}
STATE_TO_LABEL = {"working": "agent:working", "blocked": "agent:needs-input", "error": "agent:failed", "done": "agent:done"}


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
    b.setdefault("quiet_window", 90)
    b.setdefault("repos_dir", str(Path.home() / "repos"))
    b.setdefault("herdr_projects", str(Path.home() / ".herdr-projects/data/bin/herdr-projects"))
    g = cfg.setdefault("github", {})
    g.setdefault("start_label", "start")
    g.setdefault("allow_authors", [])
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
        cursor text, created_at text, updated_at text, board_state text, primary key(repo, issue))""")
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
            return
        c.execute("update tasks set label=?, state=coalesce(?, state), updated_at=? where task_id=?",
                  (label, state, now_iso(), task_id))
        c.execute("commit")
    except Exception:
        c.execute("rollback")
        raise
    if row["label"] == label:
        return
    set_label(row["repo"], row["issue"], label)
    if note:
        comment(row["repo"], row["issue"], note)
    log(f"{row['repo']}#{row['issue']} ({task_id}) -> {label}")


def notes_for(label: str, task: dict, repo: str, task_id: str) -> str:
    msg = (task.get("msg") or "").strip()
    pr = task.get("pr")
    pr_ref = f"https://github.com/{repo}/pull/{pr}" if isinstance(pr, int) or str(pr).isdigit() else (pr or "")
    return {
        "agent:needs-input": f"🤖 The agent on `{task_id}` needs an answer:\n\n> {msg or '(see the board)'}\n\nReply in a comment here.",
        "agent:ready": f"✅ `{task_id}` passed checks and review. PR: {pr_ref}\n\nComment `/approve` to merge, or leave review comments on the PR.",
        "agent:failed": f"❌ `{task_id}` failed: {msg or 'see the board'}\n\nComment to give the agent a hint, or `/stop`.",
        "agent:done": f"🚀 `{task_id}` is merged. {pr_ref}",
    }.get(label, "")


# ---------------------------------------------------------------- the three jobs

def pick_up_issues(cfg: dict, c):
    start = cfg["github"]["start_label"]
    ok = allowed(cfg)
    for r in cfg["repo"]:
        repo = r["name"]
        issues = gh_api(f"repos/{repo}/issues?labels={start}&state=open&per_page=100")
        for i in issues:
            if "pull_request" in i or active(c, repo, i["number"]):
                continue
            n = i["number"]
            who = run(["gh", "api", "--paginate", f"repos/{repo}/issues/{n}/events",
                       "--jq", f'.[] | select(.event=="labeled" and .label.name=="{start}") | .actor.login']).split()
            who = who[-1] if who else ""
            if who.lower() not in ok:
                set_label(repo, n, None, also_remove=(start,))
                comment(repo, n, f"🤖 `{start}` was added by @{who}, who is not on the allowlist. Ignored.")
                log(f"{repo}#{n}: rejected label from {who}")
                continue
            desc = render("task_from_issue.md", repo=repo, number=n, title=i["title"],
                          url=i["html_url"], body=i.get("body") or "(no description)")
            settings = [a for k, v in r.get("settings", {}).items() for a in (f"--{k}", str(v))]
            out = hp(cfg, "add", f"{i['title']} (#{n})", "--repo", r["path"], "--desc", desc,
                     "--merge.closes", str(n), "--delegate", *settings)
            task_id = out.get("id") if isinstance(out, dict) else None
            if not task_id:
                m = re.match(r"(\S+):", out.get("raw", "")) if isinstance(out, dict) else None
                task_id = m.group(1) if m else None
            if not task_id:
                raise RuntimeError(f"herdr-projects add gave no id: {out}")
            c.execute("insert or replace into tasks values(?,?,?,?,?,?,?,?,?)",
                      (repo, n, task_id, "working", "agent:working", now_iso(), now_iso(), now_iso(), "working/"))
            set_label(repo, n, "agent:working", also_remove=(start,))
            comment(repo, n, f"🤖 On it: board task `{task_id}`. Comment here to talk to the agent; PR review comments go to it too.")
            log(f"{repo}#{n}: started {task_id}")


def deliver_comments(cfg: dict, c):
    ok = allowed(cfg)
    quiet = cfg["bridge"]["quiet_window"]
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
            else:
                texts.append(f"@{x['user']['login']}: {body}")
        if texts:
            hp(cfg, "tell", tid, render("issue_comment.md", repo=repo, number=n, comments="\n\n".join(texts)))
            log(f"{repo}#{n}: told {tid} {len(texts)} comment(s)")
        c.execute("update tasks set cursor=? where task_id=?", (new[-1]["created_at"], tid))


def sync_states(cfg: dict, c):
    tasks = hp(cfg, "list", "--all")
    by_id = {t["id"]: t for t in tasks if isinstance(t, dict) and "id" in t} if isinstance(tasks, list) else {}
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
        label = "agent:ready" if (state, stage) == ("blocked", "approval") else STATE_TO_LABEL.get(state)
        if label is None:
            continue
        apply_state(c, row["task_id"], label, notes_for(label, t, row["repo"], row["task_id"]) if label != "agent:working" else "",
                    state="done" if state == "done" else state)


# ---------------------------------------------------------------- commands

def cmd_once(cfg):
    c = db()
    for job in (pick_up_issues, deliver_comments, sync_states):
        try:
            job(cfg, c)
        except Exception as e:  # one failing job (rate limit, a repo gone) must not stop the others
            log(f"{job.__name__}: {e}")


def cmd_run(cfg):
    log(f"gh-bridge: {len(cfg['repo'])} repos, every {cfg['bridge']['poll_interval']}s, as {gh_user()}")
    while True:
        cmd_once(cfg)
        time.sleep(cfg["bridge"]["poll_interval"])


def cmd_notify(cfg):
    """board.notify hook: herdr-projects runs it with HERDR_PROJECTS_ID / _EVENT on needs you / ready / failed."""
    tid, event = os.environ.get("HERDR_PROJECTS_ID", ""), os.environ.get("HERDR_PROJECTS_EVENT", "")
    label = EVENT_TO_LABEL.get(event)
    c = db()
    row = c.execute("select * from tasks where task_id=?", (tid,)).fetchone()
    if not label or not row:
        return  # a task not started from GitHub
    t = next((x for x in hp(cfg, "list", "--all") if isinstance(x, dict) and x.get("id") == tid), {})
    apply_state(c, tid, label, notes_for(label, t, row["repo"], tid))


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
        for name, (color, desc) in {**LABELS, cfg["github"]["start_label"]: ("1d76db", "Start an agent on this issue")}.items():
            run(["gh", "label", "create", name, "--repo", r["name"], "--color", color, "--description", desc, "--force"], check=False)
    log(f"{len(cfg['repo'])} repos in place, labels ensured")


def cmd_apply_settings(cfg):
    for k, v in cfg.get("herdr_projects", {}).items():
        out = hp(cfg, "set", k, str(v).lower() if isinstance(v, bool) else str(v), check=False)
        log(f"set {k} = {v}: {out.get('raw') if isinstance(out, dict) and 'raw' in out else 'ok'}")
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
