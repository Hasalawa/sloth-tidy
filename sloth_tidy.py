#!/usr/bin/env python3
"""sloth-tidy: a lazy, local, undoable file tidier powered by an Ollama model.

Point it at a messy folder. A local open-weight model reads each file's name
and a short snippet, picks a category and a clean file name, and the file is
moved. Nothing leaves your machine. Dry-run is the default; every real run is
written to a log so `undo` can put everything back.

Standard library only.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

LOG_NAME = ".sloth-tidy-log.json"
CATEGORIES = [
    "Documents", "Images", "Screenshots", "Receipts", "Code",
    "Data", "Archives", "Audio", "Video", "Installers", "Other",
]
TEXT_EXTS = {
    ".txt", ".md", ".csv", ".json", ".log", ".py", ".js", ".ts", ".html",
    ".css", ".yaml", ".yml", ".toml", ".xml", ".sql", ".sh", ".rst", ".tex",
}
EXT_FALLBACK = {
    "Documents": {".pdf", ".doc", ".docx", ".odt", ".rtf", ".txt", ".md", ".ppt", ".pptx"},
    "Images": {".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".heic", ".bmp"},
    "Code": {".py", ".js", ".ts", ".html", ".css", ".sh", ".java", ".c", ".cpp", ".go", ".rs"},
    "Data": {".csv", ".json", ".xlsx", ".xls", ".parquet", ".sql", ".xml", ".yaml", ".yml"},
    "Archives": {".zip", ".tar", ".gz", ".7z", ".rar", ".tgz"},
    "Audio": {".mp3", ".wav", ".flac", ".m4a", ".ogg"},
    "Video": {".mp4", ".mkv", ".mov", ".avi", ".webm"},
    "Installers": {".exe", ".msi", ".dmg", ".deb", ".rpm", ".apk", ".pkg"},
}
SCHEMA = {
    "type": "object",
    "properties": {
        "category": {"type": "string", "enum": CATEGORIES},
        "new_name": {"type": "string"},
    },
    "required": ["category", "new_name"],
}
PROMPT = (
    "You tidy files. Given a file's name, size and a snippet of its content, "
    "choose exactly one category from: {cats}. Also propose a short, "
    "descriptive, lowercase-with-hyphens file name WITHOUT the extension "
    "(max 6 words). If the existing name is already clear, keep it. "
    "Reply with JSON only."
)


@dataclass
class Move:
    src: Path
    dst: Path
    category: str
    used_ai: bool


# ---------------------------------------------------------------- helpers
def slugify(text: str, fallback: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    s = re.sub(r"-{2,}", "-", s)[:60].strip("-")
    return s or fallback


def snippet(path: Path, limit: int = 600) -> str:
    if path.suffix.lower() not in TEXT_EXTS:
        return ""
    try:
        with path.open("r", encoding="utf-8", errors="ignore") as fh:
            return fh.read(limit)
    except OSError:
        return ""


def rule_category(path: Path) -> str:
    ext = path.suffix.lower()
    name = path.name.lower()
    if ext in {".png", ".jpg", ".jpeg"} and name.startswith(("screenshot", "screen shot", "scr")):
        return "Screenshots"
    if any(w in name for w in ("receipt", "invoice", "bill")):
        return "Receipts"
    for cat, exts in EXT_FALLBACK.items():
        if ext in exts:
            return cat
    return "Other"


def ask_ollama(path: Path, host: str, model: str, timeout: float = 120.0) -> dict:
    body = {
        "model": model,
        "stream": False,
        "format": SCHEMA,
        "options": {"temperature": 0},
        "messages": [
            {"role": "system", "content": PROMPT.format(cats=", ".join(CATEGORIES))},
            {
                "role": "user",
                "content": json.dumps(
                    {"name": path.name, "size_bytes": path.stat().st_size, "snippet": snippet(path)}
                ),
            },
        ],
    }
    req = urllib.request.Request(
        host.rstrip("/") + "/api/chat",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.load(resp)
    return json.loads(payload["message"]["content"])


def unique(dst: Path, taken: set[Path]) -> Path:
    if dst not in taken and not dst.exists():
        return dst
    i = 1
    while True:
        cand = dst.with_name(f"{dst.stem}-{i}{dst.suffix}")
        if cand not in taken and not cand.exists():
            return cand
        i += 1


# ------------------------------------------------------------------- plan
def plan(root: Path, host: str, model: str, use_ai: bool, min_age: float = 0.0) -> list[Move]:
    moves: list[Move] = []
    taken: set[Path] = set()
    ai_alive = use_ai
    now = time.time()
    for f in sorted(root.iterdir()):
        if not f.is_file() or f.name.startswith(".") or f.name == LOG_NAME:
            continue
        if min_age and now - f.stat().st_mtime < min_age:
            continue  # still being written/downloaded
        category, stem, used_ai = rule_category(f), slugify(f.stem, "file"), False
        if ai_alive:
            try:
                out = ask_ollama(f, host, model)
                if out.get("category") in CATEGORIES:
                    category = out["category"]
                stem = slugify(str(out.get("new_name", "")), stem)
                used_ai = True
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
                print("! Ollama not reachable, falling back to simple rules.", file=sys.stderr)
                ai_alive = False
            except (KeyError, ValueError, TypeError):
                pass  # bad model output for this file: keep rule-based result
        dst = unique(root / category / f"{stem}{f.suffix.lower()}", taken)
        taken.add(dst)
        moves.append(Move(f, dst, category, used_ai))
    return moves


def apply(root: Path, moves: list[Move]) -> None:
    done = []
    for m in moves:
        m.dst.parent.mkdir(exist_ok=True)
        m.src.rename(m.dst)
        done.append({"from": str(m.src), "to": str(m.dst)})
    log_path = root / LOG_NAME
    history = json.loads(log_path.read_text()) if log_path.exists() else []
    history.append({"at": time.strftime("%Y-%m-%d %H:%M:%S"), "moves": done})
    log_path.write_text(json.dumps(history, indent=2))


def undo(root: Path) -> int:
    log_path = root / LOG_NAME
    if not log_path.exists():
        print("Nothing to undo.")
        return 0
    history = json.loads(log_path.read_text())
    if not history:
        print("Nothing to undo.")
        return 0
    run = history.pop()
    restored = 0
    for mv in reversed(run["moves"]):
        src, dst = Path(mv["to"]), Path(mv["from"])
        if src.exists() and not dst.exists():
            src.rename(dst)
            restored += 1
            try:
                src.parent.rmdir()  # remove category folder if now empty
            except OSError:
                pass
    log_path.write_text(json.dumps(history, indent=2))
    print(f"Restored {restored} file(s).")
    return restored


def show(root: Path, moves: list[Move], applied: bool) -> None:
    if not moves:
        print("Already tidy. Nothing to do. (Sloth approves.)")
        return
    for m in moves:
        tag = "AI " if m.used_ai else "rule"
        print(f"[{tag}] {m.src.name}  ->  {m.dst.relative_to(root)}")
    verb = "Moved" if applied else "Would move"
    print(f"\n{verb} {len(moves)} file(s)." + ("" if applied else "  Re-run with --apply to do it."))


# -------------------------------------------------------------------- cli
def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="sloth-tidy", description=__doc__.splitlines()[0])
    p.add_argument("--host", default="http://localhost:11434", help="Ollama URL")
    p.add_argument("--model", default="llama3.2:3b", help="any Ollama model you have pulled")
    p.add_argument("--no-ai", action="store_true", help="skip the model, use extension rules only")
    sub = p.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("tidy", help="plan (and optionally apply) a tidy-up")
    t.add_argument("folder", type=Path)
    t.add_argument("--apply", action="store_true", help="actually move files (default: dry run)")
    u = sub.add_parser("undo", help="revert the most recent tidy-up")
    u.add_argument("folder", type=Path)
    w = sub.add_parser("watch", help="tidy a folder forever, slowly")
    w.add_argument("folder", type=Path)
    w.add_argument("--every", type=float, default=300.0, help="seconds between naps")
    w.add_argument("--settle", type=float, default=10.0, help="ignore files newer than this many seconds")
    args = p.parse_args(argv)

    root = args.folder.expanduser().resolve()
    if not root.is_dir():
        print(f"Not a folder: {root}", file=sys.stderr)
        return 2
    if args.cmd == "undo":
        undo(root)
        return 0
    if args.cmd == "tidy":
        moves = plan(root, args.host, args.model, not args.no_ai)
        if args.apply and moves:
            apply(root, moves)
        show(root, moves, args.apply)
        return 0
    # watch
    print(f"Napping on {root}. Ctrl+C to wake me.")
    try:
        while True:
            moves = plan(root, args.host, args.model, not args.no_ai, min_age=args.settle)
            if moves:
                apply(root, moves)
                show(root, moves, True)
            time.sleep(args.every)
    except KeyboardInterrupt:
        print("\nBack to sleep.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
