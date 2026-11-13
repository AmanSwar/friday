"""The one model call fridayd makes: should Friday interrupt Aman about this turn, and with what words."""

from __future__ import annotations

import json
import logging
from typing import Optional

import anthropic

from friday_core.claude import Session, TurnSummary

log = logging.getLogger("fridayd.llm")

SYSTEM = """You are Friday, Aman's personal assistant. Aman is an ML systems engineer who runs several \
Claude Code "master" sessions in parallel, one per project; each master coordinates worker sessions \
(dev1, research_aws, ...) and also runs scheduled check-in prompts. You watch the masters so he doesn't \
have to. A master just finished a turn. Decide whether to interrupt him out loud, and if so, what to say.

Interrupt (speak=true) when:
- he started this turn himself (origin "human"): always. Tell him the outcome.
- the master needs him: a decision, an approval, an answer, credentials, or anything it says only Aman can do.
- something he would want to know now failed or is blocked: a crash, a dead machine, a failed run that stops progress.
- a goal he set is now done (a long run finished, a fix landed, a result is in).

Stay quiet (speak=false) for routine progress: a worker checking in, a scheduled check that found \
nothing wrong, the master dispatching work or waiting on workers.

The line is spoken aloud by a text-to-speech voice, so:
- At most two sentences, under 40 words. Lead with which master it is, said naturally \
("The AWS master...", "SGLang..."), then the specific outcome: numbers, pass or fail, what it needs.
- No markdown, no file paths, no URLs, no IDs or hashes. Say numbers the way a person would.
- If it needs something from him, end by saying exactly what.
- Your voice: Aman's frank friend who's on top of things. English only. Swearing is fine when it fits \
("fuck, the AWS box died again"), dry sarcasm now and then, never slurs. Facts first when something broke \
or needs him ("SGLang finished, all good. Nothing for you to do."). No hype, no exclamation marks."""

STALL_SYSTEM = SYSTEM.split("Interrupt (speak=true) when:")[0].replace(
    "A master just finished a turn. Decide whether to interrupt him out loud, and if so, what to say.",
    "A session (a master or one of its workers) has gone quiet in the middle of work. Decide whether it is "
    "stuck and whether to tell him.") + """Interrupt (speak=true) only when it looks stuck: a command that hung, a crashed or
dead tool, a connection that died, a loop waiting on something that will never come, or it's blocked on
Aman. Stay quiet (speak=false) when it's legitimately waiting: a long run or build with a timeout, a
scheduled wake-up or sleep, waiting on workers or on a job it started.

The line is spoken aloud: one or two sentences, under 35 words, name the session naturally ("dev2 on AWS"),
say what it's stuck on and what you'd do. Frank and calm, no exclamation marks."""

SCHEMA = {
    "type": "object",
    "properties": {
        "speak": {"type": "boolean"},
        "urgency": {"type": "string", "enum": ["low", "normal", "high"]},
        "line": {"type": "string"},
        "reason": {"type": "string"},
    },
    "required": ["speak", "urgency", "line", "reason"],
    "additionalProperties": False,
}


class Judge:
    def __init__(self, model: str = "claude-sonnet-5-5", effort: str = "low"):
        self.client = anthropic.Anthropic()  # ANTHROPIC_API_KEY or ANTHROPIC_AUTH_TOKEN (+ ANTHROPIC_BASE_URL)
        self.model = model
        self.effort = effort

    def decide(self, session: Session, turn: TurnSummary, signals: Optional[list[str]] = None) -> Optional[dict]:
        tools = "\n".join(f"- {t}" for t in turn.tools[-15:]) or "(none)"
        worker_lines = "\n".join(f"- {x}" for x in (signals or [])) or "(none)"
        user = (f"Master: {session.title} (project folder: {session.project}; topic: {session.ai_title or 'unknown'})\n"
                f"Turn started by: {turn.origin}\n"
                f"Prompt that started the turn:\n{turn.prompt}\n\n"
                f"Tool calls during the turn:\n{tools}\n\n"
                f"Failures its workers hit since its last turn (from their transcripts):\n{worker_lines}\n\n"
                f"The master's final message:\n{turn.reply[-3000:] or '(no text)'}")
        return self._ask(SYSTEM, user, session.title)

    def stalled(self, who: str, master: str, minutes: int, digest: str) -> Optional[dict]:
        """A session has written nothing for a while in the middle of work: stuck, or legitimately waiting?"""
        user = (f"Session: {who} (project: {master})\nNothing written to its transcript for {minutes} minutes, while "
                f"it was in the middle of a tool call or waiting on the model.\n\nIts recent transcript:\n{digest}")
        return self._ask(STALL_SYSTEM, user, who)

    def _ask(self, system: str, user: str, title: str) -> Optional[dict]:
        try:
            resp = self.client.messages.create(
                model=self.model,
                max_tokens=1024,
                system=system,
                messages=[{"role": "user", "content": user}],
                output_config={"effort": self.effort, "format": {"type": "json_schema", "schema": SCHEMA}},
            )
        except anthropic.RateLimitError:
            log.warning("rate limited deciding on %s", title)
            return None
        except anthropic.APIStatusError as e:
            log.warning("model call failed (%s): %s", e.status_code, e.message[:200])
            return None
        except anthropic.APIConnectionError as e:
            log.warning("model unreachable: %s", e)
            return None
        if resp.stop_reason == "refusal":
            log.info("model declined to summarise %s", title)
            return None
        text = next((b.text for b in resp.content if b.type == "text"), "")
        try:
            return json.loads(text)
        except ValueError:
            log.warning("unparseable decision: %r", text[:200])
            return None


SLACK_SYSTEM = """You give Aman a spoken heads-up about new Slack messages to him (DMs and @-mentions) at work.
One or two short sentences, under 45 words, the way a frank friend would say it (plain English only, swearing
fine, never slurs): who wrote, the gist, and whether
someone needs a reply from him. Group several messages from one person. No markdown, no links, no channel IDs."""


def slack_headsup(judge: "Judge", items: list[dict]) -> Optional[str]:
    lines = "\n".join(f"- {i.get('kind')} from {i.get('from')} in {i.get('where')}: {i.get('text', '')[:400]}" for i in items[:15])
    try:
        resp = judge.client.messages.create(model=judge.model, max_tokens=400, system=SLACK_SYSTEM,
                                            output_config={"effort": "low"},
                                            messages=[{"role": "user", "content": lines}])
    except (anthropic.APIStatusError, anthropic.APIConnectionError) as e:
        log.warning("slack heads-up failed: %s", e)
        return None
    if resp.stop_reason == "refusal":
        return None
    return next((b.text for b in resp.content if b.type == "text"), None)
