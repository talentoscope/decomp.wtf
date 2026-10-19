"""What a public decomp says about AI and contributions, read before it's forked.

Every project works differently: some don't want AI-made code at all, others don't want drive-by contributions
from people who put no work into them. decomp.wtf shows what the project's own README, CONTRIBUTING and agent
files say before anything is cloned: the lines about AI, word for word, and which way they lean. A partial clone
(no file contents except the few read) makes it work for any git host and costs a second or two.
"""

from __future__ import annotations

import os
import hashlib
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

# files a project states its rules in, at the top, in .github/ or docs/
_POLICY_FILE = re.compile(r"^(?:\.github/|docs?/)?(?:readme|contributing|contribute|code_of_conduct|agents|claude|"
                          r"gemini|ai|ai[-_]?policy|llm[-_]?policy|policy|guidelines)(?:\.(?:md|markdown|txt|rst))?$", re.I)
# mentions of AI ("AI" in capitals only: "said", "mail"...)
_AI = re.compile(r"\bA\.?I\b|(?i:\bLLMs?\b|\blarge language models?\b|\bChatGPT\b|\bCopilot\b|\bClaude\b|\bCodex\b|"
                 r"\bGPT-?\d|\bGemini\b|\bmachine[- ]generated\b|\bgenerative\b|\bartificial intelligence\b|\bvibe[- ]?cod)")
_NO = re.compile(r"\b(?:no|not|never|don'?t|do not|won'?t|will not|cannot|can'?t|prohibit\w*|ban(?:ned|s)?|forbid\w*|"
                 r"disallow\w*|reject\w*|refus\w*|unwelcome|without|closed|removed|deleted)\b", re.I)
_YES = re.compile(r"\b(?:welcome\w*|allowed|accept\w*|encourag\w*|fine|okay|ok|permitted|may use|can use|feel free)\b", re.I)


def _git(*args: str, cwd: str | None = None, timeout: int = 90) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-c", "safe.directory=*", *args], cwd=cwd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout, stdin=subprocess.DEVNULL,
                          env={"GIT_TERMINAL_PROMPT": "0", "PATH": os.environ.get("PATH", "")})


def lines_about_ai(text: str, limit: int = 8) -> list[str]:
    """The sentences of a text that mention AI, trimmed, without markdown decoration."""
    out = []
    for para in re.split(r"\n\s*\n", text):
        flat = re.sub(r"\s+", " ", re.sub(r"[*_`>#]+|\[([^\]]*)\]\([^)]*\)", r"\1", para)).strip(" -")
        for sentence in re.split(r"(?<=[.!?])\s+(?=[A-Z])", flat):
            if sentence.count(" - ") >= 3 or sentence.count(" | ") >= 3:  # a table of contents, a table row
                continue
            if _AI.search(sentence) and len(sentence) > 12:
                out.append(sentence[:400])
                if len(out) >= limit:
                    return out
    return out


def lean(lines: list[str]) -> str:
    """no | rules | yes | unclear | silent, from what the lines say (a hint: the lines themselves are what to read).
    "rules": AI is allowed, with limits of the project's ("acceptable ... refrain from letting AI name fields")."""
    if not lines:
        return "silent"
    yes = any(_YES.search(ln) and not _NO.search(ln) for ln in lines)
    no = any(_NO.search(ln) for ln in lines)
    return "rules" if yes and no else "no" if no else "yes" if yes else "unclear"


MAX_FILE_BYTES = 400_000
MAX_FILES = 32
NOTICE_MARKER = "<!-- decomp.wtf generated reconstruction notice -->"


def _priority(name: str) -> tuple:
    base = Path(name).stem.lower()
    return (base in ("readme", "code_of_conduct"), name.lower())


def inspect(root: Path, *, working_tree: bool = False) -> dict:
    """Read bounded policy evidence from HEAD, or the actual local files before exporting commits."""
    root = Path(root).resolve()
    skipped, names = [], []
    revision = _git("rev-parse", "HEAD", cwd=str(root)).stdout.strip()
    revision = revision if re.fullmatch(r"[0-9a-f]{40,64}", revision) else None
    if working_tree:
        for relative in ("", ".github", "doc", "docs"):
            directory = root / relative
            if not directory.is_dir():
                continue
            if not directory.resolve().is_relative_to(root):
                skipped.append({"file": relative, "reason": "external directory link"})
                continue
            try:
                for i, path in enumerate(directory.iterdir()):
                    if i >= 4096:
                        skipped.append({"file": relative or ".", "reason": "directory scan limit"})
                        break
                    name = path.relative_to(root).as_posix()
                    if _POLICY_FILE.fullmatch(name) and path.is_file():
                        names.append(name)
            except OSError:
                skipped.append({"file": relative or ".", "reason": "unreadable directory"})
    else:
        listing = _git("ls-tree", "-r", "--name-only", revision or "HEAD", cwd=str(root))
        if listing.returncode:
            return {"files": [], "lines": [], "lean": "silent", "error": "could not read committed policy files"}
        names = [f for f in listing.stdout.splitlines() if _POLICY_FILE.fullmatch(f)]
    names = sorted(set(names), key=_priority)
    if len(names) > MAX_FILES:
        skipped += [{"file": f, "reason": "policy file limit"} for f in names[MAX_FILES:]]
    files, found, fingerprint = names[:MAX_FILES], [], hashlib.sha256()
    for name in files:
        try:
            if working_tree:
                path = root / name
                if not path.resolve().is_relative_to(root):
                    raise ValueError("external file link")
                size = path.stat().st_size
                if size > MAX_FILE_BYTES:
                    raise ValueError("policy file exceeds 400000 bytes")
                with path.open("rb") as stream:
                    data = stream.read(MAX_FILE_BYTES + 1)
                if len(data) > MAX_FILE_BYTES:
                    raise ValueError("policy file exceeds 400000 bytes")
                text = data.decode("utf-8", "replace")
            else:
                size = _git("cat-file", "-s", (revision or "HEAD") + ":" + name, cwd=str(root))
                if size.returncode or not size.stdout.strip().isdigit():
                    raise ValueError("unreadable policy file")
                if int(size.stdout) > MAX_FILE_BYTES:
                    raise ValueError("policy file exceeds 400000 bytes")
                shown = _git("show", (revision or "HEAD") + ":" + name, cwd=str(root), timeout=60)
                if shown.returncode:
                    raise ValueError("unreadable policy file")
                text = shown.stdout
                data = text.encode("utf-8")
            fingerprint.update(name.encode() + b"\0" + hashlib.sha256(data).digest())
            # The reconstruction notice is provenance, not an upstream contribution rule. Any appended rules
            # still count, including rules added to this very README by its owner.
            evidence = lines_about_ai(text)
            if NOTICE_MARKER in text:
                evidence = [t for t in evidence if t != "Functions reconstructed by AI agents on decomp.wtf."]
            found += [{"file": name, "text": t} for t in evidence]
        except (OSError, ValueError) as exc:
            skipped.append({"file": name, "reason": str(exc) if isinstance(exc, ValueError) else "unreadable policy file"})
    found = found[:12]
    return {"files": files, "lines": found, "lean": lean([x["text"] for x in found]),
            "revision": revision,
            "fingerprint": fingerprint.hexdigest(), "skipped": skipped,
            "error": "policy review is incomplete; some files could not be inspected" if skipped else None}


def check(url: str) -> dict:
    """Read a public repository without checking out or executing its contents."""
    if not shutil.which("git"):
        return {"files": [], "lines": [], "lean": "silent", "error": "git isn't installed"}
    with tempfile.TemporaryDirectory(prefix="decompwtf-policy-") as tmp:
        try:
            res = _git("clone", "--depth", "1", "--filter=blob:none", "--no-checkout", "--quiet", url, tmp)
            if res.returncode != 0:
                return {"files": [], "lines": [], "lean": "silent",
                        "error": f"couldn't read the repository: {(res.stderr or res.stdout).strip()[-300:]}"}
            return inspect(Path(tmp))
        except subprocess.TimeoutExpired:
            return {"files": [], "lines": [], "lean": "silent", "error": "the repository took too long to answer"}
