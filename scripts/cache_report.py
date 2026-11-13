#!/usr/bin/env python3
"""Friday's Claude spend and prompt-cache health, per thread, from Hermes's per-call log lines.

    scripts/cache_report.py              last 24 hours
    scripts/cache_report.py --hours 72
    scripts/cache_report.py --since 2026-11-11

Every cache write is labelled with its cause, because writes are what cost money (a 1h write is 20x a read):
  new        first call of a session in the window (a new thread, or one not seen since)
  expired1h  more than an hour since the session's previous call
  expired5m  5-60 minutes since the previous call (a session without the 1h TTL, or the TTL not working)
  compaction the session was summarised just before this call
  churn      a big write mid-session with no reason above (prefix changed: persona refresh, tool list, edits)
  growth     the normal new turn / tool results (small)
List prices: Claude Sonnet 5.5 $2 in, $10 out, $0.20 cache read, 1.25x/2x input for 5m/1h writes."""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import re
from pathlib import Path

HOME = Path.home() / ".friday"
LOG = HOME / "logs" / "agent.log"
CALL = re.compile(r"^(\S+ \S+?),\d+ INFO (?:\[([^\]]+)\] )?agent\.conversation_loop: API call #(\d+): .*?\bin=(\d+) out=(\d+)"
                  r".*?(?:cache=(\d+)/\d+ \(\d+%\))?\s*write=(\d+)")
COMPACT = re.compile(r"^(\S+ \S+?),\d+ INFO (?:\[([^\]]+)\] )?agent\.conversation_compression: context compression done")
BIG_WRITE = 5000


def branch_names() -> dict[str, str]:
    """session id -> what to call it: thread titles, and scheduled jobs grouped by job name."""
    names = {"friday-voice": "General"}
    try:
        for b in json.loads((HOME / "state" / "branches.json").read_text()).get("branches", []):
            names[b["session"]] = b["title"]
    except (OSError, ValueError, KeyError):
        pass
    try:
        import sqlite3
        db = sqlite3.connect(f"file:{HOME / 'state.db'}?mode=ro&immutable=1", uri=True)
        for sid, source, title in db.execute("SELECT id, source, title FROM sessions WHERE source IN ('cron', 'oneshot')"):
            names.setdefault(sid, f"{source}: {(title or sid).split(' · ')[0]}")
    except Exception:
        pass
    return names


def one_hour(session: str) -> bool:
    return session == "friday-voice" or session.startswith("friday-b-")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=24)
    ap.add_argument("--since", help="YYYY-MM-DD[ HH:MM]")
    args = ap.parse_args()
    start = dt.datetime.fromisoformat(args.since) if args.since else dt.datetime.now() - dt.timedelta(hours=args.hours)

    names = branch_names()
    last_call: dict[str, dt.datetime] = {}
    last_total: dict[str, int] = {}
    compacted: dict[str, dt.datetime] = {}
    per = collections.defaultdict(lambda: collections.Counter())
    causes = collections.defaultdict(lambda: collections.Counter())
    for line in LOG.read_text(errors="replace").splitlines():
        m = COMPACT.match(line)
        if m:
            compacted[m.group(2) or "-"] = dt.datetime.fromisoformat(m.group(1))
            continue
        m = CALL.match(line)
        if not m:
            continue
        ts, session = dt.datetime.fromisoformat(m.group(1)), m.group(2) or "(untagged)"
        prev = last_call.get(session)
        prev_total = last_total.get(session, 0)
        total, out, read, write = int(m.group(4)), int(m.group(5)), int(m.group(6) or 0), int(m.group(7))
        last_call[session], last_total[session] = ts, total
        if ts < start:
            continue
        p = per[session]
        p["calls"] += 1; p["prompt"] += total; p["read"] += read; p["write"] += write; p["out"] += out
        p["uncached"] += max(0, total - read - write)
        intact = read >= 0.95 * prev_total     # the whole previous prompt was read back: only new content written
        if prev is None:
            cause = "new"
        elif intact or write <= BIG_WRITE:
            cause = "growth"
        elif (ts - prev).total_seconds() > 3600:
            cause = "expired1h"
        elif (ts - prev).total_seconds() > 300:
            cause = "expired5m"
        elif compacted.get(session) and prev <= compacted[session] <= ts:
            cause = "compaction"
        else:
            cause = "churn"
        causes[session][cause] += write

    if not per:
        print(f"no API calls since {start:%Y-%m-%d %H:%M}")
        return
    def cost(s: str, p) -> float:
        wr = 4.0 if one_hour(s) else 2.5
        return (p["uncached"] * 2.0 + p["read"] * 0.2 + p["write"] * wr + p["out"] * 10.0) / 1e6
    hours = max(1e-9, (dt.datetime.now() - start).total_seconds() / 3600)
    print(f"since {start:%Y-%m-%d %H:%M} ({hours:.0f} h), list prices\n")
    groups = collections.defaultdict(lambda: [collections.Counter(), collections.Counter(), 0.0, set()])
    for s, p in per.items():
        g = groups[names.get(s, s)]
        g[0].update(p); g[1].update(causes[s]); g[2] += cost(s, p); g[3].add(s)
    print(f"{'thread / job':36} {'sess':>4} {'calls':>5} {'prompt/call':>11} {'hit%':>5} {'writes':>8}  {'$':>6}  writes by cause")
    total_cost, tot = 0.0, collections.Counter()
    for label, (p, why_c, c, sessions) in sorted(groups.items(), key=lambda kv: -kv[1][2]):
        total_cost += c; tot.update(p)
        hit = 100 * p["read"] / max(1, p["prompt"])
        why = ", ".join(f"{k} {v/1000:.0f}K" for k, v in why_c.most_common() if v)
        print(f"{label[:36]:36} {len(sessions):4} {p['calls']:5} {p['prompt']//p['calls']:11,} {hit:5.1f} {p['write']/1000:7.0f}K  {c:6.2f}  {why}")
    hit = 100 * tot["read"] / max(1, tot["prompt"])
    print(f"\n{'total':28} {tot['calls']:5} {tot['prompt']//max(1, tot['calls']):11,} {hit:5.1f} {tot['write']/1000:7.0f}K  {total_cost:6.2f}"
          f"   (~${total_cost / hours * 24:.2f}/day at this rate)")
    allc = collections.Counter()
    for c in causes.values():
        allc.update(c)
    print("writes by cause:", ", ".join(f"{k} {v/1000:.0f}K" for k, v in allc.most_common() if v))


if __name__ == "__main__":
    main()
