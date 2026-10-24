"""A helper pinned to one function, getting it closer with every call: the lean loop.

What an agent program sends its model on every turn is mostly itself: its instructions, the tools' definitions and a
conversation that grows with every compile (one project's helpers averaged about 109,000 tokens an attempt). This
loop asks for the function only. Each call is one short prompt, laid out so the part that stays the same comes first
(a provider's prompt cache, or llama.cpp's, keeps it): the rules, the target, the declarations it uses. Then what
changes: the closest C so far, what still differs (summed up when one difference repeats), and what was tried. The
model answers with C and nothing else; the app compiles it, keeps what's closer, and asks again. No history rides
along, so the prompt doesn't grow.

Methods: several versions per call (each compiled, the best kept: the default), one, or both in turn on the same
function, which is a fair test of the two: same function, same model. Every attempt says which method made it and
what tokens it cost, so the overview's setup comparison shows which gets closer per token. Measured on 10 Game Boy
functions with Codex (gpt-6-luna, high), 6 calls each: three per call judged 3x the versions for 3% more tokens (most
of a Codex call is its own instructions) and got 2 functions closer, one per call none.

A helper works this way pinned to one function (the drawer's "Work on this"), or on function after function as
agents claim them (the launcher's "How it works"), moving on with a note after LEAN_MOVE_ON calls without a gain.

Steering: after calls with no gain, the prompt asks for something different (structure while the function is far
off, registers and order once it's close) and the sampling loosens; after a few, a stronger model takes over if the
user picked one; a gain resets all that. The loop stops at a match, when the function isn't open any more, or after
`stall` calls without a gain, leaving a note with what was tried.
"""

from __future__ import annotations

import re
import threading
from collections import Counter
from dataclasses import dataclass
from typing import Callable

from .. import scoring

ONE, SEVERAL, BOTH = "one", "several", "both"
METHODS = (ONE, SEVERAL, BOTH)
VARIANTS = 3  # versions per call for "several"
STALL = 10  # calls without a gain before the helper stops
SWITCH_AFTER = 3  # calls without a gain before the model to switch to (if any) takes over
TRIED_SHOWN = 12  # entries of the "tried so far" list in a prompt
DIFF_ROWS = 30  # rows of the diff in a prompt

SYSTEM = """\
You make a C function compile to exactly the target machine code, byte for byte. Each message gives the target, the \
declarations you can use, the closest C so far, what still differs and what was tried. Answer with C only, as asked: \
no explanations. Pure C: no inline assembly, no __naked, no pragmas, no #include (context.h is included for you; don't \
repeat its declarations). Keep the function's name and signature unless the target shows they're wrong. Score: lower \
is closer, 0 matches."""

# directions for versions, so they don't come back the same: the shape while a function is far off, the details once
# it's close. A call's versions each get the next ones in turn (the first is the model's own best idea).
STRUCTURE = ("a different loop form (for, while, do-while) or loop variable type",
             "early returns, or a different if/else shape",
             "different types: signed or unsigned, 8 or 16 bits, a pointer instead of an index",
             "one expression split into steps with a temporary, or steps merged into one expression",
             "a helper the target calls written as a call, or a call written inline",
             "a switch instead of if/else chains, or the reverse")
DETAILS = ("the local variables declared in a different order",
           "a temporary variable, or one fewer",
           "a cast, or `x += 1` for `x++` (or the reverse)",
           "statements in another order where the meaning stays the same",
           "a comparison written the other way round (`a < b` as `b > a`, `!= 0` left out)",
           "a different type for one variable (u8, s8, u16, int)",
           "the loop counter counting the other way, or tested at the other end")

_FENCED = re.compile(r"```[ \t]*(?:c|C|cpp|c\+\+|h)?[ \t]*\n(.*?)```", re.S)
_COMMENTS = re.compile(r"//[^\n]*|/\*.*?\*/", re.S)
_IDEA = re.compile(r"^\s*(?://\s*(?:idea\s*:)?\s*(.+?)\s*$|/\*\s*(?:idea\s*:)?\s*(.+?)\s*\*/)", re.I | re.M)
_WORD = re.compile(r"[A-Za-z_]\w*")
_TOKEN = re.compile(r"\$[0-9a-fA-F]+|0x[0-9a-fA-F]+|[A-Za-z_.][\w.]*|-?\d+|\S")
_KEYWORDS = frozenset(
    "auto break case char const continue default do double else enum extern float for goto if inline int long "
    "register restrict return short signed sizeof static struct switch typedef union unsigned void volatile while "
    "_Bool bool define ifdef ifndef endif undef include pragma __at __sfr __banked __nonbanked __critical __interrupt "
    "__sdcccall __z88dk_fastcall __z88dk_callee __z88dk_shortcall __preserves_regs __reentrant __data __code "
    "__xdata __idata __near __far NULL".split())


def method_label(n: int) -> str:
    """How an attempt was asked for, as attempts record it."""
    return "1 per call" if n == 1 else f"{n} per call"


def extract(text: str, name: str, n: int) -> list[str]:
    """Up to `n` C versions from a reply: its ```c blocks (the ones that define `name`, when some do), else the whole
    reply when it reads as the function."""
    blocks = [b.strip() for b in _FENCED.findall(text or "") if b.strip()]
    if not blocks and name in (text or "") and "{" in text:
        blocks = [text.strip()]
    defining = [b for b in blocks if re.search(rf"\b{re.escape(name)}\s*\(", b)]
    return (defining or blocks)[:n]


def idea_of(source: str) -> str:
    """What a version says it changed (its first comment), short."""
    m = _IDEA.search(source or "")
    return " ".join((m.group(1) or m.group(2) or "").split())[:100] if m else ""


def head(source: str, name: str) -> str:
    """What a source has before the function's definition: its own declarations (registers, globals, prototypes)."""
    m = re.search(rf"^[^\n;{{}}]*\b{re.escape(name)}\s*\([^;{{}}]*\)\s*\{{", source or "", re.M)
    return source[:m.start()] if m else ""


def carry(source: str, best: str | None, name: str) -> str:
    """A version that's only the function gets the closest version's own declarations put back in front: models
    often answer with the function alone, and then nothing it uses is declared."""
    if not best or same_code(head(source, name)):
        return source
    before = head(best, name).strip()
    return f"{before}\n\n{source}" if same_code(before) else source  # (comments alone aren't worth carrying)


_PIN = re.compile(r'(\bregister\b[^;{}()=]*?)\s*\b(?:__)?asm(?:__)?\s*\(\s*"[^"]*"\s*\)')


def unpin(source: str) -> str:
    """A version without GCC's register pins (`register u8 n asm("b")`): SDCC has none (they don't compile there),
    and models reach for them on every register difference. The rest of the version is still worth judging."""
    return _PIN.sub(r"\1", source)


def same_code(source: str) -> str:
    """A version without comments and spacing: two that read the same are the same try."""
    return re.sub(r"\s+", "", _COMMENTS.sub("", source or ""))


def differences(rows: list[tuple[str, str, str]], limit: int = 8) -> list[str]:
    """What still differs, summed up: one line per kind of difference, with how often it repeats (`7x: the target
    has b where yours has c`), instructions only one side has, and ones in another order."""
    swaps: Counter = Counter()
    example: dict = {}
    only_target = [t for m, t, _ in rows if m == "<"]
    only_yours = [c for m, _, c in rows if m == ">"]
    moved = sum(1 for m, _, _ in rows if m == "o")
    for m, t, c in rows:
        if m in " <>o":
            continue
        a, b = _TOKEN.findall(t), _TOKEN.findall(c)
        pairs = [(x, y) for x, y in zip(a, b) if x != y] if len(a) == len(b) else []
        if a and b and a[0] == b[0] and 0 < len(pairs) <= 2:
            key = tuple(pairs)
            swaps[key] += 1
            example.setdefault(key, (t, c))
        else:
            swaps[("line", t, c)] += 1
    out = []
    for key, n in swaps.most_common():
        if key[0] == "line":
            out.append(f"- {n}x: the target has `{key[1]}` where yours has `{key[2]}`")
            continue
        t, c = example[key]
        what = ", ".join(f"`{x}` where yours has `{y}`" for x, y in key)
        out.append(f"- {n}x: the target has {what} (as in `{t}` / yours `{c}`)")
    if only_target:
        out.append(f"- {len(only_target)} instruction{'s' if len(only_target) != 1 else ''} only in the target: "
                   + ", ".join(f"`{x}`" for x in only_target[:4]) + (" ..." if len(only_target) > 4 else ""))
    if only_yours:
        out.append(f"- {len(only_yours)} instruction{'s' if len(only_yours) != 1 else ''} only in yours: "
                   + ", ".join(f"`{x}`" for x in only_yours[:4]) + (" ..." if len(only_yours) > 4 else ""))
    if moved:
        out.append(f"- {moved} instruction{'s' if moved != 1 else ''} in another order")
    return out[:limit] + ([f"- ... and {len(out) - limit} more kinds"] if len(out) > limit else [])


def _items(header: str) -> list[str]:
    """A header's top-level items: preprocessor lines, and declarations up to their `;` outside braces."""
    items, cur, depth, i = [], [], 0, 0
    text = header or ""
    while i < len(text):
        if depth == 0 and not "".join(cur).strip() and text[i] == "#":
            j = text.find("\n", i)
            while j != -1 and text[j - 1] == "\\":
                j = text.find("\n", j + 1)
            j = len(text) if j == -1 else j
            items.append(text[i:j].strip())
            i = j + 1
            continue
        if text.startswith("//", i):
            j = text.find("\n", i)
            i = len(text) if j == -1 else j + 1
            continue
        if text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = len(text) if j == -1 else j + 2
            continue
        ch = text[i]
        cur.append(ch)
        depth += ch == "{"
        depth -= ch == "}"
        if ch == ";" and depth == 0:
            items.append("".join(cur).strip())
            cur = []
        i += 1
    if "".join(cur).strip():
        items.append("".join(cur).strip())
    return items


def trim_header(header: str, wanted: set[str]) -> str:
    """The part of context.h a function can use: its type definitions without bodies (short, and what everything
    else is written in), and the declarations that name something in `wanted` (what its assembly, its closest C or
    its draft mention). The whole header is still what compiles; this is what the model reads."""
    items = _items(header)
    types = set()
    for it in items:
        if it.startswith("typedef"):
            names = _WORD.findall(re.sub(r"\{.*\}", " ", it, flags=re.S))
            types.update(names[-1:])
    keep = []
    for it in items:
        if it.startswith("#include"):
            continue
        if it.startswith("typedef") and "{" not in it:
            keep.append(it)
            continue
        words = set(_WORD.findall(re.sub(r'"(?:\\.|[^"\\])*"', " ", it))) - _KEYWORDS - types
        if it.startswith("#define"):
            words = set(_WORD.findall(it)[1:2])
        if words & wanted:
            keep.append(it)
    return "\n".join(keep)


@dataclass
class Tried:
    method: str
    idea: str
    score: int | None  # None: didn't compile
    before: int | None  # the best when it was tried
    note: str = ""  # why it wasn't compiled (a repeat), or what the permuter did


class Lean:
    """One pinned function, one helper: call after call, each meant to get it closer.

    `ask(messages, temperature, switch)` -> (reply text, tokens the call used, model that answered): the helper's
    endpoint, or with `switch` the stronger one the user picked. `stamp(method, tokens, model)` tells the attempts
    about to be recorded what made them. `polish(fn)` (the permuter's turn between calls) -> True when it got closer.
    """

    def __init__(self, net, agent, slug: str, name: str, ask: Callable, *, method: str = SEVERAL,
                 variants: int = VARIANTS, stall: int = STALL, push: Callable = lambda kind, text: None,
                 stamp: Callable = lambda method, tokens, model: None, polish: Callable | None = None,
                 can_switch: bool = False, interrupt: Callable = lambda: False):
        if method not in METHODS:
            raise ValueError(f"method must be one of {', '.join(METHODS)}")
        self.net, self.agent, self.slug, self.name, self.ask = net, agent, slug, name, ask
        self.method, self.variants, self.stall = method, max(2, int(variants)), max(1, int(stall))
        self.push, self.stamp, self.polish, self.can_switch = push, stamp, polish, can_switch
        self.interrupt = interrupt  # callable that returns True when the session should stop immediately
        self.tried: list[Tried] = []
        self.seen: set[str] = set()
        self.since_gain = 0
        self.calls = 0
        self.error = ""  # the last call's compile error, when none of its versions compiled
        self.owed: Counter = Counter()  # tokens of calls that compiled nothing, carried to the method's next attempts
        self.repeated = 0  # versions the last call repeated word for word
        self.turn = 0  # where the next directions start in their list
        self._rows: tuple[str, list] | None = None  # (the best source, its diff)
        self.start_best = None

    # -- state

    def fn(self):
        try:
            return self.net._function(self.name, self.slug)
        except Exception:
            return None

    def percent(self, fn, score: int | None = None) -> str:
        score = fn["best_score"] if score is None else score
        if score is None:
            return "nothing compiled yet"
        return scoring.percent_text(max(0.0, 100 * (1 - score / max(1, (fn["n_insns"] or 1) * 100))))

    def sdcc(self, fn) -> bool:
        """The project compiles with SDCC (GBDK-2020: Game Boy, Master System, NES)."""
        if not hasattr(self, "_sdcc"):
            proj = self.net.store.one("SELECT * FROM projects WHERE id=?", fn["project_id"])
            self._sdcc = str((self.net.cfg(proj).get("compiler") or {}).get("toolchain") or "").startswith("gbdk")
        return self._sdcc

    def count(self) -> int:
        """Versions to ask for in the next call: 1, several, or (both) the two in turn."""
        if self.method == BOTH:
            return 1 if self.calls % 2 == 0 else self.variants
        return 1 if self.method == ONE else self.variants

    # -- the prompt

    def rows(self, fn) -> list | None:
        """The closest source's diff (worked out once per best, without recording an attempt)."""
        src = fn["best_source"]
        if not src:
            return None
        if self._rows is None or self._rows[0] != src:
            self._rows = (src, preview(self.net, fn, src))
        return self._rows[1]

    def stable(self, fn) -> list[str]:
        """The prompt's first part: the same from call to call (cached by the provider when it can)."""
        proj = self.net.store.one("SELECT * FROM projects WHERE id=?", fn["project_id"])
        cfg = self.net.cfg(proj)
        comp = cfg.get("compiler") or {}
        lines = [f"# {fn['name']}: {fn['n_insns']} instructions, {cfg.get('platform') or proj['arch']}, "
                 f"{comp.get('toolchain') or ''} {' '.join(comp.get('flags') or [])}".rstrip(),
                 "", "## Target assembly", "```asm", scoring.compact_asm(self.net._asm_text(fn, proj)), "```"]
        if (comp.get("toolchain") or "").startswith("gbdk"):  # what models reach for first on a register difference
            lines += ["", "SDCC picks the registers itself: register pins (`register u8 n asm(\"b\")`) don't compile "
                          "and inline assembly is refused. Registers follow from the C: the order variables are declared "
                          "and first used, their types, the loop's shape (`do { } while (--n)`, `for`, `while`), "
                          "temporaries."]
        lines += self.net._extra_brief(fn, proj)
        asm = self.net._asm_text(fn, proj)
        wanted = set(_WORD.findall(asm)) | set(_WORD.findall(fn["best_source"] or "")) | set(_WORD.findall(fn["seed"] or ""))
        wanted.add(fn["name"])
        decl = trim_header(self.net._context(proj["id"], fn["unit"])["header"], wanted)
        if decl:
            lines += ["", "## Declarations you can use (from context.h, which is included for you)", "```c", decl, "```"]
        return lines

    def directions(self, fn, n: int) -> list[str]:
        """What each version of the next call should try ("": the model's own best idea). Several versions: the
        first free, the rest each a direction of their own. One version: free while calls gain, a direction once one
        didn't (or repeated an earlier version). Directions go round their list, so calls don't ask for the same."""
        stage = scoring.stage(fn["best_score"], fn["n_insns"], fn["attempts"])
        hints = STRUCTURE if stage in ("fresh", "structural") else DETAILS
        if n == 1 and self.since_gain == 0 and not self.repeated:
            return [""]
        out = [""] if n > 1 else []
        while len(out) < n:
            out.append(hints[self.turn % len(hints)])
            self.turn += 1
        return out

    def steer(self, fn) -> str:
        text = ""
        if self.repeated:
            text = (f"Your last reply repeated {self.repeated} earlier version{'s' if self.repeated != 1 else ''} word "
                    "for word: every version this time must differ from all the versions tried. ")
        if self.since_gain >= 2:
            text += f"The last {self.since_gain} calls got no closer: try something none of the above tried."
            if self.since_gain >= max(4, self.stall // 2):
                text += (" Small changes keep failing: rewrite it from the target assembly rather than from the "
                         "closest version.")
        return text.strip()

    def messages(self, fn, n: int) -> list[dict]:
        lines = self.stable(fn)
        if fn["best_source"]:
            lines += ["", f"## Closest so far: score {fn['best_score']} ({self.percent(fn)} match), the whole source", "```c",
                      fn["best_source"].strip(), "```"]
            rows = self.rows(fn)
            if rows:
                lines += ["", "## What still differs (target | yours)", *differences(rows), "", "```",
                          scoring.render_rows(rows, context=1, max_rows=DIFF_ROWS), "```"]
        elif fn["seed"]:
            lines += ["", "## A machine-made draft (expect wrong types and names)", "```c", fn["seed"].strip(), "```"]
        else:
            lines += ["", "No C for it yet: write it from the assembly."]
        if self.error:
            lines += ["", "## Your last versions didn't compile", "```", self.error[:800], "```"]
        if self.tried:
            lines += ["", "## Tried so far (newest last)"]
            for t in self.tried[-TRIED_SHOWN:]:
                if t.note:
                    lines.append(f"- {t.idea or 'a version'}: {t.note}")
                elif t.score is None:
                    lines.append(f"- {t.idea or 'a version'}: didn't compile")
                else:
                    was = "" if t.before is None else (" (closer)" if t.score < t.before else " (no closer)")
                    lines.append(f"- {t.idea or 'a version'}: score {t.score}{was}")
        if (steer := self.steer(fn)):
            lines += ["", steer]
        whole = ("the whole source: what the closest version declares outside the function (registers, globals) "
                 "as well as the function")
        ways = self.directions(fn, n)
        if n == 1:
            lines += ["", f"Reply with one complete version of {fn['name']} in a ```c block, starting with a one-line "
                          f"// idea: comment: {whole}." + (f" This time try {ways[0]}." if ways[0] else "") + " Nothing else."]
        else:
            plan = "; ".join(f"version {i + 1}: {w or 'your own best idea'}" for i, w in enumerate(ways))
            lines += ["", f"Reply with {n} complete versions of {fn['name']}, each a different idea ({plan}), each in "
                          f"its own ```c block starting with a one-line // idea: comment: {whole}. Nothing else."]
        return [{"role": "system", "content": SYSTEM}, {"role": "user", "content": "\n".join(lines)}]

    # -- a call

    def round(self) -> str | None:
        """One call: why the helper should stop now (matched, not open, no closer for `stall` calls), or None."""
        fn = self.fn()
        if fn is None:
            return f"{self.name} isn't in the project any more"
        if fn["status"] == "matched":
            return f"{self.name} matched 🎉"
        if fn["status"] != "open":
            return f"{self.name} isn't open any more ({fn['status']})"
        if self.start_best is None:
            self.start_best = fn["best_score"] if fn["best_score"] is not None else -1
        n = self.count()
        label = method_label(n)
        switch = self.can_switch and self.since_gain >= SWITCH_AFTER
        temperature = 0.3 if self.since_gain < 2 else 0.7 if self.since_gain < 5 else 1.0
        if self.interrupt():
            return "stopped"
        text, tokens, model = self.ask(self.messages(fn, n), temperature, switch)
        self.calls += 1
        best_before = fn["best_score"]
        sources = extract(text, fn["name"], n)
        ideas = {}  # (the idea is read before declarations are carried in front of it)
        fresh = []
        for src in sources:
            full = carry(src, fn["best_source"], fn["name"])
            ideas[full] = idea_of(src)
            if self.sdcc(fn) and (bare := unpin(full)) != full:
                full = bare
                ideas[full] = f"{idea_of(src)} (its register pins taken out: SDCC has none)"
            key = same_code(full)
            if key in self.seen or key in (same_code(x) for x in fresh):
                self.tried.append(Tried(label, ideas[full], None, best_before, "the same as an earlier version (not compiled)"))
            else:
                fresh.append(full)
        self.repeated = len(sources) - len(fresh)
        if not fresh:
            self.owed[label] += tokens
            if not sources:
                self.tried.append(Tried(label, "", None, best_before, "the reply had no C in it"))
            self.push("info", f"call {self.calls} ({label}): {'no C in the reply' if not sources else 'only repeats'}")
        share = (tokens + self.owed.pop(label, 0)) // max(1, len(fresh))
        errors = []
        best = best_before
        for src in fresh:
            self.seen.add(same_code(src))
            self.stamp(label, share, model)
            res = self.net.attempt(self.agent, fn["name"], src, self.slug)
            self.tried.append(Tried(label, ideas.get(src, ""), res["score"], best))
            if res["matched"]:
                self.push("info", f"call {self.calls} ({label}): matched 🎉")
                return f"{self.name} matched 🎉"
            if res["score"] is None:
                errors.append(res["text"].split("\n\n⏭")[0])  # the error, not the nudge to release (no tools here)
            elif best is None or res["score"] < best:
                best = res["score"]
        self.stamp("", None, "")
        self.error = errors[0] if fresh and len(errors) == len(fresh) else ""
        gained = best is not None and (best_before is None or best < best_before)
        if self.polish is not None and best is not None:
            fn2 = self.fn()
            if fn2 is not None and fn2["status"] == "open" and self.polish(fn2):
                gained = True
                now = self.fn()
                if now is not None and now["status"] == "matched":
                    self.tried.append(Tried("permuter", "the permuter's rewrites", 0, best, "matched"))
                    return f"{self.name} matched 🎉 (the permuter's rewrite of the best)"
                if now is not None:
                    self.tried.append(Tried("permuter", "the permuter's rewrites of the closest (order, temporaries, "
                                                        "casts)", None, best, f"got it to score {now['best_score']}"))
        self.since_gain = 0 if gained else self.since_gain + 1
        if fresh:
            fn3 = self.fn() or fn
            self.push("info", f"call {self.calls} ({label}{', ' + model if model else ''}): "
                              f"{'closer: ' if gained else 'no closer: '}{self.percent(fn3)}"
                              f"{'' if gained else f' ({self.since_gain} without a gain)'}")
        if self.since_gain >= self.stall:
            return f"no closer in {self.stall} calls: stopping at {self.percent(self.fn() or fn)}"
        return None

    def summary(self) -> str:
        """A note for whoever comes next: how far the pinned helper got, and what it tried."""
        fn = self.fn()
        if fn is None:
            return ""
        start = "nothing" if self.start_best in (None, -1) else self.percent(fn, self.start_best)
        ideas = [f"{t.idea} ({'no compile' if t.score is None else t.score})" for t in self.tried[-8:] if t.idea]
        return (f"Pinned helper, {self.calls} calls: from {start} to {self.percent(fn)}."
                + (f" Tried: {'; '.join(ideas)}." if ideas else ""))


def preview(net, fn, source: str) -> list | None:
    """A source's diff against the target, without recording an attempt (None: it doesn't compile or score)."""
    proj = net.store.one("SELECT * FROM projects WHERE id=?", fn["project_id"])
    try:
        ctx = net._context(proj["id"], fn["unit"])
        result = net._compile_function(proj, fn, ctx["header"], source)
        if not result.ok:
            return None
        cand, _verdict, _ = net._evaluate(fn, proj, result.obj)
        if cand is None:
            return None
        return scoring.diff(net._target_obj(fn, proj), cand).rows
    except Exception:  # a diff is a nicety in the prompt
        return None
