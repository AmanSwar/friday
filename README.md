# Friday

A voice-first personal assistant for macOS, built around one job: keeping track of many Claude Code sessions
running in parallel without having to read them. Friday watches them, tells you out loud when one finishes, gets
stuck or needs a permission, answers "where are we at?", and lets you approve or steer them by voice.

Around that: push-to-talk questions answered in a cloned voice, conversation threads per topic, a notch "island"
UI, Slack (read-only), your own browser through a tab-group extension, and Mac control (apps, Spotify, notes).

## Pieces

| Part | What it is |
| --- | --- |
| `tools/FridayEars/` | The Mac app (Swift/SwiftUI): hold Right ⌥ to talk (Wispr Flow transcribes), the notch island and panel, the follow-up mic, and the supervisor that starts everything else. |
| `src/fridayd/` | The daemon (Python): the voice (streamed TTS, quiet mode, ducking), turns and narration, conversation threads, the Claude Code watcher, permission prompts, the live UI state. |
| `plugins/friday/` | Friday's tools for Hermes: Claude Code sessions, Slack, Signal, Brave, the Mac, notes. |
| `extensions/brave/` | A Brave/Chrome extension: Friday only sees the tabs you pin into a "Friday" tab group. |
| `bin/friday-*-hook` | Claude Code hooks (event log, permission requests, session start); `scripts/install-claude-hooks.py` installs them. |
| `tools/FridayVoiceWindows/` | A Windows tray indicator for the TTS server on a laptop GPU. |
| `config/` | Templates: Friday's persona (`SOUL.md`), Hermes config, daemon settings. |

The agent itself runs on a customized fork of [Hermes Agent](https://github.com/NousResearch/hermes-agent) (MIT),
kept private and cloned into `oss/hermes-agent`. The model is Claude (Sonnet for answers, Haiku for routing).

## Running it

This is a personal project wired to one machine: it expects macOS 14+, Wispr Flow, Claude API access, the
Hermes fork in `oss/hermes-agent`, and settings in `~/.friday`. `scripts/install.sh` sets up `~/.friday`,
`scripts/build-ears.sh` builds the app, `scripts/brave-extension.sh` prepares the extension, and
`scripts/install-claude-hooks.py` installs the hooks. `DESIGN.md` explains how everything works and why.
