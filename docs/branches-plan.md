# Friday branches: plan (11 Nov 2026)

> **Status (11 Nov 2026):** Phases 1 and 2 are built and tested live; Phase 3 waits on measurements and the
> proxy question. Code: `src/fridayd/branches.py`, `src/fridayd/router.py`, conversation.py (per-turn
> session, thread line, fan-out), `scripts/cache_report.py`, fork change `request_cache_ttl` glob, FridayEars
> island and panel. Live: "what's up with master_aws" opened the AWS thread (27K prompt tokens per call
> vs 55K in the single conversation); its follow-up stayed; "compare SGLang and AWS" asked both threads,
> staggered, then answered in General.

A tree of conversations. The root (fridayd plus a fast router) never answers. It sends each voice turn
to a branch: a persistent Hermes session for one topic. Branches persist, compact on their own, keep
their own 1h cache, and share Aman's memory, the branch index and session_search.

## What we're optimizing

Cost per answered turn, with latency second. Not cache hit rate: hit% = reads / (reads + writes), and
small, focused contexts lower both. What matters is **writes**: at the 1h TTL a written token costs
2x input ($4/MTok on Sonnet 5.5), a read 0.1x ($0.20/MTok). One rewritten token costs as much as 20 reads.

Measured baseline (voice session, after the 9 Nov fixes): ~55K prompt tokens per call, ~2.5 calls per
turn, 99-100% reads within a turn and across gaps under 1h. That's about $0.04 per turn at list price
(it was ~$0.33 per turn before the 60K cap and the 1h fix).

The fixed prefix every session carries: ~20K tokens. Tools are ~14.5K, the system prompt ~5.2K (SOUL,
memory snapshot, and "Conversation started: <date>" at the end).

## Where cache writes come from, and how branches handle each

| Cause | Today | With branches |
|---|---|---|
| Normal growth (new turn, tool results) | written once, then read | same; unavoidable and small (1-5K per call) |
| New conversation | rare (one session) | every new branch writes ~20K fixed + its first turn. Phase 2 shares the 20K across branches (below) |
| Idle > 1h | rewrite of the whole context (9% of gaps) | rewrite of that branch only (20K + its history, usually 5-25K, not 55K) |
| Compaction | rewrite after each summary (~every few heavy turns at 60K) | per branch, on a smaller context, so less often and smaller |
| Prefix churn (persona/memory refresh, tool list change) | full rewrite | same per touched branch; refresh only on SOUL changes, keep the tool list identical in every branch |
| Proxy dropping TTLs | fixed 9 Nov (request-level 1h marker) | same marker for every branch session |

### Cache rules every branch follows (engineering invariants)

1. Append-only. Nothing earlier in a branch is edited: alerts for a project, the focus-tab line, the
   branch index and pending notes all go into the **new user message**, never into the system prompt or
   earlier messages.
2. One identical tool list in every branch (same tools, same order, same descriptions). Branch-specific
   tool sets would make the fixed prefix diverge and kill sharing. Measure eager vs. deferred (below).
3. The system prompt stays per-session and frozen. Rebuild only on deliberate persona changes
   (`hermes sessions refresh-prompts`), never for routine state.
4. Every branch uses the 1h request-level marker (`prompt_caching.session_ttl`: glob `friday-b-*`, fork
   change: exact match today).
5. Fan-out across branches is staggered: send the first request, wait for its first streamed token,
   then the rest. A cache entry is readable only once its writer starts streaming.
6. Long tool loops stay within the 20-position lookback (Hermes's per-call request already moves the
   breakpoint each call; nothing to do unless a turn makes 20+ parallel calls).

### Sharing the 20K fixed prefix across branches (Phase 2, depends on the proxy)

A cache entry exists only where a breakpoint was written, and the request-level marker always lands on
the last block, so no entry ever ends exactly at "tools + system". Two ways to get one:
- **Proxy passes block-level TTLs** (best). Then Hermes's own breakpoint after the system prompt (1h) plus the
  request-level marker share the prefix automatically. **Open question: is the API proxy yours?**
  Today it writes block-level 1h markers as 5m and drops system-block markers.
- **No proxy change:** every branch starts with the same fixed opener message, and fridayd pre-warms
  "tools + system + opener" once a day (and after persona/memory changes) with a `max_tokens: 0`
  request. New branches then read 20K instead of writing it. Saves ~$0.07 per new branch.

### Keeping the active branches warm (Phase 2, if the data says so)

Only 9% of turn gaps exceed 1h today. For the 1-2 most recently used branches, while Aman is at the
desk, a pre-warm read every ~55 minutes costs about 0.1x of the branch context ($0.001-0.01) vs a
rewrite at 2x. Needs a Hermes fork endpoint that replays a session's exact request bytes with
`max_tokens: 0` (`POST /api/sessions/{id}/prewarm`). Build it only if the cache report shows
>1h-gap rewrites are a real share of spend.

### Shrinking the fixed prefix (measure first)

Tools are ~14.5K of every call's prompt. Candidates to defer (`tools.tool_search.defer`): image
generation, TTS tool (fridayd speaks), kanban, connections, rarely used desktop helpers. Candidate to make
**eager**: Friday's own tools (claude_*, brave_*, slack_*, mac/spotify/notes). Today they're
deferred, so each new session spends 2 extra model calls (tool_search, tool_describe, ~3-6 s) before
first use, and with branches every branch would repeat that. Each eager schema costs a cached read per
call; each extra call re-reads the whole context. Decide by measurement.

## Feature design

### Branches
- Kinds: **project** (one per Claude Code master, auto-created: "AWS", "SGLang"), **tab** (keyed by the
  focus tab's URL: brainstorm/learning on a doc, survives days), **topic** (anything else: reviewing an
  X post, a concept he's learning, planning), **personal** (kept apart from work), **quick** (one-shots:
  music, volume, time, notes; reset daily, never mixed into others), **general** (today's friday-voice
  becomes this).
- Registry in fridayd (`~/.friday/state/branches.json`): id, title, kind, Hermes session id
  (`friday-b-<slug>-<n>`), created, last_used, one-line summary, links (master session id, tab URL),
  status (active/archived).
- Lifecycle: created by the router (announced: "New thread for your X post."), titled in Hermes
  (PATCH /api/sessions/{id}), archived after 7 days idle (summary line kept in the index; full history
  stays searchable with session_search), reopened on mention.

### The root (router)
- Rules first, no model call:
  - focus tab set → its tab branch;
  - explicit cues ("new topic", "let's talk about X", "back to the Trainium thing", "in the AWS thread");
  - a master or project named → that project branch;
  - quick intents (play/pause/volume/what time) → quick;
  - a follow-up within ~2 min of the last turn with no new topic → stay.
- Otherwise a classifier call: Claude Haiku 5.5, effort low, JSON schema
  `{action: stay|switch|new|fanout, branch_id?, new: {title, kind}?, branch_ids?}`. Input: static
  instructions (cached), the branch index (titles, kinds, one-line summaries, last used), the current
  branch and the last 2 exchanges, the utterance. Runs while the acknowledgment plays; 2.5 s timeout
  falls back to "stay".
- Fan-out ("compare what AWS and SGLang are doing"): ask both branches (staggered), then one short
  synthesis in the current branch.
- Misrouting guards: stickiness by default, switches announced, "no, this is about X" moves the turn
  (re-asks it in the right branch).

### Shared across branches
- Hermes memory (USER.md / MEMORY.md, small, frozen per session; branches ending lets it refresh).
- session_search (all branches' histories, FTS, no model calls).
- The branch index line in each user message ("Other threads: AWS (2h ago), Trainium course (yesterday)").

### Events into branches
- Watcher alerts and Claude Code turn summaries for master X are queued as notes on X's project
  branch and prepended to that branch's next user message. The branch already knows what happened
  when he asks.
- Help offers ([browser]) go to the focus tab's branch.

### UI
- Island: branch name in the title ("AWS · 12s"). Panel: current branch, other branches with last-used,
  tap to switch. Voice: "switch to…", "new topic", "what threads do I have?".

## Engineering components

| Piece | Where | Notes |
|---|---|---|
| Branch registry | `src/fridayd/branches.py` | JSON file, lock, index rendering, archive job |
| Router | `src/fridayd/router.py` | rules + Haiku classifier (anthropic SDK, request-level 1h marker), timeout fallback |
| Per-turn session | `src/fridayd/conversation.py` | session id per turn from the router; context line = branch index + focus tab + pending notes |
| Alerts → branches | `src/fridayd/__main__.py` (deliver) | queue notes on project branches |
| Fan-out | conversation | staggered first-token start, synthesis turn |
| TTL glob | Hermes fork `request_cache_ttl` | `friday-b-*` → 1h |
| Tool eager/defer | `~/.friday/config.yaml` `tools.tool_search.defer` | after measuring |
| Pre-warm (Phase 2) | Hermes fork endpoint + fridayd scheduler | only if the report shows the need |
| Cache report | `scripts/cache_report.py` | per branch, from agent.log ("API call #N ... cache=… write=…") + state.db: reads, writes, hit%, $; writes classified as first-call / expiry (gap > 1h) / compaction / churn (unexpected write > 5K mid-session) |
| UI | FridayEars panel + island | branch title, list, switch |

## Phases

1. Branch registry, rules-only router, per-turn session ids, TTL glob, the general branch from friday-voice,
   alerts routed to project branches, cache report. Measure a few days.
2. Haiku classifier, fan-out, panel/island UI, archive job.
3. Tool eager/defer from measurements; shared-prefix (proxy change or opener + daily pre-warm); keep-warm
   pre-warm if >1h-gap rewrites matter.

## Expected effect (estimates, to be checked with the report)

- Per call: ~20K fixed + 5-25K branch history vs ~55K today, so reads drop 30-60% and rewrites after idle
  shrink the same way.
- Per new branch: +~20K write (~$0.08) until Phase 2 sharing.
- Net cost per turn: from ~$0.04 to roughly $0.025-0.03. The big drop already happened with the 60K
  cap and the 1h fix. Branches mainly buy focus and quality, plus faster first tokens on smaller contexts.
