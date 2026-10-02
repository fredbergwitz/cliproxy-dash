#!/usr/bin/env python3
"""Backfill the dashboard from local Claude Code / Codex CLI session logs.

Reads ONLY timestamps, model names and token/limit counters from the JSONL
logs; message text is never read into the database. Imported rows are tagged
`local session log (...)` and stop at the first request the proxy itself
recorded, so proxied sessions are not counted twice. Safe to re-run: earlier
imports are replaced.

    python3 import_logs.py [--claude DIR] [--codex DIR] [--dry-run]

With several accounts per provider, pick one: --claude-auth / --codex-auth INDEX.
"""
import argparse, glob, json, os, sqlite3
from datetime import datetime

TAG = "local session log"

def ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()

def claude_rows(root):
    """One row per API message (streamed chunks share a message id; keep the last)."""
    best = {}
    for f in glob.glob(os.path.join(root, "**", "*.jsonl"), recursive=True):
        for line in open(f, errors="replace"):
            try:
                o = json.loads(line)
            except ValueError:
                continue
            m = o.get("message")
            if not isinstance(m, dict) or not isinstance(m.get("usage"), dict) or not o.get("timestamp"):
                continue
            model = m.get("model") or ""
            if not model or model.startswith("<"):
                continue
            u = m["usage"]
            key = m.get("id") or o.get("requestId") or o.get("uuid")
            out = u.get("output_tokens") or 0
            if key not in best or out >= best[key][5]:
                best[key] = (ts(o["timestamp"]), model, u.get("input_tokens") or 0, u.get("cache_read_input_tokens") or 0,
                             u.get("cache_creation_input_tokens") or 0, out)
    return [("claude", t, model, tin, tout, tc, tw, 0, tin + tout + tc + tw)
            for t, model, tin, tc, tw, tout in best.values()]

def codex_rows(root):
    """One row per model request (token_count events whose running total moved) + limit snapshots."""
    reqs, lims = [], []
    for f in glob.glob(os.path.join(root, "**", "*.jsonl"), recursive=True):
        model, last_total = "codex", None
        for line in open(f, errors="replace"):
            try:
                o = json.loads(line)
            except ValueError:
                continue
            p = o.get("payload")
            if not isinstance(p, dict):
                continue
            if o.get("type") == "turn_context" and p.get("model"):
                model = p["model"]
            if p.get("type") != "token_count" or not o.get("timestamp"):
                continue
            t = ts(o["timestamp"])
            for name in ("primary", "secondary"):
                w = (p.get("rate_limits") or {}).get(name)
                if isinstance(w, dict) and w.get("window_minutes") and w.get("resets_at") is not None:
                    lims.append((t, w["window_minutes"], float(w.get("used_percent") or 0), float(w["resets_at"])))
            info = p.get("info")
            if not isinstance(info, dict):
                continue
            tot = (info.get("total_token_usage") or {}).get("total_tokens")
            if tot is None or tot == last_total:
                continue
            last_total = tot
            u = info.get("last_token_usage") or {}
            cached = u.get("cached_input_tokens") or 0
            reqs.append(("codex", t, model, max((u.get("input_tokens") or 0) - cached, 0), u.get("output_tokens") or 0,
                         cached, u.get("cache_write_input_tokens") or 0, u.get("reasoning_output_tokens") or 0,
                         u.get("total_tokens") or 0))
    return reqs, lims

def win_label(m):
    return ("%dd" % (m // 1440)) if m >= 1440 else ("%dh" % (m // 60))

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.environ.get("DASH_DB", os.path.expanduser("~/.local/share/usage-dash/usage.db")))
    ap.add_argument("--claude", default=os.path.expanduser("~/.claude/projects"))
    ap.add_argument("--codex", default=os.path.expanduser("~/.codex/sessions"))
    ap.add_argument("--claude-auth"); ap.add_argument("--codex-auth")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    db = sqlite3.connect(a.db)
    auth = {}
    for prov, want in (("claude", a.claude_auth), ("codex", a.codex_auth)):
        ids = [r[0] for r in db.execute("select idx from auth where provider=?", (prov,))]
        if want: auth[prov] = want
        elif len(ids) == 1: auth[prov] = ids[0]
        elif ids: raise SystemExit("several %s accounts %s: pass --%s-auth" % (prov, ids, prov))
        else: print("no %s account in the database yet; skipping %s" % (prov, prov))

    cut = db.execute("select min(ts) from req where ua not like ?", (TAG + "%",)).fetchone()[0] or 1e18
    rows, lims = [], []
    if "claude" in auth and os.path.isdir(a.claude):
        rows += claude_rows(a.claude)
    if "codex" in auth and os.path.isdir(a.codex):
        r, lims = codex_rows(a.codex)
        rows += r
    rows = [r for r in rows if r[1] < cut]
    lims = [l for l in lims if l[0] < cut]

    ins = [(t, auth[prov], prov, model, "local log", 200, 0, 0, 0, 0, tin, tout, tr, tc, tw, tot,
            "%s (%s)" % (TAG, "claude-code" if prov == "claude" else "codex-cli"), "local")
           for prov, t, model, tin, tout, tc, tw, tr, tot in rows]
    # limit snapshots: keep changes only
    lim_ins, seen = [], {}
    for t, mins, used, reset in sorted(lims):
        k = win_label(mins)
        if seen.get(k) != (used, reset):
            seen[k] = (used, reset)
            lim_ins.append((t, auth["codex"], k, used, reset, mins * 60))
    print("cutoff (first proxied request): %s" % (datetime.fromtimestamp(cut).isoformat(" ", "seconds") if cut < 1e18 else "none"))
    print("requests to import: %d (claude %d, codex %d), limit snapshots: %d" % (
        len(ins), sum(r[2] == "claude" for r in ins), sum(r[2] == "codex" for r in ins), len(lim_ins)))
    if ins:
        print("range: %s .. %s" % (datetime.fromtimestamp(min(r[0] for r in ins)).date(), datetime.fromtimestamp(max(r[0] for r in ins)).date()))
    if a.dry_run:
        return
    with db:
        db.execute("delete from req where ua like ?", (TAG + "%",))
        db.executemany("insert into req values(%s)" % ",".join("?" * 18), ins)
        if lim_ins:
            db.execute("delete from lim where auth=? and ts<?", (auth["codex"], cut))
            db.executemany("insert into lim values(?,?,?,?,?,?)", lim_ins)
    print("done")

if __name__ == "__main__":
    main()
