"""Running the user's AI agents on a ROM project.

CLI agents (Claude Code, Codex, Antigravity CLI, opencode) run headless in a private working folder, connected
to this app's MCP endpoint and allowed to use only its tools. Each session works through a few functions
and exits; the runner starts a fresh one until the user stops it or nothing is left. Models reached with
an API key (DeepSeek, OpenRouter, a local Ollama, any OpenAI-compatible endpoint) run in a small built-in
tool loop instead. Everything the agents print is parsed into a readable live feed.

The prompt goes in on stdin, never on the command line: no length limit (Windows caps a command line at 32,767
characters, and the prompt with its guides and the user's instructions can be long) and nothing in it is parsed.
"""

from __future__ import annotations

import collections
import itertools
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from ..scoring import MECHANICAL, NEAR_MISS, STALL_ATTEMPTS, stage  # noqa: F401 (the tiers' thresholds)
from ..network import Agent, NetworkError
from . import tools
from .engine import RomNetwork

WIN = sys.platform == "win32"


class Fatal(RuntimeError):
    """A helper can't work until the user changes something (shown as the helper's error, not retried)."""


@dataclass
class Kind:
    id: str
    name: str
    command: str  # executable, "" for the built-in agent
    vendor: str
    install: dict[str, str]  # os -> command
    signin: str
    docs: str


KINDS = {
    "claude": Kind("claude", "Claude Code", "claude", "Anthropic",
                   {"unix": "curl -fsSL https://claude.ai/install.sh | bash",
                    "windows": "irm https://claude.ai/install.ps1 | iex"},
                   "Open a terminal, run `claude` and sign in with /login.", "https://docs.claude.com/en/docs/claude-code"),
    "codex": Kind("codex", "Codex CLI", "codex", "OpenAI",
                  {"unix": "npm install -g @openai/codex", "windows": "npm install -g @openai/codex"},
                  "Run `codex login` once in a terminal.", "https://github.com/openai/codex"),
    "antigravity": Kind("antigravity", "Antigravity CLI", "agy", "Google",
                        {"unix": "curl -fsSL https://antigravity.google/cli/install.sh | bash",
                         "windows": "irm https://antigravity.google/cli/install.ps1 | iex"},
                        "Open a terminal and run `agy` once to sign in.", "https://antigravity.google/docs/cli"),
    "opencode": Kind("opencode", "opencode", "opencode", "any provider (DeepSeek, Qwen, Kimi, ...)",
                     {"unix": "curl -fsSL https://opencode.ai/install | bash", "windows": "npm install -g opencode-ai"},
                     "Run `opencode auth login` once to add a provider (DeepSeek, OpenRouter, ...).", "https://opencode.ai"),
    "api": Kind("api", "API key", "", "DeepSeek, OpenRouter, Ollama, any OpenAI-compatible API", {}, "",
                "https://api-docs.deepseek.com"),
}

API_PRESETS = {
    "deepseek": {"name": "DeepSeek", "base_url": "https://api.deepseek.com/v1", "model": "deepseek-chat", "key": True},
    "openrouter": {"name": "OpenRouter", "base_url": "https://openrouter.ai/api/v1", "model": "deepseek/deepseek-chat", "key": True},
    "ollama": {"name": "Ollama (local)", "base_url": "http://127.0.0.1:11434/v1", "model": "qwen3-coder", "key": False},
    "lmstudio": {"name": "LM Studio (local)", "base_url": "http://127.0.0.1:1234/v1", "model": "local-model", "key": False},
    "llamacpp": {"name": "llama.cpp (local)", "base_url": "http://127.0.0.1:8080/v1", "model": "local", "key": False},
    "openai": {"name": "OpenAI", "base_url": "https://api.openai.com/v1", "model": "gpt-5", "key": True},
    "custom": {"name": "Custom", "base_url": "", "model": "", "key": True},
}
# Google retired Gemini CLI on 2026-06-18; Antigravity CLI (`agy`) replaced it. Helpers and launcher choices saved
# by older versions as "gemini" carry on as Antigravity.
ALIASES = {"gemini": "antigravity", "agy": "antigravity"}

# decomp.wtf's tools are loaded up front in Antigravity (tools.ALWAYS_LOAD) and need no permission. Should a helper
# reach one through agy's generic MCP dispatcher instead, a headless run can't ask, and agy quietly refuses it:
# this rule in the user's agy settings allows decomp.wtf's tools (and nothing else).
AGY_ALLOW = "mcp(decomp/*)"

# Functions per helper session: a fresh session starts with a small, clean context (each function's assembly,
# drafts and diffs would otherwise ride along into the next one): cheaper per call, and the model stays sharp.
PER_SESSION = 2

# A helper's Focus (the launcher's menu) -> the claim strategy it works by, and what to say when that runs out.
# Default is no focus: claims work as they always have.
FOCUSES = {"quick": ("quick", "no small function (Quick Wins) is left to do here",
                     "Quick Wins, small functions only"),
           "close": ("close", "no function at 90% match or better (Close Calls) is left to do here",
                     "Close Calls, functions already 90%+ matched only")}


# A helper pinned to one function (the function drawer's "Work on this"): runs (agents' own loops) or calls (the lean
# loop) without a gain before it stops; the effort steps an agent's own loop climbs when stuck; the permuter's turn on
# the closest source between calls; the lean loop's methods, as the session says them.
PIN_STALL_RUNS = 4
EFFORT_LADDER = ("low", "medium", "high")
POLISH_SECONDS = 20
LEAN_KINDS = ("api", "codex")  # helpers that can answer the lean loop's short calls
LEAN_NAMES = {"one": "one version per call", "several": "several versions per call",
              "both": "one and several versions per call in turn (a test of the two)"}


def kind_id(kind: str) -> str:
    return ALIASES.get(kind, kind)


def agy_settings() -> Path:
    return Path.home() / ".gemini" / "antigravity-cli" / "settings.json"


def endpoint_entry(e: dict) -> dict:
    """A saved API endpoint, with what's left out filled in from its provider (a name: the provider's, or for a
    custom server the address's host, so sessions say which machine they run on)."""
    provider = e.get("provider") if e.get("provider") in API_PRESETS else "custom"
    preset = API_PRESETS[provider]
    base = (e.get("base_url") or preset["base_url"]).strip()
    host = re.sub(r"^\w+://", "", base).split("/", 1)[0]
    moved = provider == "custom" or base.rstrip("/") != preset["base_url"].rstrip("/")  # Ollama on another box...
    out = {"id": str(e.get("id") or ""), "provider": provider, "base_url": base,
           "model": (e.get("model") or preset["model"]).strip(),
           "name": (e.get("name") or "").strip() or (host if moved and host else preset["name"]) or "API"}
    for k in ("api_key", "context_tokens", "max_sessions"):
        if e.get(k):
            out[k] = e[k]
    return out


def is_local(base: str, resolve: bool = True) -> bool:
    """An address on this computer or the local network (a llama.cpp box, Ollama on another machine): a private,
    loopback, link-local or Tailscale address, a name without dots or under .local/.lan/.internal/.home.arpa, or
    (`resolve`) a name that resolves to such addresses only."""
    import ipaddress
    import socket

    host = re.sub(r"^\w+://", "", base.strip()).split("/", 1)[0].rsplit("@", 1)[-1]
    host = host[1:].split("]", 1)[0] if host.startswith("[") else host.split(":", 1)[0] if host.count(":") == 1 else host
    if not host:
        return False

    def private(ip: str) -> bool:
        try:
            a = ipaddress.ip_address(ip)
        except ValueError:
            return False
        tailscale = a.version == 4 and a in ipaddress.ip_network("100.64.0.0/10")
        return a.is_private or a.is_loopback or a.is_link_local or tailscale or a.is_unspecified

    if private(host):
        return True
    if "." not in host or re.search(r"\.(local|lan|home|internal|localdomain|home\.arpa)$", host, re.I) \
            or host.lower() == "localhost":
        return True
    if not resolve or re.fullmatch(r"[\d.]+|[0-9a-fA-F:]+", host):
        return False
    try:
        addrs = {i[4][0] for i in socket.getaddrinfo(host, None)}
    except (OSError, UnicodeError):
        return False
    return bool(addrs) and all(private(a) for a in addrs)


# what an agent CLI says when nobody signed in to it (Claude Code: "Not logged in · Please run /login")
_SIGNED_OUT = re.compile(r"not logged in|run /login|invalid api key|not authenticated|please (?:log|sign) ?in|"
                         r"auth(?:entication)? method", re.I)
# what a model's provider says when the account has no credit left (DeepSeek: "Insufficient Balance", OpenAI:
# "insufficient_quota", Anthropic: "credit balance is too low"): trying again can't help until it's topped up
_NO_CREDIT = re.compile(r"insufficient[ _](?:balance|credits?|funds|quota)|exceeded your current quota|"
                        r"credit balance is too low|payment required|out of credits?", re.I)
# and when a subscription's quota is used up until it resets (Claude Code: "You've hit your session limit · resets
# 6pm (Europe/London)", Codex: "You've hit your usage limit ... try again in 2 hours 13 minutes"): its helpers wait
_PLAN_LIMIT = re.compile(r"hit your (?:session|usage|weekly|daily|5-hour|five-hour) limit[^\n]*|"
                         r"usage limit (?:reached|exceeded)[^\n]*", re.I)
QUOTA_WAIT = 30 * 60  # how long to wait when the agent doesn't say when its plan's quota resets
# and when it's asked too much at once (HTTP 429): it works again in a while
_THROTTLED = re.compile(r"too many requests|rate[ _-]?limit|concurrency limit|\b429\b|overloaded", re.I)


def no_credit(said: str) -> str | None:
    """The helper's error when its model's provider says the account is out of credit, else None."""
    if not (m := _NO_CREDIT.search(said)):
        return None
    return (f"the model's provider says the account is out of credit (\u201c{m.group(0)}\u201d): top it up, then "
            "start the agents again")


class PlanLimit(Exception):
    """The agent's plan has used up its quota until `until` (a time): the helper waits for it, and so does every
    helper on the same account."""

    def __init__(self, said: str, until: float):
        super().__init__(said)
        self.said, self.until = said, until


def plan_limit(said: str, now: float | None = None) -> PlanLimit | None:
    """A PlanLimit when the agent says its plan's usage limit is reached, with when it resets as the agent says it
    (a time of day in its zone, "in 2 hours 13 minutes", Claude Code's older "|<epoch>"), else QUOTA_WAIT from now."""
    if not (m := _PLAN_LIMIT.search(said)):
        return None
    return PlanLimit(m.group(0).strip(), _reset_at(said[m.start():], time.time() if now is None else now))


def _reset_at(text: str, now: float) -> float:
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    if (m := re.search(r"\|\s*(1\d{9})\b", text)):
        return float(m.group(1))
    if (m := re.search(r"(?:try again|resets?)\s+in\s+((?:\d+\s*(?:days?|d|hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s)"
                       r"\b[,\s]*(?:and\s+)?)+)", text, re.I)):
        secs = sum(int(n) * (86400 if u[0] == "d" else 3600 if u[0] == "h" else 60 if u[0] == "m" else 1)
                   for n, u in re.findall(r"(\d+)\s*([a-z]+)", m.group(1).lower()))
        return now + secs + 60
    if (m := re.search(r"(?:resets?|try again)\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\b\.?\s*(?:\(([^)]+)\))?|"
                       r"(?:resets?|try again)\s+(?:at\s+)?(\d{1,2}):(\d{2})\b\s*(?:\(([^)]+)\))?", text, re.I)):
        hour, minute, half, zone = ((int(m.group(1)), int(m.group(2) or 0), m.group(3).lower(), m.group(4)) if m.group(1)
                                    else (int(m.group(5)), int(m.group(6)), "", m.group(7)))
        hour = hour % 12 + (12 if half == "p" else 0) if half else hour
        try:
            tz = ZoneInfo(zone.strip()) if zone else None
        except Exception:
            tz = None
        base = datetime.fromtimestamp(now, tz) if tz else datetime.fromtimestamp(now).astimezone()
        at = base.replace(hour=hour % 24, minute=minute, second=0, microsecond=0)
        if at.timestamp() <= now:
            at += timedelta(days=1)
        return at.timestamp() + 60
    return now + QUOTA_WAIT


# ------------------------------------------------------------------- tiered Claude Code helpers
# model="tiered": a Claude Code helper whose model is picked per function (tier_for) right before each session.
# Each session runs one function (per_session 1), so the model can change from one claim to the next.
TIERED = "tiered"
HAIKU, SONNET, OPUS = "haiku", "sonnet", "opus"  # Claude Code's aliases: always Anthropic's latest of each kind
# tier -> (model, --effort). The tiers are scoring.stage's: how far the function is (its thresholds are there).
TIERS = {
    "escalated": (OPUS, "high"),       # still far off after STALL_ATTEMPTS attempts: structure not found yet
    "near_miss": (HAIKU, "low"),       # best within NEAR_MISS of the target: operands and registers left, compile-and-adjust
    "mechanical": (HAIKU, "low"),      # structure found (best within MECHANICAL): keep adjusting
    "fresh": (SONNET, "medium"),       # no compiled attempt yet: the first structural attempt, reading look-alikes
    "structural": (SONNET, "medium"),  # a best that is far off: structure still to find
}
EFFORTS = ("low", "medium", "high", "xhigh", "max")  # what Claude Code's --effort takes


def tier_for(state: dict) -> tuple[str, str, str]:
    """(model, effort, tier) for a function's state. Keys: best_score (None = nothing compiled yet), n_insns,
    attempts (every agent's, from the store)."""
    tier = stage(state.get("best_score"), state.get("n_insns"), state.get("attempts"))
    return *TIERS[tier], tier


def next_function(net, slug: str, agent_id: int = 0):
    """The function a helper would most likely get from claim_function next (read-only: the same order as the
    default claim, nothing claimed), or None. A claim the agent still holds comes first, then the project's
    ★ Next in line pins (not on a git project, which has none)."""
    from .. import network as netmod

    if agent_id:
        held = net.store.one("SELECT f.* FROM claims c JOIN functions f ON f.id=c.function_id WHERE c.agent_id=?", agent_id)
        if held is not None:
            return held
    proj = net.project_row(slug)
    fork = bool(net.fork_slug(slug))
    if not fork:
        for name in net.cfg(proj).get("priority", []):
            row = net.store.one("SELECT f.* FROM functions f WHERE f.project_id=? AND f.name=? AND f.status='open' "
                                "AND f.id NOT IN (SELECT function_id FROM claims)", proj["id"], name)
            if row is not None:
                return row
    now = time.time()
    return net.store.one(
        f"""SELECT f.* FROM functions f WHERE f.project_id=? AND f.status='open'
            AND f.id NOT IN (SELECT function_id FROM claims)
            AND f.name NOT IN (SELECT detail FROM events WHERE project_id=? AND agent_id=? AND kind='release')
            ORDER BY f.abandons >= ?, f.name IN (SELECT detail FROM events WHERE project_id=? AND kind='release'
                                AND created_at > ?), {netmod.STRATEGIES["finish" if fork else "easiest"]} LIMIT 1""",
        proj["id"], proj["id"], agent_id, netmod.GIVEN_UP, proj["id"], now - netmod.RELEASE_COOLDOWN)


_versions: dict[str, tuple[float, str | None]] = {}
_models: dict[str, tuple[float, list[dict]]] = {}

# Claude Code's aliases always point at Anthropic's latest model of each kind.
CLAUDE_MODELS = [("fable", "Fable", "Anthropic's most capable model"), ("opus", "Opus", "very capable"),
                 ("sonnet", "Sonnet", "fast and capable, cheaper"), ("haiku", "Haiku", "fastest, cheapest")]


def _codex_config() -> dict:
    """The user's Codex settings (~/.codex/config.toml, or $CODEX_HOME's)."""
    import tomllib

    home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    try:
        return tomllib.loads((home / "config.toml").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _config_model(kind: str) -> str | None:
    """The model the user set as default in the agent's own settings, if any."""
    home = Path.home()
    try:
        if kind == "claude":
            return json.loads((home / ".claude" / "settings.json").read_text(encoding="utf-8")).get("model") or None
        if kind == "codex":
            return _codex_config().get("model") or None
        if kind == "antigravity":
            return json.loads(agy_settings().read_text(encoding="utf-8")).get("model") or None
        if kind == "opencode":
            base = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config")
            return json.loads((base / "opencode" / "opencode.json").read_text(encoding="utf-8")).get("model") or None
    except (OSError, ValueError, AttributeError):
        return None
    return None


def _config_effort(kind: str) -> str | None:
    """The reasoning effort the user set as default in the agent's own settings, if any (Codex)."""
    if kind == "codex":
        return _codex_config().get("model_reasoning_effort") or None
    return None


def _codex_models(path: str) -> list[dict]:
    """Codex's model catalog (`codex debug models`): the models its own picker lists, best first, each with the
    reasoning efforts it supports and the one it uses unless told otherwise. Falls back to the catalog bundled with
    the binary when the refresh fails (offline)."""
    for extra in ([], ["--bundled"]):
        try:
            res = subprocess.run(argv([path, "debug", "models", *extra]), capture_output=True, text=True, encoding="utf-8",
                                 errors="replace", stdin=subprocess.DEVNULL, timeout=30)
            rows = json.loads(res.stdout).get("models") or []
        except (OSError, subprocess.TimeoutExpired, ValueError, AttributeError):
            continue
        out = []
        for m in sorted((r for r in rows if isinstance(r, dict) and r.get("visibility") == "list" and r.get("slug")),
                        key=lambda r: r.get("priority") or 0):
            levels = [lv.get("effort") if isinstance(lv, dict) else lv for lv in m.get("supported_reasoning_levels") or []]
            out.append({"id": m["slug"], "name": m.get("display_name") or m["slug"], "note": m.get("description") or "",
                        "efforts": [e for e in levels if isinstance(e, str)], "effort": m.get("default_reasoning_level") or ""})
        if out:
            return out
    return []


def models(kind: str, path: str | None) -> list[dict]:
    """Models to offer for this agent: [{"id", "name", "note"}]. Claude Code's aliases, with the efforts its --effort
    takes (a tiered helper's tier picks its own); opencode's and Antigravity's own lists (what the user's sign-in
    offers); Codex's catalog, with each model's efforts."""
    if kind == "claude":
        return [{"id": TIERED, "name": "Tiered", "note": "Haiku, Sonnet or Opus per function", "efforts": []}] + \
            [{"id": i, "name": n, "note": note, "efforts": list(EFFORTS)} for i, n, note in CLAUDE_MODELS]
    if kind not in ("opencode", "antigravity", "codex") or not path:
        return []
    cached = _models.get(kind)
    if cached and time.time() - cached[0] < 600:
        return cached[1]
    if kind == "codex":
        out = _codex_models(path)
        _models[kind] = (time.time(), out)
        return out
    try:
        res = subprocess.run(argv([path, "models"]), capture_output=True, text=True, encoding="utf-8", errors="replace",
                             stdin=subprocess.DEVNULL, timeout=30)
        lines = [line.strip() for line in res.stdout.splitlines()]
    except (OSError, subprocess.TimeoutExpired):
        lines = []
    if kind == "opencode":
        rows = [r for r in lines if re.fullmatch(r"[\w.-]+/[\w.:/-]+", r)]
        out = [{"id": r, "name": r.split("/", 1)[1], "note": r.split("/", 1)[0]} for r in rows[:200]]
    else:  # agy: "<slug>\t<name>" per model
        rows = [r.split("\t", 1) for r in lines if re.fullmatch(r"[\w.:-]+\t.+", r)]
        out = [{"id": slug, "name": name.strip(), "note": slug.split("-", 1)[0]} for slug, name in rows[:200]]
    _models[kind] = (time.time(), out)
    return out


_log_levels: dict[str, str] = {}


def _said_by(argv_: list[str]) -> str:
    res = subprocess.run(argv(argv_), capture_output=True, text=True, encoding="utf-8", errors="replace",
                         stdin=subprocess.DEVNULL, timeout=20)
    return res.stdout + res.stderr


def opencode_error_level(path: str) -> str:
    """How this opencode spells its "error" log level. Its `run` accepts one spelling only: 1.x lists upper case
    (DEBUG, INFO, WARN, ERROR) and refuses "error" by printing its help instead of running; 2.x refuses "ERROR".
    Read from its own `run --help`, else from its version."""
    if path not in _log_levels:
        level = "ERROR"
        try:
            choices = re.search(r"--log-level[^\n]*choices:([^\n]*)", _said_by([path, "run", "--help"]))
            if choices:
                level = next((c for c in re.findall(r'"([A-Za-z]+)"', choices.group(1)) if c.lower() == "error"), level)
            else:
                version = re.search(r"\b(\d+)\.\d+", _said_by([path, "--version"]))
                if version and int(version.group(1)) >= 2:
                    level = "error"
        except (OSError, subprocess.TimeoutExpired):
            pass
        _log_levels[path] = level
    return _log_levels[path]


def search_path() -> str | None:
    """Where to look for agents and npm (None: this process's PATH). On Windows also the PATH set for the user right
    now, read from the registry: an agent or Node.js installed while the app runs is found without restarting it."""
    if not WIN:
        return None
    import winreg

    dirs = os.environ.get("PATH", "").split(os.pathsep)
    for root, key in ((winreg.HKEY_CURRENT_USER, "Environment"),
                      (winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment")):
        try:
            with winreg.OpenKey(root, key) as k:
                dirs += [os.path.expandvars(d) for d in str(winreg.QueryValueEx(k, "Path")[0]).split(";")]
        except OSError:
            pass
    return os.pathsep.join(dict.fromkeys(d for d in dirs if d))


def _which(cmd: str) -> str | None:
    home = Path.home()
    own = {"opencode": home / ".opencode" / "bin" / "opencode"}.get(cmd)  # its self-updating install beats a stale PATH copy
    if own and own.exists():
        return str(own)
    found = shutil.which(cmd, path=search_path())
    if found:
        return found
    dirs = [home / ".local" / "bin", home / ".claude" / "local", home / ".opencode" / "bin", home / ".npm-global" / "bin"]
    if WIN and os.environ.get("APPDATA"):
        dirs.append(Path(os.environ["APPDATA"]) / "npm")  # npm's global installs on Windows
    if WIN and os.environ.get("LOCALAPPDATA"):
        dirs.append(Path(os.environ["LOCALAPPDATA"]) / "agy" / "bin")  # where Antigravity's installer puts agy
    # on Windows the file without an extension next to npm's foo.cmd is a shell script Windows can't run
    names = (f"{cmd}.exe", f"{cmd}.cmd") if WIN else (cmd, f"{cmd}.cmd", f"{cmd}.exe")
    for d in dirs:
        for name in names:
            if (d / name).exists():
                return str(d / name)
    return None


# npm's Windows launcher (cmd-shim) ends with: "%_prog%" [node options] "%dp0%\node_modules\...\cli.js" %*
_NPM_SHIM = re.compile(r'"%_prog%"\s+(?P<opts>[^"\r\n]*?)\s*"%~?dp0%?\\(?P<script>[^"\r\n]+)"\s+%\*', re.I)


def argv(cmd: list[str]) -> list[str]:
    """The command to start, as Windows needs it. npm installs agents there as .cmd scripts, which run through cmd.exe:
    it cuts an argument at its first line break (a prompt loses everything after its first line, and the flags after
    it with it) and treats & | < > in it as commands. Start node with the agent's script instead, as the .cmd would."""
    if not WIN or Path(cmd[0]).suffix.lower() not in (".cmd", ".bat"):
        return cmd
    shim = Path(cmd[0])
    try:
        m = _NPM_SHIM.search(shim.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        m = None
    if not m:
        return cmd
    script = shim.parent / m["script"]
    node = shim.parent / "node.exe"
    node = str(node) if node.exists() else shutil.which("node", path=search_path())
    if not node or not script.exists():
        return cmd
    return [node, *m["opts"].split(), str(script), *cmd[1:]]


def signin(k: Kind) -> str:
    """How to sign in to an agent, said for where decomp.wtf runs (WSL/Docker: in decomp.wtf's own terminal)."""
    from .. import hostenv

    if not k.signin or hostenv.runtime() == "native":
        return k.signin
    text = k.signin.removeprefix("Open a terminal, ").removeprefix("Open a terminal and ")
    return f"{hostenv.terminal_hint()}, then: {text[0].lower()}{text[1:]}"


def detect() -> list[dict]:
    out = []
    for k in KINDS.values():
        row = {"id": k.id, "name": k.name, "vendor": k.vendor, "signin": signin(k), "docs": k.docs,
               "install": k.install.get("windows" if WIN else "unix", "")}
        if not k.command:
            row.update(installed=True, version=None, path=None)
        else:
            path = _which(k.command)
            cached = _versions.get(k.id)
            if path and (cached is None or time.time() - cached[0] > 300):
                try:
                    res = subprocess.run(argv([path, "--version"]), capture_output=True, text=True, encoding="utf-8",
                                         errors="replace", timeout=15)
                    version = (res.stdout or res.stderr).strip().splitlines()[0][:60] if (res.stdout or res.stderr).strip() else None
                except (OSError, subprocess.TimeoutExpired):
                    version = None
                _versions[k.id] = (time.time(), version)
            row.update(installed=path is not None, path=path, version=_versions.get(k.id, (0, None))[1] if path else None)
            if path:
                row.update(models=models(k.id, path), default_model=_config_model(k.id), default_effort=_config_effort(k.id))
        if row["install"].startswith("npm ") and not row["installed"] and not shutil.which("npm", path=search_path()):
            row["needs"] = "Node.js (https://nodejs.org) for npm"
        elif k.id == "claude" and WIN and not shutil.which("git", path=search_path()):
            row["needs"] = "Git for Windows (https://git-scm.com/downloads/win): Claude Code needs its Git Bash"
        out.append(row)
    return out


# ---------------------------------------------------------------------------------- output parsing

def _tool_name(name: str) -> str:
    for prefix in ("mcp__decomp__", "mcp_decomp_", "decomp_agent.", "decomp_agent__", "decomp__", "decomp_", "decomp."):
        if name.startswith(prefix):
            return name[len(prefix):]
    return name


def _args_brief(args) -> str:
    if not isinstance(args, dict):
        return ""
    parts = []
    for k, v in args.items():
        if k == "source":
            # (documented assembly has a label line and no braces; anything else reads as C)
            asm = "{" not in str(v) and re.search(r"(?m)^\s*[A-Za-z_.@]\w*:", str(v))
            parts.append(f"{len(str(v).splitlines())} lines of {'assembly' if asm else 'C'}")
        elif isinstance(v, str):
            parts.append(v if len(v) < 40 else v[:37] + "…")
        elif v not in (None, "", {}):
            parts.append(str(v))
    return ", ".join(parts)


def _error_text(err) -> str:
    """The message in an agent's error object, whatever its shape (opencode nests it under data)."""
    if isinstance(err, dict):
        data = err.get("data") if isinstance(err.get("data"), dict) else {}
        msg = err.get("message") or data.get("message") or ""
        name = err.get("name") or err.get("type") or ""
        if msg and name and name not in ("UnknownError", "Error") and name not in msg:
            return f"{name}: {msg}"
        return str(msg or name or json.dumps(err)[:300])
    return str(err or "")


def _parse_agy(obj: dict, state: dict) -> list[tuple[str, str]]:
    """Antigravity CLI's stream-json: step_update events (text arrives in deltas, gathered per step) and a result."""
    out: list[tuple[str, str]] = []
    ev = obj.get("event")
    if ev == "step_update" and isinstance(obj.get("step_update"), dict):
        st = obj["step_update"]
        step, kind = st.get("step_index"), st.get("step_type")
        if kind in ("agent_response", "thinking", "planner_response"):
            texts = state.setdefault("agy_text", {})
            texts[step] = texts.get(step, "") + str(st.get("text_delta") or "")
            if st.get("state") == "DONE" and texts.get(step, "").strip():
                out.append(("think" if kind == "thinking" else "text", texts.pop(step).strip()))
        elif kind == "tool" and st.get("state") == "ACTIVE" and step not in state.setdefault("agy_tools", set()):
            state["agy_tools"].add(step)
            info = st.get("tool_info") if isinstance(st.get("tool_info"), dict) else {}
            name, params = info.get("name") or st.get("tool_name") or "", info.get("parameters")
            if name == "call_mcp_tool" and isinstance(params, dict):  # agy reaches MCP tools through one dispatcher
                name, params = params.get("ToolName") or name, params.get("Arguments")
            out.append(("tool", f"{_tool_name(name)}({_args_brief(params)})"))
    elif ev == "result" and isinstance(obj.get("result"), dict):
        res = obj["result"]
        usage = res.get("usage") if isinstance(res.get("usage"), dict) else {}
        if usage.get("total_tokens"):
            out.append(("usage", json.dumps({"tokens": usage["total_tokens"]})))
        denied = [d for d in res.get("denied_actions") or [] if isinstance(d, dict)]
        if any(d.get("action") == "mcp" for d in denied):
            out.append(("error", f"Antigravity refused decomp.wtf's tools: a background run can't ask you for permission. "
                                 f"Allow them once: add \"{AGY_ALLOW}\" to \"permissions\": {{\"allow\": [...]}} in "
                                 f"{agy_settings()} (only decomp.wtf's tools; shell, files and the web keep asking)."))
        if res.get("status") not in (None, "SUCCESS"):
            out.append(("error", str(res.get("error") or f"session ended: {res.get('status')}")[:400]))
        else:
            turns = res.get("num_turns")
            out.append(("info", "session finished" + (f" ({turns} turns)" if turns else "")))
    elif ev == "error":
        out.append(("error", _error_text(obj.get("error") or obj.get("message"))[:400]))
    return out


def clean_stderr(kind: str, line: str) -> str:
    """One line of an agent's stderr as worth showing, or "" for noise."""
    line = line.strip()
    if kind == "opencode" and "level=" in line:  # opencode --print-logs: keep the error itself, drop stack noise
        m = re.search(r'\berror="((?:[^"\\]|\\.)*)"', line)
        return m.group(1).replace('\\"', '"') if m and "level=ERROR" in line else ""
    if line.startswith("AGY_ERROR:"):  # agy: a structured error line
        try:
            return _error_text(json.loads(line.split(":", 1)[1]))[:300]
        except ValueError:
            return line
    return line


def parse_line(kind: str, line: str, state: dict | None = None) -> list[tuple[str, str]]:
    """(type, text) items for the live feed from one line of agent output: text, think, tool, info, error, usage.
    `state` carries what spans lines (Antigravity streams text in pieces) for one session."""
    line = line.strip()
    if not line:
        return []
    try:
        obj = json.loads(line)
    except ValueError:
        return [("info", line[:400])]
    if not isinstance(obj, dict):
        return []
    if "event" in obj and "type" not in obj:  # Antigravity CLI
        return [(k, v) for k, v in _parse_agy(obj, {} if state is None else state) if v]
    t = obj.get("type")
    out: list[tuple[str, str]] = []
    if t == "assistant" and isinstance(obj.get("message"), dict):  # Claude Code
        for c in obj["message"].get("content", []):
            if c.get("type") == "text" and c.get("text", "").strip():
                out.append(("text", c["text"].strip()))
            elif c.get("type") == "thinking" and c.get("thinking", "").strip():
                out.append(("think", c["thinking"].strip()))
            elif c.get("type") == "tool_use" and c.get("name") != "ToolSearch":  # ToolSearch: Claude Code loading tools
                out.append(("tool", f"{_tool_name(c.get('name', ''))}({_args_brief(c.get('input'))})"))
    elif t == "result":  # Claude Code end of session
        if obj.get("is_error") or obj.get("status") == "error":
            out.append(("error", str(obj.get("result") or obj.get("error") or "session failed")[:400]))
        else:
            cost = obj.get("total_cost_usd")
            turns = obj.get("num_turns")
            bits = [f"{turns} turns" if turns else "", f"${cost:.2f}" if isinstance(cost, (int, float)) else ""]
            out.append(("info", "session finished" + (f" ({', '.join(b for b in bits if b)})" if any(bits) else "")))
            if isinstance(cost, (int, float)):
                out.append(("usage", json.dumps({"usd": cost})))
            stats = obj.get("stats") if isinstance(obj.get("stats"), dict) else {}
            if stats.get("total_tokens"):
                out.append(("usage", json.dumps({"tokens": stats["total_tokens"]})))
    elif t in ("item.started", "item.completed") and isinstance(obj.get("item"), dict):  # Codex
        item = obj["item"]
        it = item.get("type") or item.get("item_type")
        if it == "agent_message" and t == "item.completed":
            out.append(("text", str(item.get("text", "")).strip()))
        elif it == "reasoning" and t == "item.completed" and item.get("text"):
            out.append(("think", str(item["text"]).strip()))
        elif it == "mcp_tool_call" and t == "item.started":
            out.append(("tool", f"{_tool_name(item.get('tool', ''))}({_args_brief(item.get('arguments'))})"))
        elif it == "command_execution" and t == "item.started":
            out.append(("tool", f"shell: {str(item.get('command', ''))[:80]}"))
        elif it == "error":
            out.append(("error", str(item.get("message", ""))[:400]))
    elif t == "turn.completed":
        out.append(("info", "turn finished"))
        usage = obj.get("usage") if isinstance(obj.get("usage"), dict) else {}
        tokens = sum(v for k, v in usage.items() if k in ("input_tokens", "output_tokens") and isinstance(v, int))
        if tokens:
            out.append(("usage", json.dumps({"tokens": tokens})))
    elif t in ("error", "turn.failed"):  # Codex; opencode: {"error": {"name", "data": {"message"}}}
        text = _error_text(obj.get("error") or obj.get("message"))
        if not text.startswith("Unexpected server error"):  # opencode's placeholder: its log line says what failed
            out.append(("error", text[:400]))
    elif t == "tool_use":  # opencode
        part = obj.get("part") if isinstance(obj.get("part"), dict) else obj
        name = part.get("tool_name") or part.get("tool") or part.get("name") or ""
        args = part.get("parameters") or (part.get("state") or {}).get("input") or part.get("input")
        out.append(("tool", f"{_tool_name(name)}({_args_brief(args)})"))
    elif t == "text" and isinstance(obj.get("part"), dict):  # opencode
        text = obj["part"].get("text", "").strip()
        if text:
            out.append(("text", text))
    elif t in ("step_finish", "step-finish") and isinstance(obj.get("part"), dict):  # opencode: cost per step
        part = obj["part"]
        if isinstance(part.get("cost"), (int, float)) and part["cost"]:
            out.append(("usage", json.dumps({"usd": part["cost"]})))
    return [(k, v) for k, v in out if v]


# ---------------------------------------------------------------------------------- sessions

@dataclass
class Session:
    id: str
    kind: str
    label: str
    slug: str
    agent_name: str
    options: dict
    state: str = "starting"  # starting | working | waiting | stopped | error
    started_at: float = field(default_factory=time.time)
    runs: int = 0
    error: str = ""
    feed: collections.deque = field(default_factory=lambda: collections.deque(maxlen=400))
    seq: int = 0
    stop: threading.Event = field(default_factory=threading.Event)
    proc: subprocess.Popen | None = None
    client: httpx.Client | None = None  # api / lean loop: the httpx client, closed on stop for immediate kill
    usd: float = 0.0  # what the agent's provider charged, as the agent reports it
    tokens: int = 0
    signed_out: bool = False  # the agent CLI isn't signed in: trying again won't help
    throttled: bool = False  # the last run ended on the provider's limit of requests at once: not a failure
    routed_model: str = ""  # tiered helpers: the model picked for the current run
    routed_effort: str = ""  # ...and the --effort that goes with it
    tier: str = ""  # ...which tier picked it (tier_for)
    tier_function: str = ""  # ...and the function it was picked for
    agent_id: int = 0  # the coordinator's agent id, once the run registered it
    # pinned to one function (options "pin"): how the next attempts were asked for and what each cost, for their
    # stamps; the lean loop (API helpers) and its state across runs; why the session is done; runs without a gain
    method: str = ""
    call_tokens: int | None = None
    using_model: str = ""
    lean: object = None
    pin_done: str = ""
    pin_stall: int = 0
    last_output: float = 0.0  # when the agent CLI last said anything (0: no CLI run going): the watchdog's clock
    resume_note: str = ""  # for the next session's prompt: where the one the watchdog ended was
    wait_until: float = 0.0  # waiting for its account: a plan's quota to reset, or the provider's limit of requests
    wait_why: str = ""
    # API helpers: when the request now waiting on the model went out (0: none), when the routine it holds runs out of
    # its time (ROUTINE_BUDGET; 0: it holds none), which routine that is, and the model's measured tokens a second
    asking_since: float = 0.0
    routine_until: float = 0.0
    timed_out: set = field(default_factory=set)  # routines (names) this helper ran out of time on: not taken back
    routine_id: int = 0
    tok_rate: float = 0.0
    log_path: Path | None = None  # the run written out in full, a JSON line per event (Manager._open_log)
    log_lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def tiered(self) -> bool:
        return self.options.get("model") == TIERED

    def push(self, kind: str, text: str) -> None:
        self.seq += 1
        self.feed.append({"seq": self.seq, "t": time.time(), "type": kind, "text": text[:4000]})
        self.log(kind, text=text)

    def log(self, event: str, **data) -> None:
        """One event of the run, in full, to its log file (none open: nothing). A log that can't be written never
        stops the helper."""
        if self.log_path is None:
            return
        line = json.dumps({"t": round(time.time(), 3), "type": event} | data, ensure_ascii=False, default=str)
        try:
            with self.log_lock, open(self.log_path, "a", encoding="utf-8", newline="\n") as f:
                f.write(line + "\n")
        except OSError:
            pass

    def snapshot(self, since: int = 0) -> dict:
        return {"last_output": self.last_output, "wait_until": self.wait_until, "wait_why": self.wait_why,
                "asking_since": self.asking_since, "routine_until": self.routine_until,
                "id": self.id, "kind": self.kind, "label": self.label, "slug": self.slug, "agent": self.agent_name, "focus": self.options.get("focus") or "",
                "state": self.state, "runs": self.runs, "error": self.error, "started_at": self.started_at,
                "model": (self.routed_model if self.tiered else self.options.get("model")) or "",
                "effort": (self.routed_effort if self.tiered else self.options.get("effort")) or "",
                "tiered": self.tiered, "tier": self.tier, "tier_function": self.tier_function,
                "usd": round(self.usd, 4), "tokens": self.tokens, "only_starred": bool(self.options.get("only_starred")),
                "pin": self.options.get("pin") or "", "lean": self.options.get("lean") or "",
                "track": self.options.get("track") or "",
                "feed": [f for f in self.feed if f["seq"] > since]}


# The compiler guide a ROM project's agents read first, by platform (on N64, by its compiler).
ROM_COMPILER_GUIDES = {"psx": "psx", "ps2": "ps2", "gba": "agbcc", "gb": "gb", "gbc": "gb", "sms": "sms", "gg": "sms", "nes": "nes", "c64": "c64"}


def compiler_guide(ov: dict) -> str:
    """The guide() topic for a ROM project's compiler (`ov` is its overview)."""
    return ROM_COMPILER_GUIDES.get(ov.get("platform")) or (
        "ido" if ov["compiler"]["toolchain"].startswith("ido") else "gcc-kmc")


class Manager:
    def __init__(self, net: RomNetwork, home: Path, mcp_url: str):
        self.net, self.home, self.mcp_url = net, home, mcp_url
        self.sessions: dict[str, Session] = {}
        self.assignments: dict[int, str] = {}  # agent id -> project slug
        self._lock = threading.Lock()
        self._prep: dict[str, threading.Event] = {}  # slug -> giving its code source files before agents claim
        self.on_stop = lambda: None  # set by the server: close tunnels when the app quits
        self.persist = lambda running: None  # set by the server: remember helpers to resume after a restart
        # an account's helpers wait together: until its plan's quota resets, or after its provider said too many
        # requests (each time a little longer, until a run goes through)
        self.paused: dict[str, float] = {}
        self.paused_why: dict[str, str] = {}
        self.backoff: dict[str, int] = {}
        if net is not None:
            net.run_info = self.run_info  # each attempt says which helper setup made it

    def run_info(self, agent_id: int) -> dict | None:
        """The kind, model and effort of the helper running as this agent, for its attempts (None: no helper of
        this app's is). A model or effort the helper wasn't given is its own default: the user's setting, when
        decomp.wtf can read it."""
        s = next((x for x in list(self.sessions.values()) if x.agent_id == agent_id and agent_id), None)
        if s is None:
            return None
        model = (s.routed_model if s.tiered else s.options.get("model")) or ""
        effort = (s.routed_effort if s.tiered else s.options.get("effort")) or ""
        if s.kind != "api":
            model = model or _config_model(s.kind) or "default"
            effort = effort or _config_effort(s.kind) or "default"
        out = {"kind": s.kind, "model": s.using_model or model, "effort": effort}
        if s.options.get("pin") or s.options.get("lean"):  # how it asked (1 or several per call, or the agent's own loop)
            out["method"] = s.method or "agent"
            if s.call_tokens is not None:
                out["tokens"] = s.call_tokens
        return out

    # -- control

    def start(self, kind: str, slug: str, count: int = 1, options: dict | None = None) -> list[str]:
        options = dict(options or {})
        if kind_id(kind) != kind:
            kind = kind_id(kind)
            options.pop("model", None)  # a Gemini CLI model name may mean nothing to Antigravity: use its default
            options.pop("effort", None)
        if kind not in KINDS:
            raise ValueError(f"unknown agent kind {kind!r}")
        if options.get("lean") and kind not in LEAN_KINDS:
            raise ValueError("the lean loop is for API helpers and Codex: other agents work their own way")
        if options.get("pin"):
            options["per_session"] = 1  # one function: each run a fresh session on it
            count = 1
        if options.get("model") == TIERED:
            if kind != "claude":
                raise ValueError("tiered models (Haiku, Sonnet or Opus per function) are for Claude Code helpers")
            options["per_session"] = 1  # one function per session: the next claim gets its own model
            options.pop("effort", None)  # the tier picks the effort with the model
        ids = []
        self._prepare(slug)
        with self._lock:
            for _ in range(max(1, min(int(count), 8))):
                n = 1 + sum(1 for s in self.sessions.values() if s.kind == kind)
                sid = f"{kind}-{n}"
                while sid in self.sessions:
                    n += 1
                    sid = f"{kind}-{n}"
                if kind == "api":  # numbered per endpoint: "gpu-box #2" says which machine it runs on
                    name = options.get("provider_name") or "API"
                    k = 1 + max((int(s.label.rsplit(" #", 1)[1]) for s in self.sessions.values() if s.kind == "api"
                                 and s.label.rsplit(" #", 1)[0] == name and s.label.rsplit(" #", 1)[-1].isdigit()), default=0)
                    label = f"{name} #{k}"
                else:
                    label = f"{KINDS[kind].name} #{n}"
                s = Session(sid, kind, label, slug, sid, dict(options))
                self.sessions[sid] = s
                threading.Thread(target=self._run, args=(s,), daemon=True, name=f"agent-{sid}").start()
                ids.append(sid)
        self.persist(self.running())
        return ids

    def _prepare(self, slug: str) -> None:
        """Before agents claim: free matches from identical code (twins, in the background: what they'd match is
        left for last meanwhile), and on a GameCube/Wii decomp where code has no source file yet, files for it
        (start_files, checked against the game's checksum), or they'd all share the few hard functions left."""
        try:
            self.net.twins.refresh(slug)
            self.net.permuter.start(slug)  # near misses agents leave, on spare CPU
        except Exception:  # agents work without them
            pass
        with self._lock:
            if slug in self._prep:
                return
            try:
                if not self.net.fork_slug(slug):
                    return
                _, fp, cfg = self.net.forks.get(slug)
                if not (self.net.forks.uncovered(slug) or {}).get("functions") or not fp.linkable():
                    return
            except Exception:
                return
            done = self._prep[slug] = threading.Event()

        def run() -> None:
            try:
                self.net.forks.start_files(slug, log=lambda *_: None)
            except Exception:  # the agents still work on what has files
                pass
            finally:
                done.set()
                with self._lock:
                    self._prep.pop(slug, None)

        threading.Thread(target=run, daemon=True, name=f"start-files-{slug}").start()

    def running(self) -> list[dict]:
        """The helpers that should keep working: resumed by the server after the app restarts."""
        return [{"kind": s.kind, "slug": s.slug, "options": {k: v for k, v in s.options.items() if k not in ("api_key", "escalate")}}
                for s in self.sessions.values() if s.state in ("starting", "working", "waiting") and not s.stop.is_set()]

    def stop(self, sid: str | None = None) -> None:
        """Stop one helper (for good) or all of them."""
        for s in list(self.sessions.values()):
            if sid in (None, s.id):
                s.stop.set()
                if s.proc and s.proc.poll() is None:
                    _terminate(proc=s.proc)
                elif s.client is not None:
                    # API-only agents have no subprocess: close the httpx client to kill any
                    # in-flight HTTP call immediately. Lean agents also have an httpx client
                    # but we don't need to touch it — they manage it via their own context manager.
                    _terminate(client=s.client)
                if s.state not in ("error",):
                    s.state = "stopped"
                if sid is not None and s.agent_id:
                    # its function goes back now, not when its thread ends: an API helper waiting on its model kept
                    # it until the reply came (and the room's host is asked off this thread: Stop doesn't wait)
                    row = self.net.store.one("SELECT id, name, owner, trusted FROM agents WHERE id=?", s.agent_id)
                    if row is not None:
                        agent = Agent(row["id"], row["name"], row["owner"], bool(row["trusted"]))
                        if self.net.links.get(s.slug) is None:
                            self._free_claims(s, agent)
                        else:
                            threading.Thread(target=self._free_claims, args=(s, agent), daemon=True,
                                             name=f"free-{s.id}").start()
        if sid is not None:
            self.persist(self.running())

    def shutdown(self) -> None:
        """Stop and remove every helper, and don't resume them when the app starts again."""
        self.stop()
        self.sessions.clear()
        self.persist([])
        for slug in list(self.net.permuter.jobs):
            self.net.permuter.stop(slug)
        self.net.release_local_claims()

    def remove(self, sid: str) -> None:
        self.stop(sid)
        self.sessions.pop(sid, None)

    def snapshot(self, since: dict | None = None) -> list[dict]:
        since = since or {}
        return [s.snapshot(int(since.get(s.id, 0))) for s in self.sessions.values()]

    def project_of(self, agent_id: int) -> str | None:
        return self.assignments.get(agent_id)

    # -- loop

    def _progress(self, agent_id: int) -> int:
        """Compiles + claims + notes by this agent: a session that adds none of these made no progress."""
        row = self.net.store.one(
            "SELECT (SELECT COUNT(*) FROM attempts WHERE agent_id=?) + (SELECT COUNT(*) FROM events WHERE agent_id=?) AS n",
            agent_id, agent_id)
        return row["n"]

    @staticmethod
    def _asm_left(net, proj) -> int:
        """Hand-written routines of a project still to document (the assembly track)."""
        return sum(1 for r in net.store.all("SELECT status, meta FROM functions WHERE project_id=? AND status='asm'",
                                            proj["id"]) if net.asm_track(r))

    def _left(self, slug: str, track: str = "") -> int:
        proj = self.net.project_row(slug)
        if track == "asm":
            return self._asm_left(self.net, proj)
        row = self.net.store.one("SELECT COUNT(*) AS n FROM functions WHERE project_id=? AND status='open'", proj["id"])
        return row["n"]

    def _console(self, slug: str) -> dict:
        """The project's platform and CPU, which decide the console-only tools a session gets (unknown, as in a room
        another computer hosts: every tool, and its handler says no)."""
        try:
            cfg = self.net.cfg(self.net.project_row(slug))
        except Exception:
            return {}
        return {"platform": str(cfg.get("platform") or ""), "arch": str(cfg.get("arch") or "")}

    def _starred_left(self, slug: str, agent_id: int, since: float = 0.0) -> tuple[int, int]:
        """(★ Next in line functions still worth this helper's time: open and not released by it since `since`, how many
        of those it could claim now: nobody else holds them)."""
        proj = self.net.project_row(slug)
        names = self.net.cfg(proj).get("priority", [])
        names = [n for n in names if n not in self.net.released_since(agent_id, proj["id"], names, since)]
        rows = [self.net.store.one(
            "SELECT f.id, (SELECT agent_id FROM claims WHERE function_id=f.id) AS holder FROM functions f "
            "WHERE f.project_id=? AND f.name=? AND f.status='open'", proj["id"], name) for name in names]
        rows = [r for r in rows if r]
        return len(rows), sum(1 for r in rows if r["holder"] in (None, agent_id))

    def _account(self, s: Session, usage: dict) -> None:
        """Add what a session spent to the session and to the project (kept in its config across restarts)."""
        usd, tokens = float(usage.get("usd") or 0), int(usage.get("tokens") or 0)
        s.usd += usd
        s.tokens += tokens
        self.net.add_spend(s.slug, usd, tokens)

    def over_budget(self, slug: str) -> float | None:
        """The budget (USD) when the project's agents have spent it, else None."""
        cap = self.budget()
        spent = (self.net.spend(slug) or {}).get("usd", 0)
        return cap if cap and spent >= cap else None

    def budget(self) -> float:
        return 0.0  # set by the server from the settings

    def _open_log(self, s: Session) -> None:
        """Where this run is written out in full: logs/agents/<project>-<helper>-<when it started>.jsonl, the oldest
        beyond AGENT_LOGS removed."""
        folder = self.home / "logs" / "agents"
        try:
            folder.mkdir(parents=True, exist_ok=True)
            when = time.strftime("%Y%m%d-%H%M%S", time.localtime(s.started_at))
            s.log_path = folder / f"{_safe_name(s.slug)}-{_safe_name(s.id)}-{when}.jsonl"
            old = sorted(folder.glob("*.jsonl"), key=lambda f: f.stat().st_mtime)
            for f in old[:max(0, len(old) - AGENT_LOGS + 1)]:
                f.unlink(missing_ok=True)
        except OSError:
            s.log_path = None
            return
        s.log("start", project=s.slug, helper=s.id, agent_kind=s.kind, label=s.label,
              options={k: v for k, v in s.options.items() if "key" not in k.lower() and "token" != k.lower()})

    def _run(self, s: Session) -> None:
        if s.log_path is None:
            self._open_log(s)
        agent, token = self.net.local_agent(s.agent_name)
        only_starred = bool(s.options.get("only_starred"))
        if only_starred:
            self.net.starred_only[agent.id] = s.started_at
        else:
            self.net.starred_only.pop(agent.id, None)
        focus = FOCUSES.get(s.options.get("focus") or "")
        if focus:
            self.net.focus[agent.id] = focus[0]
            s.push("info", f"focus: {focus[2]}")
        track = "asm" if s.options.get("track") == "asm" else ""
        if track:
            self.net.tracks[agent.id] = track
            s.push("info", "track: documenting hand-written routines in assembly")
        else:
            self.net.tracks.pop(agent.id, None)
        pin = s.options.get("pin") or ""
        if pin:
            self.net.pinned[agent.id] = pin
            s.push("info", f"pinned to {pin}" + (f": the lean loop, {LEAN_NAMES.get(s.options.get('lean'), '')}"
                                                  if s.options.get("lean") else ""))
            s.options.setdefault("base_effort", s.options.get("effort") or "")
        self.assignments[agent.id] = s.slug
        s.agent_id = agent.id
        workdir = self.home / "agents" / s.id
        workdir.mkdir(parents=True, exist_ok=True)
        failures = throttles = 0
        s.push("info", f"{s.label} starting on {s.slug}")
        if (prep := self._prep.get(s.slug)) is not None:
            s.push("info", "first giving the game's code without a source file one (about a minute)")
            prep.wait(900)
        twins = getattr(getattr(self.net, "twins", None), "jobs", {}).get(s.slug)
        if twins is not None and not twins.ordered.is_set():
            s.push("info", "first setting hand-written assembly aside and putting the functions in order "
                           "(seconds; a minute the first time)")
            twins.ordered.wait(120)
        while not s.stop.is_set():
            if pin and not s.pin_done:
                s.pin_done = self._pin_check(s, pin)
            if s.pin_done:  # (a lean helper also says so when nothing is left for it)
                s.push("info", f"stopping: {s.pin_done}")
                break
            if only_starred:
                total, free = self._starred_left(s.slug, agent.id, s.started_at)
                if total == 0:
                    s.push("info", "no ★ Next in line function left to try: they're done, or you gave the rest back 🎉")
                    break
                if free == 0:
                    if s.state != "waiting":
                        s.push("info", "the ★ functions left are being worked on by others: waiting for them")
                    s.state = "waiting"
                    s.stop.wait(30)
                    continue
            elif not pin and self._left(s.slug, track) == 0:
                s.push("info", "nothing left on the assembly track here 🎉" if track else "nothing left to decompile here 🎉")
                break
            if focus and self.net.strategy_left(s.slug, focus[0], agent.id) == 0:
                s.push("info", f"stopping: {focus[1]}")
                break
            if (cap := self.over_budget(s.slug)) is not None:
                s.push("info", f"stopping: the agents on this project have spent the ${cap:.2f} budget (Setup)")
                break
            link = self.net.links.get(s.slug)
            if link is not None and (link.error or time.time() - link.last_ok > 30):
                # in a room whose host is away (or that hasn't let us in yet): wait, don't fail
                if s.state != "waiting" or not s.feed or "waiting for the room" not in s.feed[-1]["text"]:
                    s.push("info", f"waiting for the room's host: {link.error or 'not reachable right now'}")
                s.state = "waiting"
                try:
                    link.sync()
                except Exception:
                    pass
                s.stop.wait(10)
                continue
            if (until := self.paused.get(self.account(s), 0.0)) > time.time():
                if s.wait_until != until:
                    s.wait_until, s.wait_why = until, self.paused_why.get(self.account(s), "")
                    s.push("info", f"waiting until {time.strftime('%H:%M', time.localtime(until))}: {s.wait_why}")
                s.state = "waiting"
                s.stop.wait(min(60.0, until - time.time()))
                continue
            s.wait_until, s.wait_why = 0.0, ""
            s.state = "working"
            s.runs += 1
            t0 = time.time()
            before = self._progress(agent.id)
            pin_best = self._pin_best(s, pin) if pin else None
            if pin and not s.options.get("lean"):
                self._pin_effort(s)
            if s.tiered:  # the model for the function this session is about to claim
                s.routed_model, s.routed_effort, s.tier, s.tier_function = self.tier(s)
                s.push("info", f"session {s.runs}: {s.tier.replace('_', ' ')} \u2192 {s.routed_model} ({s.routed_effort} effort)"
                               + (f" for {s.tier_function}" if s.tier_function else ""))
            try:
                if s.options.get("lean"):
                    ok = self._run_lean(s, agent)
                elif s.kind == "api":
                    ok = self._run_api(s, agent)
                else:
                    ok = self._run_cli(s, token, workdir)
            except Fatal as e:  # needs the user first: retrying can't help
                s.state, s.error = "error", str(e)
                break
            except PlanLimit as e:  # every helper on this account waits for the reset, then carries on
                self.pause(s, e.until, f"the plan's usage limit is used up (\u201c{e.said[:120]}\u201d)")
                continue
            except Exception as e:  # keep the runner alive; show what happened
                s.push("error", f"{type(e).__name__}: {e}")
                ok = False
            if pin and not s.options.get("lean") and not s.stop.is_set():
                self._pin_after(s, pin, pin_best)
            if ok and not s.stop.is_set() and self._progress(agent.id) == before:
                if only_starred and self._starred_left(s.slug, agent.id, s.started_at)[1] == 0:
                    # another helper took the last free ★ between the check and this session's claim: not a failure
                    s.push("info", "the ★ functions left were taken by others meanwhile: waiting for them")
                    s.state = "waiting"
                    s.stop.wait(30)
                    continue
                s.error = "the session ended without compiling anything (check the agent's output)"
                s.push("error", s.error)
                ok = False
            if s.stop.is_set():
                break
            if s.signed_out:
                s.state = "error"
                break
            if s.throttled and throttles < 10:  # the provider's limit, not the agent's failure: wait it out
                s.throttled, throttles = False, throttles + 1
                key = self.account(s)  # with every helper on the account, a little longer each time
                n = self.backoff[key] = self.backoff.get(key, 0) + 1
                self.pause(s, time.time() + min(900, 60 * 2 ** (n - 1)),
                           "the model's provider says too many requests at once (fewer helpers on this account "
                           "would wait less)")
                continue
            if ok:
                self.backoff.pop(self.account(s), None)
            throttles = 0 if ok else throttles
            failures = 0 if ok else failures + 1
            if failures >= 3:
                s.state = "error"
                s.error = s.error or "the agent failed three times in a row"
                s.push("error", f"stopping: {s.error}. {signin(KINDS[s.kind])}".strip())
                break
            s.state = "waiting"
            s.stop.wait(3 if ok else min(60, 5 * failures + max(0, 10 - (time.time() - t0))))
        if s.state != "error":
            s.state = "stopped"
        if not s.stop.is_set():  # finished by itself (done, budget, errors): no need to resume it
            s.stop.set()
            self.persist(self.running())
        self._free_claims(s, agent)
        self.net.starred_only.pop(agent.id, None)
        self.net.focus.pop(agent.id, None)
        self.net.pinned.pop(agent.id, None)

    def account(self, s: Session) -> str:
        """Whose quota and request limits a helper uses: an agent CLI's sign-in on this computer (a lean Codex helper's
        too), or an API endpoint."""
        return f"api:{s.options.get('base_url') or ''}" if s.kind == "api" else s.kind

    def pause(self, s: Session, until: float, why: str) -> None:
        """This helper's account waits until `until`, and every helper on it with it."""
        key = self.account(s)
        if until > self.paused.get(key, 0.0):
            self.paused[key], self.paused_why[key] = until, why
        s.state = "waiting"

    # -- after a crash: helpers' agent CLIs the last run left running, and claims its helpers left

    def _running_procs(self, add: tuple | None = None, drop: int | None = None) -> dict:
        """The agent CLIs this app started and that haven't ended (agents/running.json: pid -> [agent, program]),
        kept so the next start can stop what a crash left running."""
        path = self.home / "agents" / "running.json"
        with self._lock:
            try:
                procs = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                procs = {}
            if add is not None:
                procs[str(add[0])] = [add[1], add[2]]
            if drop is not None:
                procs.pop(str(drop), None)
            if add is not None or drop is not None:
                try:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(json.dumps(procs), encoding="utf-8")
                except OSError:
                    pass
            return procs

    def stop_strays(self) -> list[str]:
        """At start: agent CLIs the last run left running (it crashed, or was killed) are stopped, so they don't keep
        working, and spending, beside the helpers that resume. One is stopped only while it's still its own process
        group and still that program (a pid can be reused). Then the claims this computer's helpers held go back:
        the ones that resume claim again. Returns the stray helpers' names."""
        import signal

        stopped = []
        procs = self._running_procs()
        for pid_s, (name, program) in procs.items():
            pid = int(pid_s)
            if WIN or not program:
                continue
            try:
                if os.getpgid(pid) != pid:
                    continue
                args = subprocess.run(["ps", "-o", "args=", "-p", str(pid)], capture_output=True, text=True,
                                      timeout=5).stdout
            except (OSError, ProcessLookupError, subprocess.SubprocessError):
                continue
            if program not in args:
                continue
            try:
                os.killpg(pid, signal.SIGTERM)
                stopped.append(name)
            except OSError:
                pass
        for pid_s in procs:
            self._running_procs(drop=int(pid_s))
        with self.net.store.tx() as db:  # (an agent of the user's own, connected from outside, keeps its claim)
            db.execute("DELETE FROM claims WHERE agent_id IN (SELECT id FROM agents WHERE origin='local')")
        return stopped

    def _free_claims(self, s: Session, agent) -> None:
        """Give back whatever the helper's agent still holds, so others can take it at once (not the function's fault:
        no abandon counted). In a room the host holds the real claim, so it's told too."""
        held = self.net.store.one("SELECT f.name FROM claims c JOIN functions f ON f.id=c.function_id WHERE c.agent_id=?", agent.id)
        link = self.net.links.get(s.slug)
        if held and link is not None:
            try:
                link.call("/collab/release", agent, function=held["name"], notes="")
            except Exception:
                pass
        with self.net.store.tx() as db:
            db.execute("DELETE FROM claims WHERE agent_id=?", (agent.id,))

    # -- a helper pinned to one function

    def _pin_check(self, s: Session, pin: str) -> str:
        """Why a pinned helper is done before its next run ("" if it isn't): its function matched, set aside or gone,
        or `stall` runs without a gain (agents' own loops; the lean loop counts its calls itself)."""
        try:
            fn = self.net._function(pin, s.slug)
        except Exception:
            return f"{pin} isn't in the project any more"
        if fn["status"] == "matched":
            return f"{pin} matched 🎉"
        if fn["status"] != "open":
            return f"{pin} isn't open any more ({fn['status']})"
        stall = int(s.options.get("stall") or 0) or PIN_STALL_RUNS
        if not s.options.get("lean") and s.pin_stall >= stall:
            return f"no closer in {s.pin_stall} runs"
        return ""

    def _pin_best(self, s: Session, pin: str):
        try:
            return self.net._function(pin, s.slug)["best_score"]
        except Exception:
            return None

    def _pin_effort(self, s: Session) -> None:
        """Steering for an agent's own loop: a step up in effort (low, medium, high) from the user's choice every two
        runs without a gain, back to that choice after one; never below it (Claude Code and Codex; a tiered helper's
        tier does this already)."""
        if s.kind not in ("claude", "codex") or s.tiered:
            return
        chosen = s.options.get("base_effort") or ""
        base = chosen or _config_effort(s.kind) or ""
        if s.pin_stall < 2 or (base and base not in EFFORT_LADDER):  # xhigh, max: never a step down
            s.options["effort"] = chosen
            return
        at = EFFORT_LADDER.index(base) if base else 1
        step = EFFORT_LADDER[min(len(EFFORT_LADDER) - 1, at + s.pin_stall // 2)]
        if step != (s.options.get("effort") or ""):
            s.push("info", f"no closer in {s.pin_stall} runs: {step} effort for the next")
        s.options["effort"] = step

    def _pin_after(self, s: Session, pin: str, before) -> None:
        """After an agent's own run on its pinned function: the permuter's turn on the closest source (if wanted),
        and whether the run got it closer."""
        try:
            fn = self.net._function(pin, s.slug)
        except Exception:
            return
        if fn["status"] == "open" and fn["best_source"] and s.options.get("polish"):
            self._polish(s, fn)
            fn = self.net._function(pin, s.slug)
        now = fn["best_score"] if fn["status"] == "open" else 0
        gained = now is not None and (before is None or now < before)
        s.pin_stall = 0 if gained else s.pin_stall + 1

    def _polish(self, s: Session, fn) -> bool:
        """The permuter on a pinned function's closest source, between calls (C): register and order rewrites, at no
        token cost. True when it got closer."""
        permuter = getattr(self.net, "permuter", None)
        if permuter is None:
            return False
        before = fn["best_score"]
        try:
            after = permuter.polish(fn, float(s.options.get("polish_seconds") or POLISH_SECONDS), s.stop)
        except Exception as e:  # a nicety: the helper goes on without it
            s.push("info", f"permuter: {e}")
            return False
        if after is not None and before is not None and after < before:
            s.push("info", f"permuter: score {before} -> {after}")
            return True
        return False

    def tier(self, s: Session) -> tuple[str, str, str, str]:
        """(model, effort, tier, function) for a tiered session's next run: the policy (tier_for) applied to the
        function it is about to claim. Nothing to look at (no function left, a store error): the fresh tier."""
        state, name = {}, ""
        try:
            fn = (self.net._function(s.options["pin"], s.slug) if s.options.get("pin")  # pinned: its function
                  else next_function(self.net, s.slug, s.agent_id))
        except Exception:  # routing is best-effort: a helper still starts
            fn = None
        if fn is not None:
            state, name = {"best_score": fn["best_score"], "n_insns": fn["n_insns"], "attempts": fn["attempts"]}, fn["name"]
        model, effort, tier = tier_for(state)
        return model, effort, tier, name

    def prompt(self, s: Session) -> str:
        text = self._prompt(s)
        if s.options.get("only_starred"):
            text += ("\nThis session is for the functions the user marked ★ Next in line, and only those: "
                     "claim_function gives you the next one, and refuses anything else. When it says none is left, "
                     "stop: the session ends by itself.\n")
        # the know-how every session reads first, given up front (saves each session its first tool calls)
        ov = self.net.overview(s.slug)
        if ov.get("kind") == "fork":
            # CodeWarrior's register and stack rules decide most of what's left on GameCube, Wii and DS
            topics = ["fork", "objdiff", *(["mwcc"] if ov.get("platform") in ("gc", "wii", "nds") else [])]
        else:
            topics = ["workflow", compiler_guide(ov)]
        text += "\nKnow-how you'd otherwise read with guide() (more topics: guide()):\n\n" + "\n\n".join(
            tools._guide(t) for t in topics)
        if (learned := self.net.learnings(s.slug)):
            text += ("\nWhat agents learned on this project so far (compiler quirks, conventions; add yours with "
                     f"add_note and no function):\n{learned}\n")
        if (wish := self.net.instructions(s.slug)):
            text += f"\nThe user's instructions for this project (they come first):\n{wish}\n"
        return text + s.resume_note

    def _prompt(self, s: Session) -> str:
        ov = self.net.overview(s.slug)
        if s.options.get("pin") and ov.get("kind") != "fork":
            return tools.PIN_PROMPT.format(title=ov["title"], name=s.options["pin"], compiler_guide=compiler_guide(ov))
        if ov.get("kind") == "fork":
            guides = ["fork", "objdiff", *tools.PLATFORM_GUIDES.get(ov.get("platform") or "", [])]
            return tools.FORK_PROMPT.format(title=ov["title"], platform=ov.get("platform_name") or "its platform",
                                            repeat=self._repeat(s),
                                            guides=", ".join(f'guide("{g}")' for g in guides))
        if s.options.get("track") == "asm":
            return tools.ASM_PROMPT.format(title=ov["title"], repeat=self._asm_repeat(s),
                                           **tools.asm_words(ov.get("platform") or ""))
        return tools.WORK_PROMPT.format(title=ov["title"], repeat=self._repeat(s), compiler_guide=compiler_guide(ov))

    @staticmethod
    def _asm_repeat(s: Session) -> str:
        if s.kind == "api":  # the app trims this agent's context, so it keeps going like on the C track
            return ("Keep going: claim a routine, document or release it, then claim the next, until claim_function "
                    "says nothing is left")
        return f"Repeat for up to {s.options.get('per_session', PER_SESSION)} routines, then stop"

    @staticmethod
    def _repeat(s: Session) -> str:
        if s.kind == "api":  # the app manages this agent's context, so it just keeps going
            return ("Keep going: claim a function, work it, release or match it, then claim the next "
                    "(claim_function(count=4) is ONE claim) until claim_function says nothing is left")
        return (f"Repeat until you have claimed {s.options.get('per_session', PER_SESSION)} times "
                "(claim_function(count=4) is ONE claim), then stop")

    def _command(self, s: Session, token: str, workdir: Path) -> tuple[list[str], dict, str]:
        """The agent's command line, environment and what goes in on stdin (the prompt)."""
        k = KINDS[s.kind]
        exe = _which(k.command)
        if not exe:
            raise RuntimeError(f"{k.name} is not installed")
        env = dict(os.environ)
        if WIN:
            env["PATH"] = search_path()  # the agent finds node (and what it needs) even when installed after the app
        # An agent that only loads the MCP servers in the user's own settings (Antigravity CLI since 1.1.3 ignores a
        # workspace's .agents/mcp_config.json) reaches decomp.wtf through the `decomp mcp` the user connected it
        # with: that bridge works as this session's agent when it finds this, not as a new agent of the user's
        env["DECOMP_SESSION_TOKEN"] = token
        prompt = self.prompt(s)
        if s.tiered and not s.routed_model:  # started outside the run loop: pick now
            s.routed_model, s.routed_effort, s.tier, s.tier_function = self.tier(s)
        model = (s.routed_model if s.tiered else s.options.get("model")) or ""
        if s.kind == "claude":
            cfg = workdir / "mcp.json"
            cfg.write_text(json.dumps({"mcpServers": {"decomp": {"type": "http", "url": self.mcp_url,
                                                                  "headers": {"Authorization": f"Bearer {token}"}}}}),
                           encoding="utf-8")
            cmd = [exe, "-p", "--output-format", "stream-json", "--verbose", "--mcp-config", str(cfg),
                   "--strict-mcp-config", "--allowedTools", "mcp__decomp", "--disallowedTools",
                   "Bash,Edit,Write,NotebookEdit,WebFetch,WebSearch"]
            if model:
                cmd += ["--model", model]
            effort = s.routed_effort if s.tiered else str(s.options.get("effort") or "")
            if effort in EFFORTS:  # else Claude Code's own default for the model
                cmd += ["--effort", effort]
        elif s.kind == "codex":
            env["DECOMP_TOKEN"] = token
            # The worker's own server, under a name of its own: `-c` settings merge into the user's config, and
            # `decomp connect codex` gave that a stdio `decomp` entry (a url on top of it: "url is not supported for
            # stdio"). The user's MCP servers are switched off for the run, as --strict-mcp-config does for Claude.
            cmd = [exe, "exec", "--json", "--skip-git-repo-check", "--ephemeral", "-C", str(workdir), "-s", "read-only"]
            for name in (_codex_config().get("mcp_servers") or {}):
                if re.fullmatch(r"[A-Za-z0-9_-]+", name):
                    cmd += ["-c", f"mcp_servers.{name}.enabled=false"]
            cmd += ["-c", f'mcp_servers.decomp_agent={{url="{self.mcp_url}", bearer_token_env_var="DECOMP_TOKEN", '
                          'default_tools_approval_mode="approve", tool_timeout_sec=180}',
                    "-c", 'approval_policy="never"']
            if model:
                cmd += ["-m", model]
            effort = str(s.options.get("effort") or "")
            if re.fullmatch(r"[a-z]{2,16}", effort):  # else Codex's own: the user's config, then the model's default
                cmd += ["-c", f'model_reasoning_effort="{effort}"']
            cmd.append("-")  # the prompt comes from stdin
        elif s.kind == "antigravity":
            # the workspace's own MCP config; tool permission comes from the user's rule for decomp's tools (AGY_ALLOW)
            (workdir / ".agents").mkdir(exist_ok=True)
            (workdir / ".agents" / "mcp_config.json").write_text(json.dumps({"mcpServers": {"decomp": {
                "serverUrl": self.mcp_url, "headers": {"Authorization": f"Bearer {token}"}}}}), encoding="utf-8")
            # a session works through several functions: more than agy's default 5 minutes
            cmd = [exe, "--output-format", "stream-json", "--input-format", "stream-json", "--print-timeout", "2h"]
            if model:
                cmd += ["--model", model]
            prompt = json.dumps({"event": "user", "message": {"content": prompt}}) + "\n"
        elif s.kind == "opencode":
            # "deny" takes the tools out of the request, and OpenCode Zen's free models then refuse it ("can only be
            # used from within OpenCode"). "ask" keeps the usual tools, and `opencode run` rejects every ask itself.
            zen = (model or _config_model("opencode") or "opencode/").startswith("opencode/")
            never = "ask" if zen else "deny"
            (workdir / "opencode.json").write_text(json.dumps({
                "$schema": "https://opencode.ai/config.json",
                "mcp": {"decomp": {"type": "remote", "url": self.mcp_url, "enabled": True,
                                   "headers": {"Authorization": f"Bearer {token}"}}},
                "permission": {"edit": never, "bash": never, "webfetch": never},
            }), encoding="utf-8")
            # --print-logs: opencode's JSON error only says "Unexpected server error"; the log line says what failed.
            # (No --dir: it runs in workdir, and OpenCode before 1.4 stops at the flag.)
            cmd = [exe, "run", "--format", "json", "--print-logs", "--log-level", opencode_error_level(exe)]
            if model:
                cmd += ["-m", model]
        else:
            raise RuntimeError(s.kind)
        return argv(cmd), env, prompt

    def _run_cli(self, s: Session, token: str, workdir: Path) -> bool:
        if s.kind == "antigravity" and s.runs == 1:
            from . import connect  # (connect imports this module)

            if not connect.status("antigravity"):  # decomp.wtf's tools only reach it through the user's settings
                raise Fatal("Antigravity CLI only loads the MCP servers in your own settings: connect it to decomp.wtf "
                            "once (Connect your agent, or `decomp connect antigravity`), then start the helpers again")
        cmd, env, prompt = self._command(s, token, workdir)
        s.resume_note = ""  # (told in this prompt)
        s.push("info", f"session {s.runs}: {KINDS[s.kind].name} started")
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if WIN else 0
        proc = subprocess.Popen(cmd, cwd=workdir, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", bufsize=1,
                                creationflags=flags, start_new_session=not WIN)
        s.proc = proc
        self._running_procs(add=(proc.pid, s.agent_name, Path(cmd[0]).name))
        errors: collections.deque = collections.deque(maxlen=20)

        def feed_prompt():
            try:
                proc.stdin.write(prompt)
                proc.stdin.close()  # end of input: the agent works on this one prompt, then exits
            except OSError:  # it exited before reading (stderr says why)
                pass

        def drain_err():
            for line in proc.stderr:
                s.last_output = time.time()
                if (text := clean_stderr(s.kind, line)) and (not errors or errors[-1] != text):
                    errors.append(text)

        # An agent that says nothing for `idle` seconds is ended: Codex sessions were seen sitting an hour at 0% CPU,
        # each holding its function. The next session is told where it was (prompt), and carries on from there.
        idle, silent = agent_idle(), threading.Event()
        s.last_output = time.time()

        def watch():
            while proc.poll() is None and not s.stop.wait(5):
                if time.time() - s.last_output > idle:
                    silent.set()
                    _terminate(proc)
                    return
        threading.Thread(target=feed_prompt, daemon=True).start()
        threading.Thread(target=drain_err, daemon=True).start()
        threading.Thread(target=watch, daemon=True).start()
        saw_tool = False
        said: collections.deque = collections.deque(maxlen=20)
        state: dict = {}
        last_error = refused = ""
        for line in proc.stdout:
            s.last_output = time.time()
            for kind, text in parse_line(s.kind, line, state):
                if kind == "usage":
                    self._account(s, json.loads(text))
                    continue
                saw_tool |= kind == "tool"
                said.append(text)
                if kind == "error":
                    last_error = text
                    refused = refused or (text if text.startswith("Antigravity refused") else "")
                s.push(kind, text)
            if s.stop.is_set():
                _terminate(proc)
                break
        code = proc.wait()
        s.proc = None
        self._running_procs(drop=proc.pid)
        s.last_output = 0.0
        if silent.is_set() and not s.stop.is_set():
            held = self.net.store.one("SELECT f.name, f.best_score, f.n_insns FROM claims c JOIN functions f ON "
                                      "f.id=c.function_id WHERE c.agent_id=?", s.agent_id) if s.agent_id else None
            if held is not None:
                best = ("nothing compiled yet" if held["best_score"] is None else
                        f"best score so far {held['best_score']} of {max(1, held['n_insns'] or 0) * 100}")
                s.resume_note = (f"\nYour previous session said nothing for {_span(idle)} and was ended while "
                                 f"it held {held['name']} ({best}). claim_function gives it back to you: carry on from "
                                 "the best attempt, or release it with notes if it's stuck.\n")
            s.error = f"{KINDS[s.kind].name} said nothing for {_span(idle)}: ended it; the next session carries on"
            s.push("error", s.error)
            return False
        if refused and not s.stop.is_set():
            raise Fatal(refused)  # every retry would be refused the same way
        if code != 0 and not s.stop.is_set() and (why := no_credit(" ".join([*said, *errors]))):
            raise Fatal(why)  # every agent on this account fails the same way until it's topped up
        if code != 0 and not s.stop.is_set() and (limit := plan_limit(" ".join([*said, *errors]))):
            raise limit  # every helper on this account waits for the reset
        if code != 0 and not s.stop.is_set() and _THROTTLED.search(" ".join([*said, *errors])):
            s.throttled = True
        if code != 0 and not s.stop.is_set() and _SIGNED_OUT.search(" ".join([*said, *errors])):
            s.signed_out = True
            s.error = f"{KINDS[s.kind].name} isn't signed in on this computer. {signin(KINDS[s.kind])}"
            s.push("error", s.error)
            return False
        if code != 0 and not s.stop.is_set():
            tail = " · ".join(list(errors)[-3:]) or last_error or f"exited with code {code}"
            s.error = tail[:300]
            s.push("error", f"exited with code {code}: {tail[:600]}" if tail != f"exited with code {code}" else tail)
            return False
        if not saw_tool and not s.stop.is_set():
            s.error = "the agent never used the decomp tools" + (f": {list(errors)[-1]}" if errors else "")
            s.push("error", s.error)
            return False
        s.error = ""
        return True

    # -- built-in agent for OpenAI-compatible APIs

    def _run_lean(self, s: Session, agent) -> bool:
        """A run of the lean loop (see lean.py): up to LEAN_CALLS calls. Pinned, on its one function (s.pin_done says
        when it's done); otherwise on each function it claims the usual way (focus, ★ Next in line, easiest first) in
        turn, moving on, with a note, when one stops getting closer. What it tried on a function, and the repeats it
        knows, carry over from run to run."""
        from . import lean

        if s.kind == "api" and (not s.options.get("base_url") or not s.options.get("model")):
            raise RuntimeError("set the API address and model first")
        max_tokens = int(s.options.get("max_tokens") or LEAN_MAX_TOKENS)
        pin = s.options.get("pin") or ""
        before = self._progress(agent.id)
        with httpx.Client(timeout=httpx.Timeout(API_TIMEOUT, connect=15)) as client:
            s.client = client  # store on session for stop() to close
            def ask(messages, temperature, switch):
                if s.kind == "codex":  # Codex picks its own sampling: the versions' directions do the varying
                    text, tokens, model = codex_reply(messages, s.options.get("model") or "", s.options.get("effort") or "",
                                                      self.home / "agents" / s.id / "lean")
                else:
                    opts = s.options.get("escalate") if switch and s.options.get("escalate") else s.options
                    several = "one complete version" not in messages[-1]["content"]  # more room for several
                    text, tokens, model = _chat(s, client, opts, messages, temperature, max_tokens * (2 if several else 1))
                self._account(s, {"tokens": tokens})
                return text, tokens, model

            def stamp(method, tokens, model):
                s.method, s.call_tokens, s.using_model = method, tokens, model

            def loop_on(name: str):
                return lean.Lean(self.net, agent, s.slug, name, ask, method=s.options.get("lean") or lean.SEVERAL,
                                 variants=int(s.options.get("variants") or lean.VARIANTS),
                                 stall=int(s.options.get("stall") or (lean.STALL if pin else LEAN_MOVE_ON)),
                                 push=s.push, stamp=stamp,
                                 polish=(lambda fn: self._polish(s, fn)) if s.options.get("polish") else None,
                                 can_switch=bool(s.options.get("escalate")),
                                 interrupt=lambda: s.stop.is_set())

            s.push("info", f"session {s.runs}: {s.options.get('model') or 'default model'}"
                           + (f" via {s.options.get('base_url')}" if s.kind == "api" else f" ({KINDS[s.kind].name})"))
            if pin:
                if s.lean is None:
                    s.lean = loop_on(pin)
                for wait in range(13):  # the function first (the permuter's turn on it lasts a minute at most)
                    try:
                        self.net.claim_row(agent, s.slug, pin)
                        break
                    except NetworkError as e:
                        if "claimed by" not in str(e):
                            break  # matched or set aside meanwhile: the loop's first call says so
                        if wait == 0:
                            s.push("info", f"waiting for it: {e}")
                        if s.stop.wait(10):
                            return True
            for _ in range(LEAN_CALLS):
                if s.stop.is_set():
                    break
                if s.lean is None:  # the next function, the way agents claim theirs
                    try:
                        fn = self.net.claim_row(agent, s.slug, None, self.net.focus.get(agent.id) or "easiest")
                    except NetworkError as e:  # (★ only: none of those left for it)
                        s.pin_done = str(e)
                        break
                    if fn is None:
                        s.pin_done = "nothing left to claim here 🎉"
                        break
                    s.lean = loop_on(fn["name"])
                    s.push("info", f"on {fn['name']} ({fn['n_insns']} instructions)")
                loop = s.lean
                loop.ask, loop.stamp = ask, stamp  # this run's connection
                done = loop.round()
                if not done:
                    continue
                note = loop.summary() if "matched" not in done else ""
                if pin:
                    s.pin_done = done
                    if note:
                        self._note(agent, s.slug, note, pin)
                    break
                s.push("info", done)
                s.lean = None
                if "matched" not in done:  # moving on: the next agent finds what this one tried
                    try:
                        self.net.release(agent, loop.name, note or "no closer", s.slug)
                    except NetworkError:
                        pass
        return self._progress(agent.id) != before or bool(s.pin_done)

    def _routine_clock(self, s: Session, agent) -> tuple[int, str] | None:
        """The routine an API helper holds, as (id, name), its clock started when it first holds it (ROUTINE_BUDGET from
        then: s.routine_until). None when it holds none."""
        row = self.net.store.one("SELECT f.id, f.name FROM claims c JOIN functions f ON f.id=c.function_id "
                                 "WHERE c.agent_id=? ORDER BY c.function_id LIMIT 1", agent.id)
        if row is None:
            s.routine_id, s.routine_until = 0, 0.0
            return None
        if row["id"] != s.routine_id:
            s.routine_id, s.routine_until = row["id"], time.time() + routine_budget()
        return row["id"], row["name"]

    def _out_of_time(self, s: Session, agent, function_id: int, name: str) -> str:
        """A routine whose time is up: the first time it goes to the back of the queue with a note; the second, it's
        marked stuck (red on the map) until a person unlocks it. What to tell the model."""
        span = _span(routine_budget())
        s.routine_id, s.routine_until = 0, 0.0
        s.timed_out.add(name)
        before = self.net.store.one("SELECT COUNT(*) AS n FROM notes WHERE function_id=? AND text LIKE ?",
                                    function_id, OUT_OF_TIME + "%")["n"]
        if before:
            self.net.give_up_stuck_function(agent.id)
            s.push("info", f"out of time on {name} again ({span}): marked it stuck, moving on")
            return (f"Time's up on {name}: {span} per routine, and it ran out a second time, so it's marked stuck for "
                    "a person to look at. Leave it and claim_function for the next routine.")
        try:
            self.net.release(agent, name, f"{OUT_OF_TIME} {span} on it without documenting it; the next try starts "
                                          "from the best attempt and these notes", s.slug)
        except NetworkError:
            pass
        s.push("info", f"out of time on {name} ({span}): back to the queue, moving on")
        return (f"Time's up on {name}: {span} per routine. It's released to the back of the queue with a note. Don't "
                "go on with it: claim_function for the next routine.")

    def _server_failed(self, s: Session, agent, held: tuple[int, str] | None) -> str:
        """The model server failed mid-conversation: the routine held goes back to the queue (no strike towards
        stuck), and what to tell the model as it starts over."""
        s.routine_id, s.routine_until = 0, 0.0
        if held is None:
            return "The model server failed on the last request, so this conversation starts over. Carry on: claim_function."
        try:
            self.net.release(agent, held[1], "The model server failed while this was being worked on; the next try "
                                             "starts from the best attempt and these notes", s.slug)
        except NetworkError:
            pass
        return (f"The model server failed while you were on {held[1]}, so this conversation starts over and {held[1]} "
                "is back in the queue. claim_function for the next routine.")

    def _note(self, agent, slug: str, text: str, function: str) -> None:
        try:
            self.net.add_note(agent, slug, text, function)
        except Exception:  # a nicety: the helper goes on without it
            pass

    def _run_api(self, s: Session, agent) -> bool:
        """The agent loop for any OpenAI-compatible API. The conversation is kept within the model's context
        (`context_tokens`, or what a local llama.cpp server says it has): older tool output is trimmed first, and
        when the server still says it's too long, trimmed harder and sent again."""
        base = (s.options.get("base_url") or "").rstrip("/")
        model = s.options.get("model") or ""
        if not base or not model:
            raise RuntimeError("set the API address and model first")
        headers = {"Content-Type": "application/json"}
        if s.options.get("api_key"):
            headers["Authorization"] = f"Bearer {s.options['api_key']}"
        one_at_a_time = is_local(base)  # local models take one function at a time, not a batch
        asm = s.options.get("track") == "asm"
        schema = api_tool_schemas(fork=bool(self.net.fork_slug(s.slug)), single=one_at_a_time, asm=asm,
                                  **self._console(s.slug))
        task = self.prompt(s)
        if one_at_a_time:
            task = task.replace("claim_function(count=4)", "claim_function()")
        messages = [{"role": "system", "content": tools.ASM_INSTRUCTIONS if asm else tools.INSTRUCTIONS},
                    {"role": "user", "content": task}]
        used_tool = server_failed = idle_timeout = claimed = False
        with httpx.Client(timeout=httpx.Timeout(API_TIMEOUT, connect=15)) as client:
            s.client = client  # store on session for stop() to close
            window = context_window(client, base, s.options)
            s.push("info", f"session {s.runs}: {model} via {base} ({window // 1000}K tokens of context)")
            budget = int(window * 0.7) - _tokens(schema)  # room for the reply and the tools' definitions
            for _step in itertools.count() if not s.options.get("max_steps") else range(int(s.options["max_steps"])):
                if s.stop.is_set():
                    return True
                held = self._routine_clock(s, agent)
                if held is not None and time.time() >= s.routine_until - ROUTINE_SLACK:
                    messages.append({"role": "user", "content": self._out_of_time(s, agent, *held)})
                    continue
                left = s.routine_until - time.time() if held is not None else API_TIMEOUT
                body = {"model": model, "messages": messages, "tools": schema, "tool_choice": "auto"}
                if cap := reply_cap(s, left):
                    body["max_tokens"] = cap
                s.log("request", step=_step, routine=held[1] if held else None, seconds_left=round(left, 1),
                      max_tokens=body.get("max_tokens"), rate=round(s.tok_rate, 2), messages=len(messages),
                      last=messages[-1])
                timed_out = False
                for _attempt in range(4):
                    used = fit(messages, budget)
                    s.asking_since = asked = time.time()
                    try:
                        r = _post(s, client, f"{base}/chat/completions", headers=headers, json=body,
                                  timeout=httpx.Timeout(min(API_TIMEOUT, left), connect=min(15.0, left)))
                    except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as e:
                        s.log("no_reply", seconds=round(time.time() - asked, 1), why=repr(e))
                        if s.stop.is_set():
                            return True
                        if held is None:
                            if idle_timeout:  # twice in a row with nothing held: the server isn't answering
                                raise
                            idle_timeout = timed_out = True
                            break
                        timed_out = True  # its time is up mid-reply: no resend, the routine is put by (_out_of_time)
                        break
                    finally:
                        s.asking_since = 0.0
                    if r.status_code == 200 or not _too_long(r):
                        break
                    budget = int(budget * 0.6)  # the server counts differently, or knows better: trim more
                if timed_out:
                    if held is None:
                        s.push("info", f"no reply within {_span(left)} between routines: dropped, told the model to "
                                       "claim the next one")
                        messages.append({"role": "user", "content": IDLE_TIMEOUT})
                    else:
                        messages.append({"role": "user", "content": self._out_of_time(s, agent, *held)})
                    continue
                idle_timeout = False
                if r.status_code != 200:
                    s.log("api_error", status=r.status_code, body=r.text)
                    if (why := no_credit(r.text)) or r.status_code == 402:
                        raise Fatal(why or no_credit("payment required"))
                    if r.status_code >= 500 and not _too_long(r) and not server_failed:
                        # the model server failed on this conversation (a bad tool call it can't read back, a crash):
                        # once, start the conversation over (the routine, if any, back to the queue); twice in a row, stop
                        server_failed = True
                        s.push("info", f"the model server failed ({r.status_code}): {_brief_error(r.text)}; starting "
                                       "the conversation over")
                        messages[:] = messages[:2] + [{"role": "user", "content": self._server_failed(s, agent, held)}]
                        continue
                    s.throttled = r.status_code == 429 or bool(_THROTTLED.search(r.text))
                    s.error = f"API error {r.status_code}: {r.text[:300]}"
                    if _too_long(r):
                        s.error = ("the model's context is too small for this work even after trimming: give it at "
                                   "least 16K tokens (llama.cpp: -c 16384; Ollama: OLLAMA_CONTEXT_LENGTH=16384; LM "
                                   "Studio: Context Length) and set it in Agents & models")
                    s.push("error", s.error)
                    return False
                server_failed = False
                reply = r.json()
                msg = reply["choices"][0]["message"]
                s.log("reply", seconds=round(time.time() - asked, 1), finish=reply["choices"][0].get("finish_reason"),
                      usage=reply.get("usage"), message=msg)
                usage = reply.get("usage") if isinstance(reply.get("usage"), dict) else {}
                if usage.get("total_tokens"):
                    self._account(s, {"tokens": usage["total_tokens"]})
                measure_rate(s, usage.get("completion_tokens"), time.time() - asked)
                if msg.get("reasoning_content"):
                    s.push("think", msg["reasoning_content"][:2000])
                if msg.get("content"):
                    s.push("text", msg["content"])
                calls = msg.get("tool_calls") or []
                if broken := [c for c in calls if not _parses(c)]:
                    # a tool call cut off (its reply hit its cap) or garbled: not run, and not kept, since the server
                    # reads every earlier call back and fails on one that isn't JSON
                    names = ", ".join(sorted({(c.get("function") or {}).get("name") or "?" for c in broken}))
                    s.push("info", f"a tool call ({names}) came back cut off or not valid JSON: not run; told the model "
                                   "to send it again, shorter")
                    if msg.get("content"):
                        messages.append({"role": "assistant", "content": msg["content"]})
                    messages.append({"role": "user", "content": BROKEN_CALL.format(names=names)})
                    continue
                if not calls and reply["choices"][0].get("finish_reason") == "length":
                    # it thought until its reply cap: its time on the routine is short, so tell it to act, don't wait
                    room = f"its cap ({body['max_tokens']} tokens)" if "max_tokens" in body else "the server's cap"
                    s.push("info", f"the reply hit {room} while the model was still thinking: told it to make its "
                                   "next move")
                    if msg.get("content"):
                        messages.append({"role": "assistant", "content": msg["content"]})
                    messages.append({"role": "user", "content": CAPPED})
                    continue
                messages.append({k: v for k, v in msg.items() if k in ("role", "content", "tool_calls")} | {"role": "assistant"})
                if not calls:
                    return used_tool
                for c in calls:
                    name = c["function"]["name"]
                    try:
                        args = json.loads(c["function"].get("arguments") or "{}")
                    except ValueError:
                        args = {}
                    used_tool = True
                    if one_at_a_time and name == "claim_function":
                        args.pop("count", None)
                    s.push("tool", f"{name}({_args_brief(args)})")
                    gone = str(args.get("function") or "")
                    if name in TAKES_A_ROUTINE and gone in s.timed_out:
                        result = TIMED_OUT_ROUTINE.format(name=gone)  # not run: its 5 minutes are spent
                    else:
                        result = tools.call(self.net, agent, s.slug, name, args)
                    s.log("tool_result", name=name, args=args, result=result)
                    messages.append({"role": "tool", "tool_call_id": c["id"], "content": result})
                    if name == "claim_function" and result.startswith("## CLAIMED"):
                        if claimed:
                            # each routine gets a fresh conversation: the brief, then this claim. What earlier
                            # routines taught is in the notes and the project's names, and a short prompt stays fast
                            messages[:] = messages[:2] + [
                                {"role": "assistant", "content": "", "tool_calls": [c]},
                                {"role": "tool", "tool_call_id": c["id"], "content": result}]
                            s.log("fresh", routine=_claimed_name(result))
                            break  # any other calls in that reply belonged to the old conversation
                        claimed = True
                if used > budget:  # nothing left to trim: the next session starts fresh with the project's notes
                    return True
        return used_tool


AGENT_LOGS = 200  # helper runs kept in logs/agents, the newest
TAKES_A_ROUTINE = ("compile_and_diff", "claim_function")  # tools that would take a routine back
TIMED_OUT_ROUTINE = ("Not run: your time on {name} is up and it's been put by. Don't go back to it: claim_function() "
                     "with no function for the next routine.")
IDLE_TIMEOUT = ("Your last reply took too long and was dropped. Keep replies short: claim_function for the next "
                "routine.")


def _claimed_name(result: str) -> str:
    m = re.match(r"## CLAIMED[^:]*: \S+?/(\S+)", result)
    return m.group(1) if m else ""


def _safe_name(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", text or "")[:60] or "_"


AGENT_IDLE = 15 * 60  # an agent CLI that says nothing this long is ended (DECOMP_AGENT_IDLE, in seconds, changes it)


def agent_idle() -> float:
    return float(os.environ.get("DECOMP_AGENT_IDLE") or AGENT_IDLE)


def _span(seconds: float) -> str:
    return f"{int(seconds // 60)} minutes" if seconds >= 120 else f"{int(seconds)} seconds"


API_TIMEOUT = float(os.environ.get("DECOMP_API_TIMEOUT") or 300)  # seconds one reply may take (a slow box, a long think)
API_RESENDS = 0  # a request sent again after a timeout or a dropped connection: never (it gets stuck the same way)
ROUTINE_BUDGET = 300  # seconds an API helper spends on one routine, every request in all (DECOMP_ROUTINE_BUDGET)
ROUTINE_SLACK = 10  # seconds: less left than this, and the routine's time is up (no request worth sending)
OUT_OF_TIME = "Out of time:"  # how the note a routine is released with starts: the second time, it's marked stuck
BROKEN_CALL = ("Your call to {names} was cut off before its arguments were complete (or they weren't valid JSON), so "
               "it wasn't run. Send it again, shorter: keep the reasoning out of the arguments.")


def _parses(call: dict) -> bool:
    try:
        json.loads((call.get("function") or {}).get("arguments") or "{}")
        return True
    except ValueError:
        return False


def _brief_error(text: str) -> str:
    """An error body's message, short."""
    try:
        e = json.loads(text).get("error")
        text = e.get("message", text) if isinstance(e, dict) else str(e or text)
    except (ValueError, AttributeError):
        pass
    return " ".join(text.split())[:160]


CAPPED = ("You've used up this reply's room while still thinking, and your time on this routine is short. Stop working "
          "it out: make your next tool call now (compile_and_diff with your best version, or a short read_data), "
          "and think in a few lines.")


def routine_budget() -> float:
    return float(os.environ.get("DECOMP_ROUTINE_BUDGET") or ROUTINE_BUDGET)


def measure_rate(s: Session, completion_tokens, seconds: float) -> None:
    """The model's speed in tokens a second, from a reply: what it wrote over how long the request took (reading the
    prompt included, so it errs slow). Smoothed over replies."""
    if not completion_tokens or seconds < 1:
        return
    rate = float(completion_tokens) / seconds
    s.tok_rate = rate if not s.tok_rate else 0.7 * s.tok_rate + 0.3 * rate


REPLY_SHARE = 0.6  # of the time left a reply's cap is sized for: the model slows as the prompt grows, and a prompt
#                    the server has to read again (not cached) takes time the measured speed doesn't show


def reply_cap(s: Session, seconds_left: float) -> int | None:
    """A reply's max_tokens: what the model writes, at its measured speed, in REPLY_SHARE of the time left (a set
    max_tokens is the most). None before its speed is known and nothing is set: the request's timeout bounds it."""
    most = int(s.options.get("max_tokens") or 0)
    if not s.tok_rate:
        return most or None
    cap = max(256, int(s.tok_rate * max(seconds_left, 0) * REPLY_SHARE))
    return min(cap, most) if most else cap
API_RESEND_WAIT = 5.0  # seconds before the first resend (then twice, three times that)


def _until_stopped(s: Session, call):
    """``call()``, given up when the helper is stopped: Stop ends a request waiting on the model at once (closing the
    client doesn't interrupt a reply being waited for). The request itself ends on its own timeout."""
    box: dict = {}

    def run():
        try:
            box["r"] = call()
        except BaseException as e:  # handed to the caller's thread
            box["e"] = e

    t = threading.Thread(target=run, daemon=True, name="model-request")
    t.start()
    while True:
        t.join(0.2)
        if not t.is_alive():
            break
        if s.stop.is_set():
            raise httpx.ReadError("the helper was stopped")
    if "e" in box:
        raise box["e"]
    return box["r"]


def _post(s: Session, client, url: str, **kw):
    """POST to a model's server; after a timeout or a dropped connection the same request again, up to API_RESENDS
    times: a slow model or a flaky box on the network isn't a failed session."""
    for attempt in range(API_RESENDS + 1):
        try:
            return _until_stopped(s, lambda: client.post(url, **kw))
        except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as e:
            if attempt == API_RESENDS or s.stop.is_set():
                raise
            s.push("info", f"no reply from the model ({type(e).__name__}): sending it again ({attempt + 1} of {API_RESENDS})")
            s.stop.wait(API_RESEND_WAIT * (attempt + 1))


LEAN_MOVE_ON = 4  # a lean helper's calls without a gain before it moves on to its next function (not pinned)
LEAN_CALLS = 20  # lean calls per run (between runs the session checks its budget, a room's host...)
LEAN_MAX_TOKENS = 4096  # a reply's cap for one version (a reasoning model thinks inside it too)


def _chat(s: Session, client, opts: dict, messages: list[dict], temperature: float, max_tokens: int) -> tuple[str, int, str]:
    """One chat completion, no tools: (the reply's text, the tokens it used, the model that answered)."""
    base = (opts.get("base_url") or "").rstrip("/")
    headers = {"Content-Type": "application/json"}
    if opts.get("api_key"):
        headers["Authorization"] = f"Bearer {opts['api_key']}"
    r = _post(s, client, f"{base}/chat/completions", headers=headers,
              json={"model": opts.get("model"), "messages": messages, "temperature": temperature,
                    "max_tokens": max_tokens})
    if r.status_code != 200:
        if (why := no_credit(r.text)) or r.status_code == 402:
            raise Fatal(why or no_credit("payment required"))
        s.throttled = r.status_code == 429 or bool(_THROTTLED.search(r.text))
        raise RuntimeError(f"API error {r.status_code}: {r.text[:300]}")
    reply = r.json()
    choice = reply["choices"][0]
    msg = choice.get("message") or {}
    if msg.get("reasoning_content"):
        s.push("think", msg["reasoning_content"][:2000])
    usage = reply.get("usage") if isinstance(reply.get("usage"), dict) else {}
    tokens = int(usage.get("total_tokens") or 0) or _tokens(messages) + _tokens(msg.get("content") or "")
    if choice.get("finish_reason") == "length" and "```" not in (msg.get("content") or ""):
        s.push("info", f"the reply ran out of room ({max_tokens} tokens) before any C: a reasoning model thinks first; "
                       "raise the reply cap if this repeats")
    return msg.get("content") or "", tokens, str(reply.get("model") or opts.get("model") or "")


def codex_reply(messages: list[dict], model: str, effort: str, workdir: Path, timeout: float = 900) -> tuple[str, int, str]:
    """One lean call through Codex CLI, on its own sign-in (a ChatGPT plan works): `codex exec` with none of
    decomp.wtf's tools and the user's MCP servers off, read-only, in an empty folder, nothing kept. (The reply's last
    message, the tokens the turn used, the model.)"""
    exe = _which("codex")
    if not exe:
        raise RuntimeError("Codex CLI is not installed")
    workdir.mkdir(parents=True, exist_ok=True)
    cmd = [exe, "exec", "--json", "--skip-git-repo-check", "--ephemeral", "-C", str(workdir), "-s", "read-only",
           "-c", 'approval_policy="never"']
    for name in (_codex_config().get("mcp_servers") or {}):
        if re.fullmatch(r"[A-Za-z0-9_-]+", name):
            cmd += ["-c", f"mcp_servers.{name}.enabled=false"]
    if model:
        cmd += ["-m", model]
    if re.fullmatch(r"[a-z]{2,16}", effort or ""):
        cmd += ["-c", f'model_reasoning_effort="{effort}"']
    cmd.append("-")
    prompt = ("\n\n".join(m["content"] for m in messages)
              + "\n\nAnswer in your reply: don't run commands or open files, everything you need is above.")
    env = dict(os.environ)
    if WIN:
        env["PATH"] = search_path()
    proc = subprocess.Popen(argv(cmd), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8", errors="replace", env=env)
    try:
        out, err = proc.communicate(prompt, timeout=timeout)
    except subprocess.TimeoutExpired:
        _terminate(proc)
        raise RuntimeError(f"Codex took longer than {int(timeout)} s on one call") from None
    text, tokens, errors = "", 0, []
    for line in out.splitlines():
        for kind, value in parse_line("codex", line):
            if kind == "text":
                text = value
            elif kind == "usage":
                tokens += int(json.loads(value).get("tokens") or 0)
            elif kind == "error":
                errors.append(value)
    said = " ".join(errors) + " " + (err or "")[-600:]
    if (why := no_credit(said)):
        raise Fatal(why)
    if (limit := plan_limit(said)):
        raise limit
    if _SIGNED_OUT.search(said):
        raise Fatal(f"Codex isn't signed in: {signin(KINDS['codex'])}")
    if not text and (errors or proc.returncode):
        raise RuntimeError(f"Codex: {(errors[-1] if errors else (err or '').strip()[-300:]) or f'exit {proc.returncode}'}")
    return text, tokens, model or _config_model("codex") or "default"


def _tokens(obj) -> int:
    """A cautious token estimate: code and assembly run about 3 characters a token."""
    return len(obj if isinstance(obj, str) else json.dumps(obj)) // 3 + 4


def _too_long(r) -> bool:
    """The server refused the request because it doesn't fit the model's context."""
    return r.status_code in (400, 413, 422, 500) and bool(re.search(
        r"context|too long|exceed|maximum.*tokens|n_ctx|prompt is too|reduce the length", r.text[:2000], re.I))


def fit(messages: list[dict], budget: int) -> int:
    """Shrink the conversation to `budget` tokens: older tool output goes first (a function's assembly and diffs
    from several steps ago), then older reasoning. The instructions, the task and the latest exchange stay whole.
    Returns the estimate after."""
    total = sum(_tokens(m) for m in messages)
    keep_from = max(2, len(messages) - 2)
    for kinds in (("tool",), ("assistant",)):
        for m in messages[2:keep_from]:
            if total <= budget:
                return total
            text = m.get("content")
            if m.get("role") in kinds and isinstance(text, str) and len(text) > 300:
                before = _tokens(m)
                m["content"] = text[:240] + "\n[... trimmed to save context: call the tool again if you need this]"
                total -= before - _tokens(m)
    return total


def context_window(client, base: str, options: dict) -> int:
    """The model's context in tokens: what the user set, else what a llama.cpp server reports (/props), else a
    guess (local servers often run small contexts)."""
    try:
        if int(options.get("context_tokens") or 0) >= 2048:
            return int(options["context_tokens"])
    except (TypeError, ValueError):
        pass
    if is_local(base):
        try:
            r = client.get(re.sub(r"/v1$", "", base) + "/props", timeout=5)
            n = (r.json().get("default_generation_settings") or {}).get("n_ctx") if r.status_code == 200 else None
            if isinstance(n, int) and n >= 2048:
                return n
        except (httpx.HTTPError, ValueError, AttributeError):
            pass
        return 16384
    return 128000


def api_tool_schemas(fork: bool = False, single: bool = False, asm: bool = False, platform: str = "",
                     arch: str = "") -> list[dict]:
    """OpenAI-style tool definitions for the built-in agent, from the same tools CLI agents get: an assembly session
    (asm) gets no C-only tools and words the rest for assembly, and a project's console (platform, arch) keeps only
    the tools that work on it."""
    s = lambda **props: {"type": "object", "properties": props, "required": [k for k, v in props.items() if v.pop("req", False)]}
    st = lambda desc, req=False: {"type": "string", "description": desc, "req": req}
    it = lambda desc: {"type": "integer", "description": desc}
    defs = {
        "project_info": ("Which game, compiler and progress.", s()),
        "claim_function": ("Claim the next function (or one by name/address). Returns assembly, data, best attempt, "
                           "notes and a draft." + ("" if single else " count > 1: a batch of small functions to judge together."),
                           s(function=st("optional name or address"), strategy=st("finish | easiest | closest | hardest | random"),
                             count=it("up to 6 small functions at once"),
                             track=st("asm (Game Boy, Master System, NES): a hand-written routine to document in assembly"))),
        "get_function": ("Show any function by name or address (a long one's assembly in parts: start_line).",
                         s(function=st("name or address", True), start_line=it("assembly line to show from"))),
        "explain_difference": ("Compare two C attempts with the selected function's pinned GCC 2.x compiler and unit context. "
                               "Returns the first observed RTL dump difference; does not record matches.",
                               s(function=st("function", True), left=st("attempt A C source", True),
                                 right=st("attempt B C source", True))),
        "compile_and_diff": ("Compile C for the function, link it at its address and diff against the ROM. "
                             "Score 0 = matched. Several functions: names comma-separated, all definitions in source.",
                             s(function=st("function name, or several: a, b, c", True), source=st("C source", True))),
        "release_function": ("Give up a function with notes for the next agent.",
                             s(function=st("function", True), notes=st("what you tried, what is left", True))),
        "get_context": ("The shared header included in every compile.", s()),
        "edit_context": ("Replace `old` (exactly once) with `new` in the shared header, or append when old is empty.",
                         s(old=st("text to replace, or empty", True), new=st("replacement", True),
                           message=st("why", True))),
        "rename_symbol": ("Give a function or data address a real name.",
                          s(old=st("current name", True), new=st("new name", True), message=st("why"))),
        "split_function": ("Split a merged function at an address.",
                           s(function=st("function", True), at=st("address like 0x80001234", True), new_name=st("name"))),
        "flag_for_review": ("Set a function aside for a person (wrong split, data taken for code, code no C writes): "
                            "say why. Just hard? release_function instead.",
                            s(function=st("function", True), reason=st("what a person should look at", True))),
        "merge_function": ("Join a function with its neighbour when the split cut one routine in two.",
                           s(function=st("function", True))),
        "create_function": ("C64: add a routine the function finder missed, starting at `at`; say how it's reached.",
                            s(at=st("address of its first instruction, like $1A00", True),
                              reason=st("how it's reached, e.g. jump table at $1F00, entry 3", True), new_name=st("name"))),
        "delete_function": ("C64: remove a routine that isn't code; its bytes are kept as data. Say what they are.",
                            s(function=st("function", True), reason=st("what the bytes are instead of code", True))),
        "correct_split": ("Game Boy, Master System: tell the split what it got wrong (used when the game is split again): kind data | "
                          "code | table | operands | noreturn, at a function, NN:AAAA or an address, count (bytes, "
                          "entries), and how you know.",
                          s(kind=st("data | code | table | operands | noreturn", True),
                            at=st("function, NN:AAAA (bank:address), or address", True),
                            reason=st("how you know, a sentence", True), count=it("bytes / entries"),
                            function=st("function whose bank `at` is in"))),
        "read_data": ("Hex dump of the original's data at an address or symbol.", s(what=st("address or symbol", True),
                                                                                    length=it("bytes, up to 512"))),
        "list_functions": ("List functions by status.", s(status=st("open | matched | all"), limit=it("max rows"))),
        "add_note": ("Leave a note for other agents.", s(text=st("note", True), function=st("function"))),
        "search_notes": ("Search notes.", s(query=st("text", True))),
        "find_similar": ("Functions that look like this one, matched ones first, with the best one's source.",
                         s(function=st("function", True), limit=it("how many"))),
        "guide": ("Decompilation know-how; no topic lists topics.", s(topic=st("topic"))),
        "read_file": ("Read a project file with line numbers.", s(path=st("path from the project root", True),
                                                                 start=it("first line"), count=it("lines"))),
        "search_code": ("Regex search over the project's C/C++ sources and headers.",
                        s(pattern=st("regular expression", True), glob=st("optional file glob"))),
        "edit_file": ("Replace `old` (exactly once) with `new` in a source/header file, or append when old is empty. "
                      "Undone if any match breaks.", s(path=st("file", True), old=st("text to replace, or empty", True),
                                                       new=st("replacement", True), message=st("why", True))),
        "file_status": ("How far a source file (or a function's file) is from being linked: functions left, its data "
                        "next to the original's, why the last link failed.", s(path=st("file or function", True))),
        "split_file": ("Divide a source file at a function: it and the rest of the file's code (and data from the given "
                       "offsets) go to a new file. Kept only if the game still builds identically.",
                       s(path=st("file", True), at=st("function name or .text offset", True),
                         new_name=st("new file path (optional)"), data=st("e.g. .rodata:0x88 .data:0x28 (optional)"))),
    }
    if asm:
        console = tools.asm_words(platform)["console"] if platform else "Game Boy, Master System, NES, C64"
        defs["claim_function"] = (defs["claim_function"][0], s(
            function=st("optional name or address"), strategy=st("finish | easiest | closest | hardest | random"),
            count=it("up to 6 small routines at once"),
            track=st(f"asm ({console}): a hand-written routine to document in assembly")))
        defs["compile_and_diff"] = ("Assemble your routine at its address and compare its bytes with the game's; the "
                                    "answer lists the checks it still fails. Accepted = documented.",
                                    s(function=st("routine name", True), source=st("the whole routine in assembly", True)))
        defs["get_context"] = ("The shared header: the names and constants every routine can use.", s())
        defs["edit_context"] = ("Replace `old` (exactly once) with `new` in the shared header of names and constants, "
                                "or append when old is empty.", defs["edit_context"][1])
    if fork:
        defs["compile_and_diff"] = ("Write one complete function definition into its source file, build it and diff "
                                    "with objdiff. 100% / score 0 = matched.",
                                    s(function=st("function name", True), source=st("the function definition", True)))
    names = set(tools.tool_names(fork))
    return [{"type": "function", "function": {"name": n, "description": d, "parameters": p}}
            for n, (d, p) in defs.items() if n in names and tools.tool_fits(n, asm, platform, arch)]


def _terminate(proc: subprocess.Popen | None = None, client: httpx.Client | None = None) -> None:
    try:
        if proc is not None and proc.poll() is None:
            if WIN:
                # the agent with everything it started (node starts opencode.exe, ...): terminate() would only end
                # the first process and leave the rest working, and spending, in the background
                res = subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, timeout=30)
                if res.returncode != 0:
                    proc.terminate()
            else:
                os.killpg(proc.pid, 15)
    except (OSError, ProcessLookupError, subprocess.TimeoutExpired):
        pass
    if client is not None:
        try:
            client.close()
        except Exception:
            pass
