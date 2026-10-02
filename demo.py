#!/usr/bin/env python3
"""Run the dashboard on SYNTHETIC data (fake accounts, random traffic) for screenshots.

    python3 demo.py [port]      # serves http://127.0.0.1:<port>/ (default 8319)
"""
import os, random, sys, tempfile, time, math
DB = os.path.join(tempfile.gettempdir(), "usage-dash-demo.db")
if os.path.exists(DB): os.remove(DB)
os.environ["DASH_DB"] = DB
import usage_dash as ud
from http.server import ThreadingHTTPServer

random.seed(7)
NOW = time.time()
DAYS = 7
H = 3600

# (provider, email, plan, weight, 5h fill target, 7d fill target, status)
ACCOUNTS = [
    ("claude", "alice@example.com",   "max",  1.00, 0.78, 0.64, "active"),
    ("claude", "bob@example.com",     "max",  0.70, 0.41, 0.82, "active"),
    ("claude", "carol@example.com",   "pro",  0.45, 0.93, 0.97, "active"),
    ("claude", "team@example.org",    "team", 0.55, 0.22, 0.35, "active"),
    ("claude", "dave@example.com",    "pro",  0.25, 0.05, 0.12, "cooldown"),
    ("codex",  "alice@example.com",   "pro",  0.80, 0.52, 0.47, "active"),
    ("codex",  "erin@example.org",    "plus", 0.35, 0.88, 0.58, "active"),
    ("codex",  "frank@example.com",   "pro",  0.60, 0.30, 0.71, "active"),
]
MODELS = {
    "claude": [("claude-sonnet-5-5", 5), ("claude-opus-5-5", 3), ("claude-haiku-4-5-20251001", 2)],
    "codex": [("gpt-6.1-sol", 6), ("gpt-6.1-mini", 2)],
}
CLIENTS = [("claude-cli/2.1.4 (external, cli)", "100.64.0.11", 5), ("codex_cli_rs/0.9.2", "100.64.0.12", 3),
           ("opencode/1.2.8", "100.64.0.13", 2), ("Zed/0.231.1", "100.64.0.14", 1), ("curl/8.22.0", "127.0.0.1", 1)]
ENDPOINTS = {"claude": ["POST /v1/messages", "POST /v1/messages", "POST /v1/chat/completions"],
             "codex": ["POST /v1/responses", "POST /v1/responses", "POST /v1/chat/completions"]}

def wpick(items):
    return random.choices([i[:-1] for i in items], [i[-1] for i in items])[0]

def sessions(weight):
    """Work sessions: (start, end, req/min). Weekday daytime/evening heavy, plus one running now."""
    out = []
    for d in range(DAYS, -1, -1):
        day0 = time.mktime(time.localtime(NOW - d * 86400)[:3] + (0, 0, 0, 0, 0, -1))
        wk = time.localtime(day0 + 7200).tm_wday
        for _ in range(sum(random.random() < weight * (0.5 if wk >= 5 else 0.85) for _ in range(5))):
            st = day0 + random.choice([9, 10, 11, 13, 14, 15, 16, 19, 20, 21, 22]) * H + random.randint(0, 3000)
            out.append((st, st + random.randint(25, 140) * 60, random.uniform(0.6, 4.5) * (0.5 + weight)))
    out.append((NOW - random.randint(20, 70) * 60, NOW + 3600, random.uniform(1.5, 4) * (0.5 + weight)))
    return [s for s in out if s[0] < NOW]

reqs, lims, auths = [], [], []
for n, (prov, email, plan, w, t5, t7, status) in enumerate(ACCOUNTS):
    idx = "%016x" % random.getrandbits(64)
    ev = []
    for st, en, rate in sessions(w):
        t = st
        while t < min(en, NOW):
            t += random.expovariate(rate / 60)
            if t >= min(en, NOW): break
            model = wpick(MODELS[prov])[0]
            ua, ip = wpick(CLIENTS)[:2]
            tin = int(random.lognormvariate(7.4, 1.3)); tout = int(random.lognormvariate(5.6, 1.0))
            tcache = int(tin * random.uniform(3, 30)) if prov == "claude" and random.random() < .8 else 0
            fail = random.random() < 0.02
            status_code = 200
            if fail:
                status_code = random.choice([429, 429, 499, 529, 500])
            lat = int(random.lognormvariate(8.2, .8) * (2.2 if "opus" in model or "sol" in model else 1))
            ev.append((t, idx, prov, model, wpick([(e, 1) for e in ENDPOINTS[prov]])[0], status_code, int(fail),
                       int(random.random() < .7), lat, int(lat * random.uniform(.1, .4)),
                       0 if fail else tin, 0 if fail else tout, 0 if fail or random.random() < .6 else int(tout * .5),
                       0 if fail else tcache, 0, 0 if fail else tin + tout + tcache, ua, ip))
    ev.sort()
    reqs += [e[:15] + (e[10] + e[11] + e[13],) + e[16:] for e in ev]
    # limit windows: 5h and 7d, anchored per account, sized so "now" lands near the target fill
    tok = [(e[0], e[10] + e[11] + e[13] * 0.1) for e in ev]   # cache reads weigh little against limits
    for metric, win, target in (("5h", 5 * H, t5), ("7d", 7 * 86400, t7)):
        anchor = NOW - min(.95, max(.12, target / random.uniform(0.75, 1.5))) * win
        cur0 = anchor
        cur_sum = sum(v for t, v in tok if t >= cur0)
        cap = max(cur_sum, 1) / max(target, .02) if cur_sum > 0 else 1e6 * target
        t = NOW - DAYS * 86400
        while t < NOW - 1:
            k = math.floor((t - anchor) / win)
            w0 = anchor + k * win
            used = sum(v for tt, v in tok if w0 <= tt <= t)
            lims.append((t, idx, metric, min(100.0, 100 * used / cap), w0 + win, win))
            t += 600
        used_now = sum(v for tt, v in tok if cur0 <= tt <= NOW)
        lims.append((NOW, idx, metric, min(100.0, 100 * used_now / cap) if cur_sum else target * 100, cur0 + win, win))
    signals = {}
    if prov == "codex":
        signals = {"X-Codex-Credits-Balance": "%.4f" % random.uniform(2e3, 9e4), "X-Codex-Credits-Unlimited": "False"}
    else:
        signals = {"Anthropic-Ratelimit-Unified-Overage-Status": "rejected",
                   "Anthropic-Ratelimit-Unified-Overage-Disabled-Reason": "org_level_disabled",
                   "Anthropic-Ratelimit-Unified-Representative-Claim": random.choice(["five_hour", "seven_day"])}
    auths.append({"auth_index": idx, "provider": prov, "email": email, "label": email, "status": "active",
                  "unavailable": status == "cooldown", "cooldowns": [{"model": "*"}] if status == "cooldown" else [],
                  "id_token": {"plan_type": plan, "chatgpt_subscription_active_until": "2026-11-05T10:11:47+00:00"} if prov == "codex" else {},
                  "quota": {"signals": signals}})
    ud.ex("insert or replace into auth values(?,?,?,?,?)", (idx, prov, email, plan, NOW))

with ud.LOCK:
    ud.DB.executemany("insert into req values(%s)" % ",".join("?" * 18), reqs)
    ud.DB.executemany("insert into lim values(?,?,?,?,?,?)", lims)
    ud.DB.commit()
ud.STATE.update(auths=auths, auths_at=NOW, queue_ok=True)

if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8319
    print("demo (synthetic data): %d requests, %d accounts -> http://127.0.0.1:%d/" % (len(reqs), len(ACCOUNTS), port), flush=True)
    ThreadingHTTPServer(("127.0.0.1", port), ud.H).serve_forever()
