#!/usr/bin/env python3
"""cliproxy-dash: tiny gnuplot.info-styled usage dashboard for CLIProxyAPI.

Polls the proxy's management API (usage queue + auth files), keeps history in
SQLite, and serves one server-rendered HTML page with inline SVG plots.
Standard library only; no JavaScript.

Environment (e.g. from ~/.config/usage-dash/env):
  CPA_URL        proxy base URL            (default http://127.0.0.1:8317)
  CPA_MGMT_KEY   management secret         (required)
  DASH_BIND      comma list of host:port   (default 127.0.0.1:8318)
  DASH_DB        sqlite path               (default ~/.local/share/usage-dash/usage.db)
"""
import html, json, os, re, sqlite3, sys, threading, time, urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

CPA_URL = os.environ.get("CPA_URL", "http://127.0.0.1:8317").rstrip("/")
KEY = os.environ.get("CPA_MGMT_KEY", "")
BIND = os.environ.get("DASH_BIND", "127.0.0.1:8318")
DB_PATH = os.environ.get("DASH_DB", os.path.expanduser("~/.local/share/usage-dash/usage.db"))
STARTED = time.time()
LOCK = threading.Lock()
STATE = {"auths": [], "auths_at": 0, "queue_ok": None, "queue_at": 0, "err": ""}

PALETTE = ["#9400d3", "#009e73", "#56b4e9", "#e69f00", "#0072b2", "#e51e10", "#000000", "#999999"]
RANGES = {  # name: (seconds, bucket seconds)
    "6h": (6 * 3600, 600), "24h": (86400, 1800), "7d": (7 * 86400, 10800), "30d": (30 * 86400, 86400),
}

# ------------------------------------------------------------------ storage
def db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    c = sqlite3.connect(DB_PATH, check_same_thread=False)
    c.execute("pragma journal_mode=wal")
    c.executescript("""
    create table if not exists req(ts real, auth text, provider text, model text, endpoint text,
      status int, failed int, stream int, lat int, ttft int, tin int, tout int, treason int,
      tcache int, twrite int, ttotal int, ua text, ip text);
    create index if not exists req_ts on req(ts);
    create table if not exists lim(ts real, auth text, metric text, val real, reset real, win int);
    create index if not exists lim_ts on lim(ts);
    create table if not exists auth(idx text primary key, provider text, label text, plan text, seen real);
    """)
    return c

DB = db()

def q(sql, args=()):
    with LOCK:
        return DB.execute(sql, args).fetchall()

def ex(sql, args=()):
    with LOCK:
        DB.execute(sql, args)
        DB.commit()

# ---------------------------------------------------------------- collector
def mgmt(path, method="GET"):
    r = urllib.request.Request(CPA_URL + "/v0/management/" + path, method=method,
                               headers={"Authorization": "Bearer " + KEY})
    with urllib.request.urlopen(r, timeout=8) as f:
        return json.load(f)

def parse_ts(s):
    s = re.sub(r"(\.\d{6})\d+", r"\1", s)
    return datetime.fromisoformat(s).timestamp()

def ingest_queue():
    recs = mgmt("usage-queue")
    for r in recs or []:
        t = r.get("tokens") or {}
        fl = r.get("fail") or {}
        ts = parse_ts(r["timestamp"]) if r.get("timestamp") else time.time()
        cache = t.get("cache_read_tokens") or t.get("cached_tokens") or 0
        ex("insert into req values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            ts, r.get("auth_index", ""), r.get("provider", ""), r.get("alias") or r.get("model", ""),
            r.get("endpoint", ""), fl.get("status_code") or 0, int(bool(r.get("failed"))),
            int(bool(r.get("stream"))), r.get("latency_ms") or 0, r.get("ttft_ms") or 0,
            t.get("input_tokens") or 0, t.get("output_tokens") or 0, t.get("reasoning_tokens") or 0,
            cache, t.get("cache_creation_tokens") or 0, t.get("total_tokens") or 0,
            r.get("user_agent", ""), r.get("resolved_client_ip") or r.get("client_ip", "")))
    STATE["queue_ok"], STATE["queue_at"] = True, time.time()

def label_win(minutes):
    return ("%dd" % (minutes // 1440)) if minutes >= 1440 else ("%dh" % (minutes // 60))

def limits_from(f):
    """Normalise provider rate-limit signals into [(metric, used%, reset_ts, window_s)]."""
    s = (f.get("quota") or {}).get("signals") or {}
    out = []
    if f.get("provider") == "claude":
        for k, secs in (("5h", 18000), ("7d", 604800)):
            u, r = s.get("Anthropic-Ratelimit-Unified-%s-Utilization" % k), s.get("Anthropic-Ratelimit-Unified-%s-Reset" % k)
            if u is not None and r is not None:
                out.append((k, float(u) * 100, float(r), secs))
    elif f.get("provider") == "codex":
        for n in ("Primary", "Secondary"):
            m = int(float(s.get("X-Codex-%s-Window-Minutes" % n) or 0))
            u, r = s.get("X-Codex-%s-Used-Percent" % n), s.get("X-Codex-%s-Reset-At" % n)
            if m and u is not None and r is not None:
                out.append((label_win(m), float(u), float(r), m * 60))
    return out

_last_lim = {}
def poll_auths():
    files = mgmt("auth-files").get("files", [])
    now = time.time()
    for f in files:
        plan = (f.get("id_token") or {}).get("plan_type", "")
        ex("insert or replace into auth values(?,?,?,?,?)", (f["auth_index"], f["provider"], f.get("email") or f.get("label", ""), plan, now))
        for metric, used, reset, win in limits_from(f):
            k = (f["auth_index"], metric)
            prev = _last_lim.get(k)
            if prev is None or prev[0] != used or prev[1] != reset or now - prev[2] > 300:
                ex("insert into lim values(?,?,?,?,?,?)", (now, f["auth_index"], metric, used, reset, win))
                _last_lim[k] = (used, reset, now)
    STATE["auths"], STATE["auths_at"] = files, now

def collector():
    n = 0
    while True:
        try:
            ingest_queue()
            if n % 6 == 0:
                poll_auths()
            STATE["err"] = ""
        except Exception as e:  # keep running; surface in footer
            STATE["err"] = "%s: %s" % (type(e).__name__, e)
            STATE["queue_ok"] = False
        n += 1
        time.sleep(5)

# ---------------------------------------------------------------- formatting
esc = lambda s: html.escape(str(s), quote=True)

def fnum(n):
    n = float(n)
    for u, d in (("G", 1e9), ("M", 1e6), ("k", 1e3)):
        if abs(n) >= d:
            return "%.1f%s" % (n / d, u)
    return "%d" % n

def fdur(s):
    s = int(max(s, 0))
    d, s = divmod(s, 86400); h, s = divmod(s, 3600); m, _ = divmod(s, 60)
    if d: return "%dd %02dh %02dm" % (d, h, m)
    if h: return "%dh %02dm" % (h, m)
    return "%dm" % m

def fclock(ts, long=True):
    return time.strftime("%a %d %b %H:%M" if long else "%H:%M", time.localtime(ts))

def tbar(pct, width=20):
    pct = max(0, min(100, pct))
    n = int(round(pct / 100 * width))
    return "[" + "#" * n + "." * (width - n) + "]"

# ------------------------------------------------------------------ plotting
def nice_ticks(lo, hi, n=5):
    if hi <= lo: hi = lo + 1
    raw = (hi - lo) / n
    mag = 10 ** int(__import__("math").floor(__import__("math").log10(raw)))
    step = next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)
    t = int(lo // step) * step
    out = []
    while t <= hi + 1e-9:
        if t >= lo - 1e-9: out.append(t)
        t += step
    return out, step

def time_ticks(t0, t1):
    off = time.localtime(t1).tm_gmtoff
    span = t1 - t0
    for step in (600, 1800, 3600, 3 * 3600, 6 * 3600, 12 * 3600, 86400, 2 * 86400, 7 * 86400):
        if span / step <= 8: break
    first = ((t0 + off) // step + 1) * step - off
    ticks = []
    t = first
    while t <= t1:
        ticks.append(t); t += step
    fmt = "%H:%M" if step < 86400 and span <= 86400 * 1.2 else ("%a %H:%M" if step < 86400 else "%d %b")
    return [(t, time.strftime(fmt, time.localtime(t))) for t in ticks]

class Plot:
    """Classic gnuplot-ish axes: boxed border, inward tics, dotted grid, boxed key."""
    def __init__(self, w, h, title, xr, yr, xticks, ylabel="", yfmt=None, lm=48, rm=12):
        self.w, self.h, self.xr, self.yr = w, h, xr, yr
        self.l, self.r, self.t, self.b = lm, rm, 24, 30
        self.pw, self.ph = w - self.l - self.r, h - self.t - self.b
        self.parts = []
        yt, _ = nice_ticks(yr[0], yr[1], 5)
        self.yr = (yr[0], max(yr[1], yt[-1]) if yt else yr[1])
        yt, _ = nice_ticks(*self.yr, 5)
        for v in yt:
            y = self.y(v)
            self.parts.append('<line x1="%d" x2="%d" y1="%.1f" y2="%.1f" class="grid"/>' % (self.l, self.l + self.pw, y, y))
            self.parts.append('<text x="%d" y="%.1f" class="tl" text-anchor="end">%s</text>' % (self.l - 5, y + 3.5, (yfmt or fnum)(v)))
        for v, lab in xticks:
            x = self.x(v)
            self.parts.append('<line x1="%.1f" x2="%.1f" y1="%d" y2="%d" class="grid"/>' % (x, x, self.t, self.t + self.ph))
            self.parts.append('<text x="%.1f" y="%d" class="tl" text-anchor="middle">%s</text>' % (x, self.t + self.ph + 14, esc(lab)))
        self.title, self.ylabel, self.key = title, ylabel, []

    def x(self, v): return self.l + (v - self.xr[0]) / ((self.xr[1] - self.xr[0]) or 1) * self.pw
    def y(self, v): return self.t + self.ph - (v - self.yr[0]) / ((self.yr[1] - self.yr[0]) or 1) * self.ph

    def line(self, pts, color, label, step=True, marks=True):
        pts = sorted(pts)
        if pts:
            d, px, py = [], None, None
            for xv, yv in pts:
                x, y = self.x(xv), self.y(yv)
                if step and px is not None: d.append("L%.1f,%.1f" % (x, py))
                d.append("%s%.1f,%.1f" % ("M" if not d else "L", x, y)); px, py = x, y
            d.append("L%.1f,%.1f" % (self.l + self.pw, py))
            self.parts.append('<path d="%s" fill="none" stroke="%s" stroke-width="1.3"/>' % (" ".join(d), color))
            if marks:
                for xv, yv in pts[-60:]:
                    x, y = self.x(xv), self.y(yv)
                    self.parts.append('<path d="M%.1f,%.1f h6 M%.1f,%.1f v6" stroke="%s" stroke-width="1"/>' % (x - 3, y, x, y - 3, color))
        self.key.append(("line", color, label))

    def bars(self, buckets, series, bw):
        """series: [(label, color, [values per bucket])] stacked."""
        for i, t0 in enumerate(buckets):
            base = 0
            for lab, col, vals in series:
                v = vals[i]
                if not v: continue
                x0, x1 = self.x(t0) + 0.5, self.x(t0 + bw) - 0.5
                y0, y1 = self.y(base), self.y(base + v)
                self.parts.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" fill="%s" fill-opacity=".55" stroke="%s" stroke-width=".8"/>'
                                  % (x0, y1, max(x1 - x0, 1), max(y0 - y1, .5), col, col))
                base += v
        for lab, col, _ in series: self.key.append(("box", col, lab))

    def vline(self, xv, color="#e51e10", label=None, dash="3,3"):
        if self.xr[0] <= xv <= self.xr[1]:
            x = self.x(xv)
            self.parts.append('<line x1="%.1f" x2="%.1f" y1="%d" y2="%d" stroke="%s" stroke-dasharray="%s"/>' % (x, x, self.t, self.t + self.ph, color, dash))

    def svg(self):
        o = ['<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 %d %d" width="%d" height="%d" class="plot">' % (self.w, self.h, self.w, self.h)]
        o.append('<rect width="100%" height="100%" fill="#fff"/>')
        o += self.parts
        o.append('<rect x="%d" y="%d" width="%d" height="%d" fill="none" stroke="#000"/>' % (self.l, self.t, self.pw, self.ph))
        o.append('<text x="%d" y="14" class="ti" text-anchor="middle">%s</text>' % (self.l + self.pw // 2, esc(self.title)))
        if self.ylabel:
            o.append('<text transform="translate(11,%d) rotate(-90)" class="tl" text-anchor="middle">%s</text>' % (self.t + self.ph // 2, esc(self.ylabel)))
        if self.key:
            kw = max(len(k[2]) for k in self.key) * 6.2 + 34
            kx, ky = self.l + self.pw - kw - 6, self.t + 6
            o.append('<rect x="%.1f" y="%d" width="%.1f" height="%d" fill="#fff" fill-opacity=".9" stroke="#000"/>' % (kx, ky, kw, 14 * len(self.key) + 4))
            for i, (kind, col, lab) in enumerate(self.key):
                y = ky + 13 + i * 14
                if kind == "line":
                    o.append('<line x1="%.1f" x2="%.1f" y1="%d" y2="%d" stroke="%s" stroke-width="1.5"/>' % (kx + 6, kx + 24, y - 4, y - 4, col))
                else:
                    o.append('<rect x="%.1f" y="%d" width="18" height="8" fill="%s" fill-opacity=".55" stroke="%s"/>' % (kx + 6, y - 8, col, col))
                o.append('<text x="%.1f" y="%d" class="tl" text-anchor="end">%s</text>' % (kx + kw - 5, y - 1, esc(lab)))
        o.append("</svg>")
        return "".join(o)

def heat_color(f):
    stops = [(0, (255, 255, 255)), (.01, (225, 225, 245)), (.35, (60, 40, 190)), (.65, (200, 20, 120)), (1, (255, 200, 20))]
    for (a, ca), (b, cb) in zip(stops, stops[1:]):
        if f <= b:
            k = (f - a) / (b - a)
            return "#%02x%02x%02x" % tuple(int(ca[i] + (cb[i] - ca[i]) * k) for i in range(3))
    return "#ffc814"

# ------------------------------------------------------------------- views
def accounts():
    rows = q("select idx, provider, label, plan from auth order by provider, label")
    return {r[0]: {"idx": r[0], "provider": r[1], "label": r[2], "plan": r[3]} for r in rows}

def latest_limits():
    return q("""select l.auth, l.metric, l.val, l.reset, l.win, l.ts from lim l
                join (select auth, metric, max(ts) m from lim group by auth, metric) x
                on l.auth = x.auth and l.metric = x.metric and l.ts = x.m order by l.auth, l.win""")

def section(title, body, cls=""):
    return '<div class="sec %s"><div class="st">%s</div>%s</div>' % (cls, title, body)

def accounts_table(now):
    acc = accounts()
    stat = {f["auth_index"]: f for f in STATE["auths"]}
    lims = {}
    for a, m, v, r, w, ts in latest_limits():
        lims.setdefault(a, []).append((m, v, r, w, ts))
    day = q("select auth, count(*), sum(ttotal), sum(failed) from req where ts > ? group by auth", (now - 86400,))
    day = {r[0]: r[1:] for r in day}
    out = ['<table class="grid"><tr><th>provider</th><th>account</th><th>status</th><th>window</th><th>used</th>'
           '<th>0 ........ 100%</th><th>pace</th><th>resets at</th><th>resets in</th><th>req 24h</th><th>tok 24h</th><th>err</th></tr>']
    for idx, a in acc.items():
        f = stat.get(idx, {})
        st = f.get("status", "?")
        if f.get("unavailable") or f.get("cooldowns"): st = "cooldown"
        if f.get("disabled"): st = "disabled"
        scls = "ok" if st == "active" else "bad"
        d = day.get(idx, (0, 0, 0))
        ls = lims.get(idx) or [("?", None, 0, 0, 0)]
        for i, (m, v, r, w, ts) in enumerate(ls):
            out.append("<tr>")
            if i == 0:
                n = len(ls)
                out.append('<td rowspan="%d"><b>%s</b></td><td rowspan="%d">%s%s</td><td rowspan="%d" class="%s">%s</td>' % (
                    n, esc(a["provider"]), n, esc(a["label"]), (" <i>(%s)</i>" % esc(a["plan"])) if a["plan"] else "", n, scls, esc(st)))
            if v is None:
                out.append('<td colspan="6"><i>no rate-limit headers seen yet &mdash; send a request through the proxy</i></td>')
            else:
                stale = r < now
                used = 0 if stale else v
                start = r - w
                frac = (now - start) / w if w else 0
                proj = ("%d%%" % min(used / frac, 999)) if (0.03 < frac < 1 and not stale) else "&mdash;"
                pcls = "bad" if proj != "&mdash;" and used / frac >= 100 else ""
                out.append('<td>%s</td><td class="r">%.0f%%</td><td class="mono">%s</td><td class="r %s">%s</td><td>%s</td><td class="r">%s</td>' % (
                    m, used, tbar(used), pcls, proj,
                    fclock(r) if not stale else "<i>window rolled over</i>",
                    fdur(r - now) if not stale else "&mdash;"))
            if i == 0:
                out.append('<td class="r" rowspan="%d">%d</td><td class="r" rowspan="%d">%s</td><td class="r %s" rowspan="%d">%d</td>' % (
                    len(ls), d[0], len(ls), fnum(d[1] or 0), "bad" if d[2] else "", len(ls), d[2] or 0))
            out.append("</tr>")
    out.append("</table>")
    notes = []
    for f in STATE["auths"]:
        who = esc((f.get("email") or f.get("label") or "?").split("@")[0])
        s = (f.get("quota") or {}).get("signals") or {}
        if f.get("provider") == "codex" and s.get("X-Codex-Credits-Balance"):
            notes.append("codex %s credits: <b>%s</b>%s" % (who, fnum(float(s["X-Codex-Credits-Balance"])),
                         " (unlimited)" if s.get("X-Codex-Credits-Unlimited") == "True" else ""))
        if f.get("provider") == "claude" and s.get("Anthropic-Ratelimit-Unified-Overage-Status"):
            notes.append("claude overage: <b>%s</b> (%s)" % (esc(s["Anthropic-Ratelimit-Unified-Overage-Status"]),
                         esc(s.get("Anthropic-Ratelimit-Unified-Overage-Disabled-Reason", "-"))))
        if f.get("provider") == "claude" and s.get("Anthropic-Ratelimit-Unified-Representative-Claim"):
            notes.append("claude %s binding limit: <b>%s</b>" % (who, esc(s["Anthropic-Ratelimit-Unified-Representative-Claim"])))
        if (f.get("id_token") or {}).get("chatgpt_subscription_active_until"):
            notes.append("chatgpt %s renews/ends: <b>%s</b>" % (who, esc(f["id_token"]["chatgpt_subscription_active_until"][:10])))
    out.append('<p class="note">pace = projected use at window end if burn rate holds. Limit data is observed from upstream '
               'response headers, refreshed on every proxied request (last seen: %s).<br>%s</p>' % (
                   fclock(max([l[5] for l in latest_limits()] or [now]), False), " &middot; ".join(dict.fromkeys(notes))))
    return "".join(out)

def reset_timeline(now):
    acc = accounts()
    horizon = 7 * 86400 + 3600
    lims = [l for l in latest_limits() if l[3] > now]
    rows = len(lims) or 1
    h = 56 + rows * 22
    p = Plot(900, h, "set xrange [now:now+7d]  # upcoming limit resets", (now, now + horizon), (0, rows),
             [(now + i * 86400, "+%dd" % i if i else "now") for i in range(8)], yfmt=lambda v: "")
    p.parts = [x for x in p.parts if "<text" not in x or "tl" not in x or "text-anchor=\"middle\"" in x]
    for i, (a, m, v, r, w, ts) in enumerate(lims):
        y = p.t + 20 + i * 22
        x0, x1 = p.x(max(r - w, now - 1)), p.x(r)
        col = PALETTE[i % len(PALETTE)]
        p.parts.append('<rect x="%.1f" y="%d" width="%.1f" height="9" fill="%s" fill-opacity=".25" stroke="%s"/>' % (x0, y - 5, max(x1 - x0, 1), col, col))
        used_w = (x1 - x0) * min(v, 100) / 100
        p.parts.append('<rect x="%.1f" y="%d" width="%.1f" height="9" fill="%s" fill-opacity=".7"/>' % (x0, y - 5, used_w, col))
        p.parts.append('<path d="M%.1f,%d v6 M%.1f,%d v-6" stroke="#000"/>' % (x1, y - 8, x1, y + 8))
        lab = "%s %s  %.0f%%  %s" % (acc.get(a, {}).get("provider", "?"), m, v, fclock(r))
        anchor, tx = ("end", x1 - 4) if x1 - p.l > 330 else ("start", x1 + 8)
        p.parts.append('<text x="%.1f" y="%d" class="tl" text-anchor="%s">%s</text>' % (tx, y - 8, anchor, esc(lab)))
    return p.svg()

def util_plot(now, rng, metric):
    secs, _ = RANGES[rng]
    acc = accounts()
    rows = q("select auth, ts, val from lim where metric=? and ts > ? order by ts", (metric, now - secs))
    p = Plot(440, 250, "%s window utilisation, %% used [last %s]" % (metric, rng), (now - secs, now), (0, 100),
             time_ticks(now - secs, now), ylabel="% of window", yfmt=lambda v: "%d" % v)
    series = {}
    for a, ts, v in rows:
        series.setdefault(a, []).append((max(ts, now - secs), v))
    for i, (a, pts) in enumerate(sorted(series.items(), key=lambda kv: (acc.get(kv[0], {}).get("provider", ""), acc.get(kv[0], {}).get("label", "")))):
        who = acc.get(a, {})
        p.line(pts[::max(1, len(pts) // 300)] + pts[-1:], PALETTE[i % len(PALETTE)],
               "%s/%s" % (who.get("provider", "?"), who.get("label", "?").split("@")[0]), marks=False)
    if not series:
        p.parts.append('<text x="%d" y="%d" class="tl" text-anchor="middle">no %s samples yet</text>' % (p.l + p.pw // 2, p.t + p.ph // 2, metric))
    return p.svg()

def buckets_for(now, rng):
    secs, bw = RANGES[rng]
    start = (int((now - secs) // bw) + 1) * bw
    return list(range(start, int(now // bw) * bw + bw, bw)), bw, now - secs

def activity_plots(now, rng):
    bk, bw, t0 = buckets_for(now, rng)
    acc = accounts()
    rows = q("select ts, auth, provider, ttotal, tin, tout, tcache, failed from req where ts >= ?", (bk[0],))
    provs = sorted({r[2] or "?" for r in rows}) or []
    cols = {pv: PALETTE[i % len(PALETTE)] for i, pv in enumerate(provs)}
    idx = lambda ts: int((ts - bk[0]) // bw)
    reqs = {pv: [0] * len(bk) for pv in provs}
    for ts, a, pv, tt, ti, to, tc, fl in rows:
        i = idx(ts)
        if 0 <= i < len(bk): reqs[pv or "?"][i] += 1
    p1 = Plot(440, 250, "requests per %s [last %s]" % (fdur(bw) if bw >= 3600 else "%dm" % (bw // 60), rng), (t0, now), (0, max([sum(reqs[pv][i] for pv in provs) for i in range(len(bk))] + [4])),
              time_ticks(t0, now), ylabel="requests")
    p1.bars(bk, [(pv, cols[pv], reqs[pv]) for pv in provs], bw)
    ti, to, tc = ([0] * len(bk) for _ in range(3))
    for ts, a, pv, tt, tin, tout, tcache, fl in rows:
        i = idx(ts)
        if 0 <= i < len(bk): ti[i] += tin; to[i] += tout; tc[i] += tcache
    p2 = Plot(440, 250, "tokens per bucket [last %s]" % rng, (t0, now), (0, max([ti[i] + to[i] + tc[i] for i in range(len(bk))] + [100])),
              time_ticks(t0, now), ylabel="tokens", lm=52)
    p2.bars(bk, [("input", PALETTE[2], ti), ("output", PALETTE[5], to), ("cache read", PALETTE[1], tc)], bw)
    return p1.svg(), p2.svg()

def latency_plot(now, rng):
    secs, _ = RANGES[rng]
    rows = q("select ts, lat, provider from req where ts > ? and failed=0 and lat>0 order by ts", (now - secs,))
    top = max([r[1] for r in rows] + [1000]) / 1000
    p = Plot(440, 250, "latency per request, s [last %s]" % rng, (now - secs, now), (0, top), time_ticks(now - secs, now), ylabel="seconds", yfmt=lambda v: "%g" % v)
    provs = sorted({r[2] for r in rows})
    for i, pv in enumerate(provs):
        col = PALETTE[i % len(PALETTE)]
        pts = [r for r in rows if r[2] == pv]
        for ts, lat, _ in pts[::max(1, len(pts) // 700)]:
            x, y = p.x(ts), p.y(lat / 1000)
            p.parts.append('<path d="M%.1f,%.1f h5 M%.1f,%.1f v5" stroke="%s"/>' % (x - 2.5, y, x, y - 2.5, col))
        p.key.append(("line", col, pv))
    return p.svg()

def heatmap(now):
    days = 7
    today0 = time.mktime(time.localtime(now)[:3] + (0, 0, 0, 0, 0, -1))
    cell = {}
    for ts, tt in q("select ts, ttotal from req where ts >= ?", (today0 - (days - 1) * 86400,)):
        lt = time.localtime(ts)
        d = int((time.mktime(lt[:3] + (0, 0, 0, 0, 0, -1)) - (today0 - (days - 1) * 86400)) // 86400 + .5)
        cell[(d, lt.tm_hour)] = cell.get((d, lt.tm_hour), 0) + 1
    mx = max(cell.values() or [1])
    w, h, l, t = 440, 250, 56, 24
    cw, ch = (w - l - 40) / 24, (h - t - 30) / days
    o = ['<svg viewBox="0 0 %d %d" width="%d" height="%d" class="plot"><rect width="100%%" height="100%%" fill="#fff"/>' % (w, h, w, h),
         '<text x="%d" y="14" class="ti" text-anchor="middle">set pm3d map  # requests by day x hour</text>' % (w // 2 - 15)]
    for d in range(days):
        dt = today0 - (days - 1 - d) * 86400
        o.append('<text x="%d" y="%.1f" class="tl" text-anchor="end">%s</text>' % (l - 5, t + d * ch + ch / 2 + 3.5, time.strftime("%a %d", time.localtime(dt + 7200))))
        for hr in range(24):
            n = cell.get((d, hr), 0)
            o.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" fill="%s" stroke="#ddd" stroke-width=".5"><title>%s %02d:00 &#8212; %d req</title></rect>' % (
                l + hr * cw, t + d * ch, cw, ch, heat_color(n / mx) if n else "#fff", time.strftime("%a %d %b", time.localtime(dt + 7200)), hr, n))
    for hr in range(0, 24, 3):
        o.append('<text x="%.1f" y="%.1f" class="tl" text-anchor="middle">%02d</text>' % (l + hr * cw + cw / 2, t + days * ch + 13, hr))
    o.append('<rect x="%d" y="%d" width="%.1f" height="%.1f" fill="none" stroke="#000"/>' % (l, t, cw * 24, ch * days))
    for i in range(50):  # colour box
        o.append('<rect x="%d" y="%.1f" width="10" height="%.1f" fill="%s"/>' % (w - 30, t + (49 - i) * ch * days / 50, ch * days / 50 + .5, heat_color(i / 49)))
    o.append('<rect x="%d" y="%d" width="10" height="%.1f" fill="none" stroke="#000"/>' % (w - 30, t, ch * days))
    o.append('<text x="%d" y="%d" class="tl">%d</text><text x="%d" y="%.1f" class="tl">0</text></svg>' % (w - 32, t - 4, mx, w - 28, t + ch * days + 11))
    return "".join(o)

def tbl(head, rows, cls=None):
    cls = cls or []
    o = ['<table class="grid"><tr>' + "".join("<th>%s</th>" % h for h in head) + "</tr>"]
    for r in rows:
        o.append("<tr>" + "".join('<td class="%s">%s</td>' % ("r" if i in cls else "", c) for i, c in enumerate(r)) + "</tr>")
    if not rows: o.append('<tr><td colspan="%d"><i>no data in range</i></td></tr>' % len(head))
    return "".join(o) + "</table>"

def by_model(since):
    rows = q("""select provider, model, count(*), sum(tin), sum(tout), sum(tcache), sum(treason),
                avg(case when lat>0 then lat end), avg(case when ttft>0 then ttft end), sum(failed)
                from req where ts>? group by provider, model order by count(*) desc limit 25""", (since,))
    return tbl(["provider", "model", "req", "input", "output", "cache", "reason", "lat s", "ttft s", "err"],
               [[esc(r[0]), esc(r[1])] + [fnum(r[2]), fnum(r[3] or 0), fnum(r[4] or 0), fnum(r[5] or 0), fnum(r[6] or 0),
                 "%.1f" % ((r[7] or 0) / 1000), "%.2f" % ((r[8] or 0) / 1000), r[9] or 0] for r in rows], range(2, 10))

def by_client(since):
    rows = q("select ua, ip, count(*), sum(ttotal), max(ts) from req where ts>? group by ua, ip order by count(*) desc limit 12", (since,))
    return tbl(["client (user-agent)", "ip", "req", "tokens", "last seen"],
               [[esc(r[0][:44]), esc(r[1]), fnum(r[2]), fnum(r[3] or 0), fclock(r[4], False)] for r in rows], (2, 3))

def by_endpoint(since):
    rows = q("select endpoint, count(*), sum(stream), sum(failed), avg(case when lat>0 then lat end) from req where ts>? group by endpoint order by count(*) desc", (since,))
    return tbl(["endpoint", "req", "stream", "err", "avg lat s"],
               [[esc(r[0]), r[1], r[2] or 0, r[3] or 0, "%.1f" % ((r[4] or 0) / 1000)] for r in rows], (1, 2, 3, 4))

def status_hist(since):
    rows = q("select status, count(*) from req where ts>? group by status order by status", (since,))
    tot = sum(r[1] for r in rows) or 1
    return tbl(["http", "count", "share", ""], [[r[0] or "?", r[1], "%.1f%%" % (100 * r[1] / tot),
               '<span class="mono">%s</span>' % ("#" * int(40 * r[1] / tot))] for r in rows], (1, 2))

def totals(since):
    r = q("select count(*), sum(tin), sum(tout), sum(tcache), sum(failed), avg(case when lat>0 then lat end), sum(ttotal) from req where ts>?", (since,))[0]
    return r

def recent(n=18):
    acc = accounts()
    rows = q("select ts, auth, model, endpoint, status, lat, ttft, tin, tout, tcache from req order by ts desc limit ?", (n,))
    return tbl(["time", "account", "model", "endpoint", "http", "lat s", "in", "out", "cache"],
               [[fclock(r[0], False) + time.strftime(":%S", time.localtime(r[0])), esc(acc.get(r[1], {}).get("provider", "?")), esc(r[2]), esc(r[3].replace("POST ", "")), r[4],
                 "%.1f" % (r[5] / 1000), fnum(r[7]), fnum(r[8]), fnum(r[9])] for r in rows], (4, 5, 6, 7, 8))

def page(rng):
    now = time.time()
    secs, _ = RANGES[rng]
    since = now - secs
    pr, pt = activity_plots(now, rng)
    t = totals(since)
    t_all = q("select count(*), min(ts) from req")[0]
    health = "ok" if STATE["queue_ok"] else "<span class='bad'>collector error: %s</span>" % esc(STATE["err"] or "starting")
    nav = " | ".join(('<b>[%s]</b>' % r) if r == rng else '<a href="?r=%s">%s</a>' % (r, r) for r in RANGES)
    hdr = f"""
<table class="top"><tr><td class="logo"><pre>
  _   _ ___  __ _  __ _  ___
 | | | / __|/ _` |/ _` |/ _ \\
 | |_| \\__ \\ (_| | (_| |  __/
  \\__,_|___/\\__,_|\\__, |\\___|
                  |___/        </pre></td>
<td class="ttl"><h1>CLIProxyAPI usage</h1>
<p><i>subscription limits, resets and traffic for every account behind the proxy</i></p>
<p class="nav"><a href="#accounts">accounts</a> | <a href="#resets">resets</a> | <a href="#traffic">traffic</a> | <a href="#models">models</a> | <a href="#log">log</a> | <a href="/api.json">json</a></p></td>
<td class="meta">host <b>nuc</b><br>{time.strftime("%a %d %b %Y")}<br><b>{time.strftime("%H:%M:%S %Z")}</b><br>collector: {health}<br>
range: {nav}</td></tr></table><hr>"""
    summary = ('<table class="grid sum"><tr><th>requests</th><th>input tok</th><th>output tok</th><th>cache tok</th><th>total tok</th><th>errors</th><th>avg latency</th><th>recorded since</th></tr>'
               '<tr><td class="r">%s</td><td class="r">%s</td><td class="r">%s</td><td class="r">%s</td><td class="r">%s</td><td class="r">%s</td><td class="r">%.1fs</td><td class="r">%s</td></tr></table>') % (
        fnum(t[0] or 0), fnum(t[1] or 0), fnum(t[2] or 0), fnum(t[3] or 0), fnum(t[6] or 0), t[4] or 0, (t[5] or 0) / 1000,
        fclock(t_all[1]) if t_all[1] else "&mdash;")
    two = lambda a, b: '<table class="pair"><tr><td>%s</td><td>%s</td></tr></table>' % (a, b)
    body = [hdr,
        '<a name="accounts"></a>', section("1. Accounts and limits", accounts_table(now)),
        '<a name="resets"></a>', section("2. Reset schedule", reset_timeline(now)),
        '<a name="traffic"></a>', section("3. Traffic &mdash; last %s" % rng, summary + two(pr, pt) + two(latency_plot(now, rng), heatmap(now))),
        '<a name="models"></a>', section("4. Breakdown &mdash; last %s" % rng,
            two(util_plot(now, rng, "5h"), util_plot(now, rng, "7d")) +
            two('<div class="cell">' + by_endpoint(since) + "<br>" + status_hist(since) + "</div>",
                '<div class="cell">' + by_client(since) + "</div>") +
            '<div class="cell">' + by_model(since) + "</div>"),
        '<a name="log"></a>', section("5. Recent requests", recent()),
        '<hr><p class="foot">usage-dash &middot; source: CLIProxyAPI management API (usage queue, auth files) &middot; history kept in SQLite on the NUC &middot; page refreshes every 30s &middot; '
        'rendered in %.0f ms &middot; up %s</p>' % ((time.time() - now) * 1000, fdur(time.time() - STARTED))]
    return CSS_HEAD + "".join(body) + "</body></html>"

CSS_HEAD = """<!DOCTYPE html><html><head><meta charset="utf-8"><meta http-equiv="refresh" content="30">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>CLIProxyAPI usage</title>
<style>
body{background:#fff;color:#000;font:15px/1.35 "Times New Roman",Times,serif;margin:0 auto;padding:10px 14px;max-width:960px}
a{color:#0000ee}a:visited{color:#551a8b}
h1{font:bold 30px Georgia,Times,serif;margin:4px 0;color:#000080;text-align:center}
hr{border:0;border-top:2px solid #000080;margin:8px 0}
table{border-collapse:collapse}
table.top{width:100%}table.top td{vertical-align:middle}
td.logo{width:25%}td.logo pre{font:11px/1.05 monospace;color:#000080;margin:0}
td.ttl{text-align:center}td.ttl p{margin:2px 0}.nav{font-size:14px}
td.meta{width:25%;font-size:13px;text-align:right;border-left:1px dotted #000080;padding-left:8px}
.sec{border:1px solid #000;margin:14px 0;padding:0}
.st{background:#000080;color:#fff;font:bold 15px Georgia,serif;padding:2px 8px}
.sec>table,.sec>svg,.sec>p,.sec>.cell{margin:8px;}
table.grid{font-size:13px;width:calc(100% - 16px)}
table.grid th{background:#e6e6f2;border:1px solid #888;padding:2px 6px;text-align:left;font-weight:bold}
table.grid td{border:1px solid #bbb;padding:1px 6px;vertical-align:middle}
table.sum td{font:bold 16px Georgia,serif;text-align:center}
td.r{text-align:right;font-variant-numeric:tabular-nums}
.mono{font:12px monospace;white-space:pre}
.ok{color:#006400;font-weight:bold}.bad{color:#c00;font-weight:bold}
table.pair{width:100%;margin:4px 0}table.pair td{width:50%;text-align:center;vertical-align:top;padding:0}
.cell{padding:0 8px}.cell table.grid{width:100%}
svg.plot{max-width:100%;height:auto}
.plot .grid{stroke:#bbb;stroke-dasharray:1,3;stroke-width:1}
.plot .tl{font:10px Helvetica,Arial,sans-serif;fill:#000}.plot .ti{font:11px Helvetica,Arial,sans-serif;fill:#000}
.note{font-size:12.5px;color:#333}.foot{font-size:12px;color:#444;text-align:center}
</style></head><body>"""

# ------------------------------------------------------------------ server
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        u = urlparse(self.path)
        try:
            if u.path == "/healthz":
                self.send(200, "ok\n", "text/plain")
            elif u.path == "/api.json":
                now = time.time()
                data = {"now": now, "accounts": accounts(), "limits": [dict(zip(("auth", "metric", "used_pct", "reset", "window_s", "observed"), l)) for l in latest_limits()],
                        "totals_24h": totals(now - 86400)}
                self.send(200, json.dumps(data, indent=1), "application/json")
            elif u.path == "/":
                r = parse_qs(u.query).get("r", ["24h"])[0]
                self.send(200, page(r if r in RANGES else "24h"), "text/html; charset=utf-8")
            else:
                self.send(404, "not found\n", "text/plain")
        except Exception as e:
            self.send(500, "error: %s\n" % e, "text/plain")
    def send(self, code, body, ctype):
        b = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype); self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store"); self.end_headers()
        self.wfile.write(b)

def main():
    if not KEY:
        sys.exit("CPA_MGMT_KEY not set")
    threading.Thread(target=collector, daemon=True).start()
    servers = []
    for b in BIND.split(","):
        host, port = b.strip().rsplit(":", 1)
        servers.append(ThreadingHTTPServer((host, int(port)), H))
    for s in servers[1:]:
        threading.Thread(target=s.serve_forever, daemon=True).start()
    print("usage-dash listening on", BIND, flush=True)
    servers[0].serve_forever()

if __name__ == "__main__":
    main()
