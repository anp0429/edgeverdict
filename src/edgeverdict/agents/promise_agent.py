"""The promise proposer: reads a repo's docs and scripts, proposes the
promises worth executing and a bash probe for each. It only PROPOSES; the
deterministic runner in edgeverdict.promises decides every verdict.

sig EDGEVERDICT_PROMISES_V1
sig EDGEVERDICT_PROMISES_V2 (must/attempt contract, fixtures + tests in context)
sig EDGEVERDICT_PROMISES_V3 (network-aware prompt, sandbox tool inventory)
"""
from __future__ import annotations

from ..promises import RepoText, parse_promises, Promise
from ..providers import chat_completion, client_for, uses_anthropic
import json
import re


def _loads(text: str) -> dict:
    """Model JSON -> dict. Strips code fences; if the response was cut off,
    salvages every complete promise object seen so far."""
    text = re.sub(r"^\s*```(?:json)?|```\s*$", "", text.strip())
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        pass
    objs, stack, instr, esc = [], [], False, False
    for i, ch in enumerate(text):
        if instr:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                instr = False
            continue
        if ch == '"':
            instr = True
        elif ch == "{":
            stack.append(i)
        elif ch == "}" and stack:
            start = stack.pop()
            try:
                o = json.loads(text[start: i + 1])
            except json.JSONDecodeError:
                continue
            if isinstance(o, dict) and "probe" in o:
                objs.append(o)
    return {"promises": objs}

_SYSTEM = """You review a software repository by testing what it PROMISES \
about itself. You get its documentation and its scripts. Find the behavioral \
promises the docs make and write a bash probe for each that tries to break it.

A promise is a concrete, checkable claim about behavior: "safe to run twice", \
"never overwrites an existing file", "exits quietly if jq is missing", "each \
decision gets the next number", "works in a throwaway worktree so your branch \
is never touched". NOT promises: marketing, opinions, roadmap, anything that \
needs network, credentials, a paid API, or a GUI.

Pick the scenario the author most likely did not picture: running twice, two \
things happening before either finishes, empty or missing input, paths with \
spaces, an existing file in the way, a tool missing, a partial failure \
halfway. One promise, one scenario, one probe.

PROBE CONTRACT (the harness enforces it; violations are discarded):
- It is a bash script BODY. The harness prelude already ran: the cwd is \
$REPO, a private scratch copy of the repo (with its .git). $WORK is a \
writable scratch dir next to it. HOME is set and git has a user configured.
{network_rule}
- {tools_rule}
- Declare required tools first: `need jq git` (the harness reports a missing \
tool by name instead of guessing).
- Run every command of the repo under test through a harness helper:
  `must <cmd>` when the command is expected to succeed (if it fails, the \
harness records that the promise was never reached; this works inside \
$(...) and background jobs too), or
  `attempt <cmd>` when failing could itself be the behavior you observe \
(it never stops; it sets $RC, $OUT, $ERR for you to judge).
  Plain setup (mkdir, printf, git init of your scratch remote) does not need \
a helper; guard it with `|| exit 4`.
- A promise is broken ONLY by an observed value that contradicts it: two \
identical IDs, a file that changed, a count that is wrong, an exit code the \
docs rule out. A command that crashed before producing the value is NOT a \
broken promise; `must` handles that case for you. Never call `broken` just \
because a command exited non-zero, unless the promise is about that exit.
- End with exactly one outcome: `broken "<observed values>"` or `held \
"<observed values>"`. Both exit. Put the actual observed values in the \
message.
- Every probe MUST contain a `broken` path. A probe that can only pass is \
not evidence.
- INPUTS: when a script consumes a data format and the repo ships a FIXTURE \
or an EXISTING TEST that shows that format, build your input by copying or \
minimally editing that real data. Never invent a format the repo already \
shows you; a made-up input the real tool never produces proves nothing.
- Stub an external CLI the scripts call (e.g. a model CLI or `gh`) with a \
tiny fake script on PATH under $WORK/bin only when the promise is about the \
repo's own logic around it, never about that tool.
- "Two things before either finishes" usually means two steps in sequence \
with the step that would resolve them (merge, approve, cleanup) skipped. \
Run commands truly in parallel only when the promise is about parallel \
execution; otherwise incidental lock contention hides the behavior.
- Finish in under 60 seconds.

GROUNDING: "quote" must be copied VERBATIM from the doc named in "source" (a \
repo-relative path). The harness checks it character for character after \
whitespace normalization; a paraphrase is discarded.

Return ONLY JSON:
{{"promises": [{{"claim": "the promise in plain words",
  "quote": "verbatim text from the doc",
  "source": "path/to/doc.md",
  "scenario": "the situation the probe sets up, one sentence",
  "probe": "bash body"}}]}}

Propose at most {max_promises} promises, strongest first."""


_NET_OFF = ("- NO network. If the scenario needs a git remote, create a local "
            "bare repo under $WORK and add it as origin. Anything that must "
            "download (uv run --script, npx, pip install) will fail: skip "
            "promises that need it rather than probing them.")
_NET_ON = ("- Network IS available (the operator marked this repo trusted). You "
           "may install what a script declares it needs as SETUP, e.g. `uv run "
           "--script` resolving its inline deps, `pip install --user -r "
           "requirements.txt`, `npm ci`; guard setup with `|| exit 4`. Still "
           "never call external services the promise is not about, and still "
           "use a local bare repo for git remotes.")
_TOOLS = ("The sandbox image has: bash, coreutils, git, curl, jq, yq (Mike "
          "Farah v4 syntax), zip/unzip, shellcheck, python3 with pip and uv, "
          "node 22 with npm and corepack (pnpm, yarn). Anything else: declare "
          "it with `need` so a missing tool is reported by name.")


def network_mode(environ=None) -> str:
    """'all' only when the operator opened the network for every sandbox
    command; 'install' still leaves probes offline (a probe is `bash
    probe.sh`, never an install command), so it reads as off."""
    import os
    env = os.environ if environ is None else environ
    return "all" if env.get("EDGEVERDICT_SANDBOX_NETWORK", "").strip().lower() \
        == "all" else "none"


def system_prompt(max_promises: int, network: str = "none") -> str:
    return _SYSTEM.format(
        max_promises=max_promises,
        network_rule=_NET_ON if network == "all" else _NET_OFF,
        tools_rule=_TOOLS,
    )


def _user_block(text: RepoText) -> str:
    parts = ["FILE TREE:\n" + "\n".join(text.tree)]
    for rel, body in text.docs.items():
        parts.append(f"DOC {rel}:\n```\n{body}\n```")
    for rel, body in text.code.items():
        parts.append(f"FILE {rel}:\n```\n{body}\n```")
    for rel, body in text.fixtures.items():
        parts.append(f"FIXTURE {rel} (real data in a format this repo "
                     f"produces or consumes; build scenario inputs from "
                     f"it):\n```\n{body}\n```")
    for rel, body in text.tests.items():
        parts.append(f"EXISTING TEST {rel} (how the repo exercises itself; "
                     f"reuse its setup idioms):\n```\n{body}\n```")
    return "\n\n".join(parts)


class PromiseAgent:
    def __init__(self, model: str = "gpt-5.5", client=None, base_url: str = "",
                 max_promises: int = 8, log=print, network: str | None = None):
        self.network = network if network is not None else network_mode()
        self.model = model
        self._client = client
        self.base_url = base_url
        self.max_promises = max_promises
        self.log = log

    def _client_lazy(self):
        if self._client is None:
            self._client = client_for(self.model, self.base_url)
        return self._client

    def propose(self, text: RepoText) -> list[Promise]:
        system = system_prompt(self.max_promises, self.network)
        user = _user_block(text)
        client = self._client_lazy()
        if uses_anthropic(self.model):
            resp = client.messages.create(
                model=self.model, max_tokens=12000,
                system=system + "\n\nRespond with ONLY the JSON object.",
                messages=[{"role": "user", "content": user}],
            )
            content = "".join(b.text for b in resp.content
                              if getattr(b, "type", None) == "text")
        else:
            resp = chat_completion(
                client, model=self.model,
                response_format={"type": "json_object"},
                max_tokens=12000,
                messages=[{"role": "system", "content": system},
                          {"role": "user", "content": user}],
            )
            content = resp.choices[0].message.content or ""
            if not content.strip():
                reason = getattr(resp.choices[0], "finish_reason", "?")
                self.log(f"  [warn] promise proposer: empty completion "
                         f"(finish_reason={reason})")
        data = _loads(content or "{}")
        return parse_promises(data)[: self.max_promises]
