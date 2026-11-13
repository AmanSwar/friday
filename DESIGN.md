# Friday

A voice-first personal assistant for Aman, built on a local fork of Hermes Agent. Its core job:
keep track of his parallel Claude Code sessions (one *master* per project, coordinating workers),
tell him out loud when something needs him, answer "where are we at", and send the next prompt.
Around that: Slack (read-only), his X timeline, finances (finance dashboards), flights, and
messaging people on Signal, all through the same agent.

## Shape

```
 Claude Code sessions (VS Code)                        Aman
   │ hooks (async): Stop, StopFailure,           types (Wispr Flow) │  ▲ hears
   │ UserPromptSubmit, Notification,                                ▼  │
   │ PreToolUse(AskUserQuestion)              ┌──────────────────────────────┐
   ▼                                          │ Hermes fork (oss/hermes-agent│
 bin/friday-cc-hook ──► ~/.friday/state/      │  branch `friday`), one gateway│
                        claude-events.jsonl   │  process, HERMES_HOME=~/.friday│
   ~/.claude/projects/*/*.jsonl  (transcripts)│  plugins/friday: tools + reply │
          │                    │              │  hook → POST /speak            │
          ▼                    ▼              └──────────────┬───────────────┘
   friday_core (stdlib)   fridayd (launchd) ◄────────────────┘ 127.0.0.1:8765
   registry, tail reader, │ watcher: hook events → judge (Sonnet 5.5, low effort)
   digest, ask/send       │ speaker: Kokoro-82M int8 → one PortAudio stream
                          │ presence: idle / lock / mic-in-use → speak, hold, or phone
```

- **`src/friday_core/`** (stdlib only; imported by both sides). `claude.py`: session discovery
  from transcripts (tail-read; masters are titled `master*`), turn origin (`human` / `peer` /
  `scheduled`), digests. `control.py`: `ask` (forked headless session) and `send` (VS Code URI).
- **`src/fridayd/`**: the always-on daemon. Owns the only voice, watches hook events, decides
  what is worth saying, knows whether Aman is at the desk.
- **`plugins/friday/`**: Hermes plugin. Tools `claude_sessions`, `claude_status`, `claude_ask`,
  `claude_send`, `friday_alerts`, `friday_hush`; `post_llm_call` hook speaks replies on local
  surfaces (cli/tui/desktop), never on Signal; `/speak on|off`, `/hush`.
- **`config/`**: Hermes config template, `SOUL.md` (persona, written for the ear), `friday.toml`.

## Facts this rests on (verified 2026-11-06)

- Transcripts: `~/.claude/projects/<slug>/<session>.jsonl`, appended live; masters reach 1.4 GB, so
  everything reads from the tail (8 MB tail parse: 34 ms; full metadata scan of 54 files: 0.9 s cold,
  1 ms warm). Claude Code re-appends `custom-title`, `agent-name`, `ai-title`, `last-prompt` every
  few dozen records, so the tail always has them. The launch folder is the first record's `cwd`.
- Every user record has `turnOrigin`: `human` (typed), `peer` (a worker's cross-session message,
  `from-name="dev3"`), or `scheduled` (loops). In master_aws only ~8% of turns are human, so alerts
  key on origin: always speak for human turns; for peer/scheduled turns only when the master needs
  Aman, is blocked, failed, or reached a goal he set.
- VS Code extension URI `vscode://anthropic.claude-code/open?session=<id>&prompt=<text>` opens that
  exact session and fills its input (`setInputText`), without submitting. Window → folder mapping comes
  from VS Code's `storage.json` (`windowsState`); `code --folder-uri` focuses it. Only the final
  Return needs Accessibility permission; without it the prompt is left typed, unsent.
- `claude -p --resume <id> --fork-session --no-session-persistence` asks a master with full context
  without touching it or leaving a session behind.
- The Claude proxy (`ANTHROPIC_BASE_URL`) only accepts `Authorization: Bearer` for streaming. Hermes
  sends `x-api-key` to third-party hosts unless the credential comes from `key_cmd`, hence
  `bin/friday-token`.
- Voice: Qwen3-TTS 0.6B Base (bf16, mlx-community) on the M3 Max GPU via mlx-audio, cloning one
  reference clip (`~/.friday/voices/friday-ref.wav`: a laid-back, friendly American woman chatting
  casually, designed with Qwen3 VoiceDesign from a casual script and picked by ear; earlier references
  sounded sighing, flat or over-excited, and a clone copies its reference's delivery, not just timbre). 2.4 GB weights, ~2.6 GB resident idle, 4.1 GB peak; RTF 0.32; first audio ~0.8 s.
  Why: VoiceDesign alone re-samples the voice every sentence (5-6 voices per reply); the 8-bit
  clone still drifted; the 1.7B bf16 clone sounded right but peaked at 6 GB. Replies are spoken as
  one streamed utterance in 3-4 large generations (first sentence alone for a fast start), not one
  generation per sentence. Also tried and rejected by ear or numbers: Piper, Kokoro, Chatterbox
  Turbo, Voxtral 4B (4.1 GB, slower start), Sesame CSM-1B (RTF 1.2), VoxCPM 1.5 (6.6 GB, no
  streaming), Pocket TTS, MOSS-TTS Nano. Qwen3 sometimes returns no audio for one-word lines; the
  engine retries.
- Push-to-talk: FridayEars (/Applications, menu bar) — hold Right Option → Wispr Flow push-to-talk
  (synthetic fn; Right Option is swallowed so Wispr sees fn alone) → transcript (Wispr's paste, or
  its "copy last text" ⌘⌃C when it shows its copy bubble) → fridayd /ask → Hermes API server
  (127.0.0.1:8652, Chat Completions streaming, X-Hermes-Session-Id "friday-voice") → speech.
  Signed with Aman's Apple Development certificate so Accessibility / Input Monitoring survive rebuilds.
- Voice location: generated on the Windows laptop's RTX 3050 (qwentts.cpp, GGML/C++ port of Qwen3-TTS,
  Q8_0 talker + BF16 codec, same reference clip): RTF 0.25-0.31, first audio ~0.1 s (0.11-0.19 s over the
  SSH tunnel), 2.7 GB VRAM; played on the Mac. FridayEars starts the laptop server and owns the tunnel
  (`scripts/laptop-voice.sh`), re-checks every 60 s, and stops both on quit. fridayd's FallbackEngine uses
  the laptop and loads the Mac MLX voice only when the laptop doesn't answer (unloaded after 10 idle min).
  Measured alternatives on the 3050: PyTorch eager RTF 2.51 (launch-bound: 295k launches for 3.3 s audio,
  GPU 24% busy), faster-qwen3-tts 0.67-1.2, vLLM-Omni 0.40-0.45 (fits in 4 GB), llama.cpp 0.66 (x-vector
  clone only); CPU never real time (best 1.45). Sustained back-to-back synthesis throttles the laptop GPU
  (~90 °C, RTF up to 1.36); conversational pacing stays ~0.3.
- Spoken text goes through a respelling table first (Aman → "Amun", SGLang → "S G Lang", ...).

## Setup

```
scripts/install.sh                         # ~/.friday config + plugin link, `friday` command, FridayEars.app
python3 scripts/install-claude-hooks.py    # adds the async hooks to ~/.claude/settings.json (--remove undoes)
open /Applications/FridayEars.app          # starts Friday (gateway + fridayd); quitting it stops both
friday                                     # text chat in a terminal; `friday chat -Q -q "..."` for one-shot
```

Secrets live in `~/.friday/.env` (mode 600). Nothing touches `~/.hermes` (Aman's own Hermes).

## Still to build

1. Signal: signal-cli linked to Aman's number; Note to Self is his phone channel to Friday,
   `SIGNAL_ALLOWED_USERS` = his number only; fridayd's away alerts go there; a confirmed
   `message_person` tool for sending to others.
2. Slack read-only via a user token (Aman is checking whether his workspace allows an app).
3. Browser skills on the real Chrome profile: X timeline digest, finance dashboards, flights.
4. Project radar: morning briefing + nudges about recently discussed projects and deadlines.
5. Voice: a global hotkey for hush; optionally move TTS to the RTX 3050 laptop (the laptop's LAN address, Windows
   + WSL, `winhome` / `wsl-box` in ~/.ssh/config) to keep the Mac GPU clean during benchmarks, with
   local fallback. Blocked on the laptop's firewall (port 22).

## Browser: Aman's own Brave, through the Friday extension

Web search (facts, news) uses Hermes's keyless web_search / web_extract. Anything that needs his
accounts or a live site (X trending and timeline, finance dashboards, flight searches, "this tab")
goes through `extensions/brave`, a Manifest V3 extension loaded unpacked in his Brave:

- The extension connects out to fridayd's WebSocket bridge (`src/fridayd/browser.py`, 127.0.0.1:8766).
  fridayd accepts only a `chrome-extension://` Origin carrying the token in
  `~/.friday/state/brave-bridge-token`; `scripts/brave-extension.sh` writes it into the extension's
  `config.js` (not committed). The Hermes tools `brave_open/read/tabs/click/close` call `POST /browser`.
- Friday's pages open as background tabs in a collapsed "Friday" group and close after 20 minutes.
  His own tabs are read-only. No typing; clicks refuse post/reply/like/follow/send/buy/sell/pay/confirm.
  Links are followed by loading their URL from the extension, because a page-started navigation can
  leave the tab unscriptable and background tabs don't paint, which stalls client-side routers. A tab
  that stops answering scripts is reloaded once.
- Scripts run with `injectImmediately`: by default Brave holds an injected script until the page is
  idle, which a live site like X never reaches (reads took 20+ s and clicks hung). "Loaded" means the
  text has stopped changing, not readyState "complete". With that, opening and reading takes 1.5–5 s.
- Several extensions can be connected at once (two profiles, a test copy). The newest gets commands,
  and none is kicked off. Kicking used to make them reconnect and ping-pong.
- The `reload` action makes the extension reload itself from disk after an update (from 1.1.0 on).
- Why not Hermes's `browser.use_real_profile`: on macOS 27 Brave's profile folder carries
  `com.apple.macl`, so only Brave can read it. Hermes reports the resulting EPERM as "profile locked".
  The extension needs no Full Disk Access and no cookie copies, and it isn't a headless browser that X
  blocks. It's turned off in the config.
- Tested in a throwaway headless Brave (`--load-extension`): opening, reading, scrolling long pages,
  links, clicks, guards, his-tab protection, error pages, and the agent choosing brave_read.

### Friday's tab group (extension v2.0.0, 13 Nov 2026)

Like Claude's extension: Friday sees only the tabs he shares. He pins Friday to a tab (toolbar icon,
Option-Shift-F, right-click "Pin Friday to this tab", or "look at this tab" = brave_focus set); the tab joins a
purple, expanded "Friday" tab group and becomes "this page" (the focus, where engage.js runs). Dragging a tab
into the group shares it; the icon on a group tab takes it out; right-click "Stop sharing with Friday"
ungroups his tabs and closes Friday's tabs he never looked at. Enforced in the extension: `tabs` lists only
group tabs, `read` refuses anything outside the group (and with nothing shared reads nothing; v1 fell back to
whatever tab he was looking at), `click` only in Friday's own group tabs. `open` puts new tabs in the group
(url, or `search` for a Google search; `show` brings it to the front). Friday's tabs he never activated close
after 30 min. fridayd's turn note lists the group: "[Aman's Friday group in Brave (the only tabs you can
see): his pinned tab …; tabs you opened there: …]". Tested through a standalone bridge: nothing shared ->
read refused; a search opened in a new group; only it listed/readable; stop sharing closed it.

## Signal: private and quiet

Someone may be in the room, and Signal is his lowest priority (it's personal). `[signal] announce`
in friday.toml: `quiet` (default) never interrupts and never speaks what anyone wrote. Only counts per
person are kept, and a briefing or welcome-back that happens anyway may add "his partner messaged", nothing
more. A Signal message alone never triggers a greeting. `off` never mentions Signal; `full` is the old
behaviour (each DM spoken). Signal alerts are also left out of friday_alerts ("what did I miss"). SOUL:
when he asks, give the gist; her exact words only if he asks for them.

## The Mac: apps, Spotify, Notes, Obsidian

`src/friday_core/mac.py`, exposed as plugin tools `mac_open`, `mac_quit`, `mac_volume`, `spotify`,
`apple_notes` and `obsidian`.
- Spotify drives the desktop app through AppleScript, so it needs no Spotify developer app and no
  Premium. "Play X" finds an open.spotify.com link with Hermes's keyless web_search (1–3 s), then plays
  its URI.
- Apple Notes is AppleScript, with the body written as HTML. Obsidian is plain files in the vaults
  listed in Obsidian's obsidian.json, so it works with the app closed. A bare note name finds the note
  anywhere in the vault. No note means today's daily note, per the vault's Daily Notes settings.
  Writes append; paths can't leave the vault.
- FridayEars' Info.plist carries NSAppleEventsUsageDescription. Without it macOS denies Automation
  without asking. Each app (Spotify, Notes, System Events) prompts once.
- Anything else: Hermes computer_use (cua-driver). Its CuaDriver.app needs Accessibility + Screen
  Recording; `friday computer-use doctor` checks it.

## What Friday is doing: island line and activity panel

fridayd records each turn in `src/fridayd/activity.py`: his words, each step, the reply text, and
whether it has been spoken. (Since 12 Nov FridayEars gets it pushed over `/ui/stream`; see the next
section.)
- Steps come from Hermes's `hermes.tool.progress` SSE events in the chat-completions stream.
  Built-in tools are labelled from the tool name and its preview ("Searching the web for …").
  Friday's own tools post a precise label to `POST /activity` ("Sending a prompt to master_aws").
  The label is posted only when Hermes's `session_id` is the voice session (`friday-voice`), so cron
  jobs never label a voice turn's steps. Signal labels name no one.
- Headline sequence: Thinking… → the running step → Thinking it over… → Writing the answer… →
  Getting ready to speak… (text done, voice not started yet) → Speaking → Done. The speaker reports
  "speaking" from the first audio, not from when a streamed answer is queued.
- "Speaking" means sound is coming out, not "an answer is open". A streamed answer stays open while
  Friday works between remarks ("Checking master_aws now." → tool → answer). The speaker reports
  `audible` off after 0.8 s of silence, so the island goes back to the step instead of freezing on the
  last caption. Each stretch of speech is a step (Said "…") in order with the tools.
- A finished sentence is spoken when the text pauses for 0.4 s. Before, a remark before a tool call
  waited for the next text to show where it ended, so it was spoken only after the tool.

## FridayEars UI, rebuilt (12 Nov 2026)

Design and reasoning: docs/ui/island-redesign.html. The old island sometimes never appeared or glitched:
it picked a screen once at launch (his EK220Q monitor sits above the laptop), was ordered out and back on
several racing timers, polled `/status` every 0.3 s with answers that could land out of order, could
freeze on "Listening" after a missed key release, and nothing restarted fridayd if it died.

- **Pushed state.** `fridayd/ui.py` (`UIHub`) rebuilds a small state 10×/s and sends it on
  `GET /ui/stream` only when it changed (one JSON object per `data:` line; `: ping` every 5 s). The
  state carries timestamps, not counters, so an idle Friday sends nothing but heartbeats. History (the
  last 50 turns and alerts, including ones from before a restart, read from alerts.jsonl / voice.jsonl)
  is its own message, rebuilt once a second. Each type has a sequence number; the app drops anything
  not newer (`Link.swift`), and polls `GET /ui` every 2 s while the stream is down.
- **One decision.** `AppModel.derive()` turns (key/mic state, server state, supervisor status, clock) into
  a `Display`, in priority: listening › transcribing › starting/offline › needs you › notice › speaking
  (lingers 1.5 s) › follow-up › heard (1 s) › working on his question › background › idle with badges.
  Nothing else shows or hides the island. With the panel open, the island expands only for the mic.
- **Windows** (`Windows.swift`). The island window is never ordered out; hidden = nothing drawn and
  click-through. It catches clicks only over its own shape (pointer checked 15×/s). Each time it opens
  from nothing it moves to the pointer's screen: notch island on the MacBook, pill under the menu bar on
  the monitor. Display changes, wake and Space switches re-place it. Idle on the notch screen it draws
  exactly the notch (invisible); clicking the notch opens the panel.
- **States** (`IslandView.swift`): listening (bars follow the real mic level, `MicMeter`), transcribing,
  heard, working (thread chip, his question, current step, step dots, timer), speaking (captions; the
  spoken part estimated at ~2.6 words/s is bright), needs you (permission card with Allow / Always /
  Deny / Later), follow-up (countdown ring), quiet captions, background (compact wings beside the notch:
  presence/phone turns and the watcher judging a master), offline, notices. A new state swaps in
  (old content leaves at once, new fades in) so two states are never drawn over each other.
- **Panel** (`PanelView.swift`): opened by clicking the island or notch, left-clicking the menu bar
  icon, or double-tapping Right ⌥. Needs you (permissions, masters waiting, held alerts with Play) ·
  Now · Masters (state pill; click opens the session in VS Code via `POST /open-session`) · Earlier
  (expandable) · a type box (`POST /ask {"typed": true}`: no spoken acknowledgment). Esc or a click in
  another app closes it (a global click monitor: macOS 14+ cooperative activation means Friday may never
  become the active app, so "resign active" can't be relied on); the pin keeps it open.
- **Quiet mode** (`POST /quiet`, persisted in state/ui.json): the speaker turns each sentence into a
  caption held ~1 s per 3 words (1.5–12 s) and opens no audio device; ducking and the follow-up window
  are skipped. Switching it on mid-sentence silences the rest of that sentence. Captions undo the
  voice respellings ("G S M eight K" → GSM8K).
- **Supervisor** (`Supervisor.swift`): checks gateway, fridayd and Signal every 2 s; restarts one that
  exited or stopped answering (startup grace 240 s, 30 s once it has answered), backoff 0/5/15/30/60 s; no
  startup cutoff. One slow health check isn't an outage (up if it answered in the last 5 s). Measured:
  fridayd killed → restarted in 1 s, island red for ~2 s, back in 10 s.
- **Missed key-up**: the first version asked macOS whether Right ⌥ was physically down (keyState / flagsState
  of the HID system state). On this Mac it reports "up" while the key is held (the tap consumes its events),
  so it cut every push-to-talk at ~2 s ("Tell me what?"). Now: any other key event with no ⌥ flag means it
  was released; plus a 2-minute cap. Wispr's history is read off the main thread.
- **Microphone** (`Mic.swift`, menu → Microphone): Friday's own listening (follow-up window, listening bars)
  uses the same mic as Wispr Flow by default (config.json: `prefs.user.overrideAudioDeviceId` → its
  `rankedAudioDevices` name, matched to a CoreAudio device: "Built-in mic (recommended)" = the MacBook mic,
  "X (Bluetooth)" = X), or a pinned mic, or the system default. Captured with AVCaptureSession
  (`MicCapture`), chosen by CoreAudio UID. The first version set the device on an AVAudioEngine input node;
  it kept reporting the old device's format and installTap threw an Objective-C exception, which aborted
  the app at every push-to-talk (12 Nov). Self-tests: `FridayEars --mic-test`, `--follow-test` (run with
  `open -n -a /Applications/FridayEars.app --args --follow-test`; results in ears.log).
- **Follow-up window, third pass (13 Nov)**: replies arrived as fragments ("In case of Anthropic" after ~30 s
  of talking) because Apple's recognizer starts its transcript over after each pause and only the last stretch
  was kept. Each stretch is now kept and the next appended (`FollowUp.startsOver`: a new stretch starts with
  other words and is shorter; a revision keeps the start). End: no new words for 2.8 s and a quiet mic for
  1.2 s, or 6 s past his last word whatever the noise; 45 s cap. Server recognition allowed (more accurate),
  punctuation on, his vocabulary as hints. Test: a sentence with 2 s and 1.5 s pauses came through whole.
- **Follow-up window** (12 Nov, second pass): up to 8 s for him to start (12 s after a permission question;
  a few seconds more if the mic hears something before words come back). Once words come, it ends after
  2.2 s with no new words (was: any 1.3 s pause, which cut "…also I have like"), then waits up to 1 s for
  the recognizer's final text; 45 s cap. A level-based end was tried and never closed with YouTube
  playing. Opens 0.6 s after Friday stops (a Bluetooth speaker lags). What he says goes to
  `/ask {"reply": true}`, and the router keeps it in the current thread (one went to a new "Son
  conversation" thread before). The router also stays put within 60 s of a reply that ended in a question.
- **Media down while the mic is open**: FridayEars posts `/mic {"open"}` during push-to-talk and the
  follow-up window; the ducker treats it like Friday speaking (expires after 70 s if the app dies).
- **Narration** (narration.py, rewritten 12 Nov): steps are said with their specifics after 1.2 s
  ("Asking the AWS master directly.", "Searching for vLLM KV cache paging.", "Reading plan.md."). When
  nothing new has happened for 6 s (at most every 10 s), she says where things are: (1) her own thinking
  (Hermes streams `delta.reasoning_content`; conversation.py passes it on) condensed by Haiku into one
  line, ~1.5 s, never on turns that used Signal / notes / health tools; (2) the step still running ("The
  SGLang master's still answering."); (3) what just came back ("Got the AWS master's update. Putting it
  together."); (4) a bare "still on it" only after 14 s with nothing else. Lines never repeat in a turn.
- **Which screen**: the MacBook notch only (his call, 12 Nov); the pointer-following option is gone. With
  the lid closed it falls back to the menu-bar screen as a pill.
- **Your turn**: the follow-up state outranks the caption that lingers after she stops, a soft Tink plays
  as the mic opens, and the island reads "YOUR TURN · Go ahead, I'm listening" with a countdown that
  disappears once he's talking.
  far away, and opening it drops the speaker to 16 kHz mono headset mode. That's why the follow-up window
  heard nothing.
- **Output device** (speech.py): PortAudio reads the device list once at start, so a Bluetooth speaker that
  connected later never got Friday's voice. Before each utterance the speaker compares macOS's default
  output (CoreAudio `dOut`) with the one it started with and re-initializes PortAudio when it changed; an
  open that fails reloads and retries once.
- **Quit** (fixed twice, 12–13 Nov): in this app `NSApp.terminate` exits without asking the delegate
  (a minimal app built the same way does ask, so something in FridayEars causes it; not pinned down). So
  quitting from the menu left the gateway, fridayd, Signal and the laptop voice running. No path relies on it
  now: the menu's Quit runs Friday's own shutdown and exits; a quit Apple event (logout, restart, `quit app`)
  does the same; SIGTERM / SIGINT / SIGHUP too; `applicationWillTerminate` stops everything if AppKit ends the
  app anyway. Force quit and crashes can't be caught, so fridayd watches its parent: when FridayEars is gone
  (fridayd reparented to launchd) it stops the gateway, Signal, the laptop voice and its tunnel, and exits.
  `FRIDAY_SUPERVISE=0` runs the app without its processes, for testing these.
- Measured idle CPU: FridayEars 0.1–0.8 %, fridayd 0.6 %.

## Laptop voice lifecycle (fixed 8 Nov 2026)

Opening FridayEars starts the TTS server on the laptop and the SSH tunnel, waiting up to 40 s. The
voice runs there, and the Mac voice loads only if the laptop doesn't answer; it's rechecked every
60 s. Quitting stops the server (freeing ~2.4 GB of GPU memory) and the tunnel. Two bugs had kept all
of this from working:
- FridayEars ran its helpers with a clean environment without SSH_AUTH_SOCK. The laptop key lives in
  the macOS agent, so every `ssh wsl-box` was refused: the server never started, quitting never
  stopped it, and every reply rendered on the Mac's GPU.
- laptop-voice.sh checked "already running?" with `pgrep -f friday_tts_server.py`, which matched its
  own ssh command line. It now asks the port (`curl /health`). Stop uses the `[f]riday` pattern.
- bin/friday: Hermes's `activate` re-checks dependencies against the network on every start. On
  8 Nov 2026 a GitHub 504 (kittentts) made it abort, and the gateway didn't start at all. On failure the
  launcher now builds the environment from what's installed (`hermes_compose_env`, no network) and
  carries on; the error goes to ~/.friday/logs/activate.err.

### Permission prompts: questions, and prompts answered in Claude Code (12 Nov)

- Claude Code also sends AskUserQuestion through the PermissionRequest hook. It isn't allow/deny (it's a
  question with options), but Friday showed it as "wants to Use AskUserQuestion" with the raw dict. The hook
  and fridayd now pass it straight back (no output), so Claude Code's own question UI shows; the watcher's
  "X is asking you: …" alert still covers it.
- A prompt he answered in VS Code stayed on the island until the 110 s timeout. `Permissions.ask` now polls
  every 0.5 s and clears the prompt when the session's transcript is written after it (+2 s: the session
  moved on, so it was settled there) or when the hook's connection closes (Claude Code ended the hook).

### Laptop voice down all day on 12 Nov, and the Windows indicator

- Cause: WSL had restarted (laptop reboot or sleep) and nothing started its SSH server: WSL here runs
  without systemd and has no /etc/wsl.conf, so `ssh wsl-box` (Windows portproxy 2222 -> WSL) was reset and
  FridayEars couldn't start the server; every line used the Mac voice. Started sshd by hand
  (`ssh winhome 'wsl -d Ubuntu -u root -e /usr/sbin/service ssh start'`). Making it survive reboots is
  his call (an auto-start), not done yet.
- `friday_tts_server.py` (on the laptop, backup `.bak-20261112`) gained `GET /stats`: generating now, lines
  served, audio seconds, last line's time to first audio and real-time factor. Never the text.
- Friday Voice (tools/FridayVoiceWindows, copied by scripts/laptop-tray.sh to C:\Users\asus\FridayVoice):
  a PowerShell tray app. Icon: grey off, green ready, pulsing while speaking, red when WSL's SSH is down.
  A card bottom-right: status, GPU load graph (last minute), VRAM, temperature, power (Windows
  nvidia-smi), and the last line's speed. While it runs it keeps WSL awake and sshd up. Measured during a
  line: GPU 93 %, 2.7 / 4.0 GB, 69 W, 90 ms to first sound, 4.2x real time. Files go over as UTF-8 with a
  BOM (PowerShell 5 reads BOM-less files as ANSI). `-Snapshot x.png` renders the card over SSH.

## Signal Note to Self (8 Nov 2026)

Aman doesn't want Note to Self messages from scheduled jobs. The "Tease his partner" cron prompt no longer
sends him anything: notes like "DECISION:" (she wants something bought), "NO" (take over) and file
paths go in the run's final response, kept in Hermes's cron run history on the Mac. signal_send also
refuses Note to Self ("me", his own number) from any session other than `friday-voice`, so a drifting
prompt can't bring it back. Phone-channel replies (fridayd → signal_api.send) are unaffected.

## Spend controls (9 Nov 2026)

Measured over 6–8 Nov: 1,006 Sonnet 5.5 calls, 52.6M prompt tokens (79% cache reads, 21% writes), about
$16.5/day at list price. 72% of that was cache writes: the voice session's ~160K-token context was
re-written whenever turns came more than 5 minutes apart.
- `compression.prepare_ahead: true`: the first 60K compaction blocked a voice turn for 2 minutes (the
  summary is a ~60–70 s Sonnet call). With this, the summary is written in the background once the
  prompt is within 12% of the cap, and the turn that crosses it uses that summary.
- `compression.threshold_tokens: 60000`: conversations are summarised past 60K tokens. On the first turn
  the voice session went from 179 to 117 messages (~45K tokens); calls went from ~161K to ~70K prompt tokens.
- `prompt_caching.session_ttl: {friday-voice: 1h}`: a small fork change, `request_cache_ttl` in
  agent/prompt_caching.py, used by conversation_loop. It's resolved per request from the agent's current
  session id: the first version ran at agent init and never reached the wire, because the gateway reuses
  and rebinds agents. Proof: a turn 401 s after the last write re-wrote all 42,971 tokens. The log says
  "prompt cache TTL 1h for session friday-voice" when it applies. `auto` keys on the session source and
  treats every API-server session as machine-paced (5m). Cron and other API sessions stay on 5m; 1h
  writes cost 2x and only pay off across gaps of 5–60 minutes.
- The proxy (his API proxy) and cache TTLs, measured with usage.cache_creation: a block-level
  `ttl: 1h` marker is written as 5m; a marker on a system block caches nothing; the request-level
  `cache_control` (automatic caching) keeps its TTL (`ephemeral_1h_input_tokens`). So `prompt_caching.top_level:
  true` (fork: `top_level_cache_marker`) sends one request-level 1h marker for 1h requests and strips the
  block markers, because a 5m entry may not precede a 1h one. Before this, every voice turn more than
  5 minutes after the last re-wrote the whole prefix, even with the 1h setting logged.
- `agent.reasoning_effort: high`: was unset, so no effort was sent and the API default applied (high on
  Sonnet 5.5). Now explicit: adaptive thinking, effort high.
- The "Tease his partner" cron job (about 92 runs a day, ~73K tokens each) is deleted.
- Hermes keeps a session's system prompt for cache stability, so SOUL.md edits reach a running session
  only after it's rebuilt (compaction does this; `hermes sessions refresh-prompts` forces it).

## Feeling alive: acknowledgments, narration, help offers, Friday's tab (9 Nov 2026)

- Acknowledgment (`src/fridayd/narration.py`): when push-to-talk ends, fridayd says a short line that
  fits the request ("Let me look that up.", "Checking on the masters.", "Let me look at your tab.").
  It's spoken about 0.5 s later, because Claude's own first words take 5–20 s. It replaced the tick
  sound. `[voice] acknowledge = "voice" | "tone" | "off"`.
- Narration: a step still running after 1.5 s gets a short spoken line ("Searching for X trending
  today.", "Asking the AWS master directly. This takes a bit."). At most 4 lines per turn, never within
  3.5 s of anything else said, never once the answer is flowing or mid-sentence. "Still on it" after 12 s
  and 30 s of silence. Signal and bookkeeping tools are never narrated. All of it is fed into the turn's own
  speech stream (the speaker plays one utterance at a time) and kept out of the reply text.
  `[voice] narrate = true`.
- Friday's tab (extension 1.4): Aman points Friday at the tab he's working on: the toolbar icon (badge ★),
  right-click "Make this Friday's tab", Option-Shift-F, or by voice (brave_focus). brave_read with no tab id
  reads it, each voice turn starts with "[Aman's focus tab in Brave: ...]", and Friday lists other tabs
  only when asked.
- Help offers: engage.js runs only in Friday's tab and reports counts (active reading time, scroll-backs,
  selections, copies, text length; no page text). About 4 minutes of active reading on a text-heavy page,
  plus 3 or more scroll-backs or 2 or more selections or copies, sends `deep_read` to fridayd. fridayd
  offers help as a `[browser]` turn, only when he's at the desk, not on a call, and Friday is idle;
  once per page per day, at most every 20 minutes (`[proactive] browser_help`). Video, social and chat
  sites are excluded.
- Camera / spatial (later): local detectors emit events (Aman at the desk, someone else here, on the phone);
  a vision model looks at a single frame only when an event needs describing. Recognize Aman only; for
  others just "someone else is here", which also feeds the Signal privacy rule.

## Ducking other audio while Friday talks (9 Nov 2026)

`src/fridayd/ducking.py`. While an utterance is being delivered (`speaker.speaking`), including the quiet
stretches between Friday's remarks, Spotify and Apple Music drop to `[ducking] level` (10) via AppleScript
`sound volume`, with a two-step fade. Media in Brave tabs that are making sound go to 10% of their volume
through the extension (`duck` / `unduck`, extension 1.5). Volumes come back 1.2 s after she stops, unless
Aman changed one himself meanwhile. Saved volumes live in ~/.friday/state/ducked.json and are restored at
the next start and on /quit, so a crash never leaves his music at 10%. macOS has no public per-app volume,
so other apps can't be ducked without an audio driver. The first duck asks once for "FridayEars wants
to control Spotify".

## Threads (branches), 11 Nov 2026

The plan and the reasoning behind the cache layout are in docs/branches-plan.md. In short:
- Each topic is its own Hermes session ("friday-b-<slug>-<hex>"; General keeps "friday-voice"), so topics
  never share a context. The registry is ~/.friday/state/branches.json (`src/fridayd/branches.py`).
- The router (`src/fridayd/router.py`) never answers. Rules place most turns instantly: "new topic",
  "back to the X thread", a thread picked in the panel, one-offs (music, time) → Quick (reset daily),
  "this page" → the focus tab's thread, a Claude Code project named together with a work cue → its
  project thread, follow-ups within 2 minutes stay. The rest go to Claude Haiku 5.5 (structured output,
  thinking off, warmed at start; ~1.8 s through the proxy, while the acknowledgment plays; 4 s timeout →
  stay). Switches are announced ("Back in the AWS thread.").
- Cache invariants: append-only threads (the thread line, notes and focus tab ride in the new user
  message), one identical tool list everywhere, persona refresh only on deliberate changes, request-level
  1h marker for every friday-b-* session (`prompt_caching.session_ttl` glob, fork change).
- Master alerts are queued as notes on the project's thread and prepended to its next turn. Help offers
  go to the focus tab's thread. Cross-project questions fan out to the project threads (staggered by first
  token so they read the cache) and are answered in General.
- `scripts/cache_report.py`: spend and cache writes per thread and per cron job, each write labelled
  (new, expired, compaction, churn, growth).

### Pinning a tab starts its thread (13 Nov 2026)

Pinning Friday to a tab didn't move the conversation: the next turn stayed in whatever thread was current,
because only phrases like "this page" routed to the tab's thread. Now the router's first check after explicit
"new topic" / "switch to X": a tab pinned since his last turn (the extension's focus timestamp) sends that turn
to the tab's thread (found by URL, else created: "Starting the <title> thread."), once per pin. It beats the
panel's manual hold and the follow-up window's "stay" (the pin is newer). FOCUS_REF also covers "this video /
repo / PR / issue / site", "summarize this", "explain this", "what does it say". Tested with a throwaway
registry: pin -> tab thread; follow-up stays; "explain this" from the AWS thread -> tab thread; pin then a
follow-up-window reply -> the new tab's thread.

### Pinning a tab makes Friday speak first (13 Nov 2026, extension 2.1.0)

The extension marks why the focus changed (`by: "pin"` when he pinned it, `"moved"` when the pinned tab
closed or left the group). On a pin, fridayd (`tab_pinned`) waits 2 s (pinning several tabs gives one offer,
for the last), skips it if he's on a call, Friday is mid-turn or speaking, or it already offered on that page in
the last 10 min, then runs an "offer" turn in the tab's thread (the router's `hint.tab`: found by URL or
created): read the page, say in a sentence or two what it is and ask what he wants, with one or two options
that fit; if the thread has history, pick up from there instead of re-introducing it; private pages (email,
chats, banking) only by kind. FridayEars opens the follow-up window after "offer" turns (also the "you look
stuck" help offer, now kind "offer"), so he can just answer.

### Bug sweep, 13 Nov 2026 (after "it hears itself" and the glitching)

Root cause of the glitch storm (log 17:16-17:18): Friday's own `claude -p --fork-session` (claude_ask) fired his
PermissionRequest hook; /permission set "listen" for 120 s for any request; FridayEars then opened the 12 s
follow-up mic every time she paused between narration lines; the loudspeaker's "Almost there, I think" came
back as his reply and cancelled his real question; the reply went to the wrong thread (current was only set when
a turn finished). Fixed, plus a three-way review (app; voice pipeline; hooks, session tools, extension):

- Follow-up mic: opens only when she's completely done (not talking, nothing pending, turn over) or right after
  she asked a permission question out loud; never after Stop; closes when she starts talking (his words so far
  are sent) or when the permission is answered elsewhere; Narrator holds while his mic is open; echo filter
  (her last 20 s, word runs in order, "OK"="okay"); short first stretches kept.
- Hooks: FRIDAY_INTERNAL=1 on Friday's own claude runs, every hook exits on it (Claude Code passes its env).
  claude_ask forks run `--permission-mode plan --max-turns 3` from the project folder.
- Permissions: spoken answers only when she asked out loud, only whole short answers (no "OK so what's…", no
  "always" inside a sentence), and they settle the request she asked about last; "answered in Claude Code"
  needs a real user/assistant record after the prompt (not a worker's queued message); random ids (old phone
  buttons); no duplicate "needs your permission" alert while she's asking; dispatch names only on startup in the
  exact folder; claude_send / dispatch press Return only if the focused box holds the prompt, window by launch
  folder first.
- Security: fridayd's HTTP port refuses anything a web page could send (Origin header, non-JSON POST, foreign
  Host): verified 403 for each, 200 for Friday's own calls.
- Turns: per-turn cancel tokens; only his turns and Stop cancel; Hermes keepalives let a cancelled turn close
  its stream (which interrupts the agent); busy = turns in flight; cut-off replies (IncompleteRead) handled.
  Push-to-talk only hushes her voice (/hush); Stop keeps queued alerts (held) and drops stale permission lines.
  Router: replies honor explicit cues, follow-ups after a one-off stay with it, a cancelled turn gives the
  thread back. Activity: Stop marks only an unfinished or still-playing turn as stopped.
- App: no follow-up after Stop; panel/focus only restored after push-to-talk; Wispr not recording -> give up
  in 3 s (not 25 s holding focus); quit closes fn/mic/follow-up; timers in common modes (menus open); no crash
  without a screen; mic handler set on its queue; supervisor stop waits for its loop and doesn't block main.
- Also: Spotify volume no longer drifts down a step per duck; a recap that couldn't be spoken keeps its alerts;
  a dropped UI subscriber's stream ends (the app reconnects); a failed output device stops synthesis; presence
  loop survives errors. Extension 2.2.0: bookkeeping in storage.local (survives reloads), all changes serialized,
  `focus set` only for the active tab or one already shared.

## Awareness, voice approvals, dispatch, memory, follow-up, phone and health (11 Nov 2026)

- Signals instead of polling (`watcher.py` TranscriptTail): masters active in the last 3 days plus their
  workers (dev*/research*/worker* sessions launched from a master's folder, active in the last day).
  Worker failures in tool output (Traceback, out of memory, NO-RESULT, Killed, ...) become notes on the
  project's thread and go into the master's next turn check. A session silent for `[alerts] stall_s`
  (20 min) mid tool call or mid model call gets one "stuck or waiting?" check (Judge.stalled). Spoken only if
  stuck. The 20-minute "Watch master_aws for blockers" cron (~40% of spend) is paused.
- Focus: FridayEars reports keys per minute (counted, never recorded). At `[presence] focus_keys_per_min`
  (80) or more, routine alerts wait for a pause (at most 10 min); urgent ones still speak.
- Voice approval: bin/friday-permission-hook (PermissionRequest, 120 s) asks fridayd (`/permission`,
  `permissions.py`). For a supervised session (a master or its worker), Friday says what it wants (from Bash's
  own description, never the raw symbols), the panel shows the command with Allow / Always / Deny, the
  follow-up window listens, and "yes" / "no" / "always" settle it with no model call. Away: ntfy notification
  with buttons. No answer, or an unsupervised session (answered in ~0.4 s): no output, so the normal VS Code
  dialog shows, and clicking there always works.
- Dispatch: `claude_dispatch(folder, prompt, name)` resolves the folder under ~/code, focuses or opens its
  VS Code window, opens a new Claude Code tab with the prompt (URI), and presses Return. bin/friday-session-hook
  (SessionStart) names the new session; then `claude_send` drives it. Hooks: scripts/install-claude-hooks.py
  (Aman runs it).
- Memory: ~/.friday/memories is a symlink to ~/code/personal/friday/ (MEMORY.md = Friday's notes, USER.md = who
  Aman is, contacts/). Hermes's memory tool and background review maintain them (limits raised to 4000 / 3500
  chars). His other areas in ~/code/personal are read-only through `personal_notes` (search uses section and
  parent-bullet context).
- Follow-up window (FridayEars `FollowUp`): after Friday answers a push-to-talk turn (speech source voice:N)
  or asks something that needs an answer (`listen` in /status), Apple's recognizer (on-device when available)
  listens 6 s (10 s for permissions), ends at a 1.3 s pause, and submits like push-to-talk. Right Option cancels it.
- Phone and Watch (`phone.py`, scripts/phone-setup.py): ntfy topics in [phone]. Out: away alerts and permission
  prompts with buttons. In: the reply topic carries button answers, questions (answered as notifications), and
  "health {...}" summaries from an iPhone Shortcut (~/.friday/state/health.jsonl, `health_recent`, morning briefing).
  `[phone] enabled = false` keeps the topics but sends and follows nothing (set while the iPhone side is on hold;
  phone-setup.py turns it back on).
