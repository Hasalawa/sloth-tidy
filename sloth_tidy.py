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
import secrets
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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
# Names Windows refuses to create as files, and files we must never touch:
RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
SKIP_EXT = {".crdownload", ".part", ".partial", ".tmp", ".temp", ".download", ".opdownload", ".aria2"}
SKIP_NAMES = {"desktop.ini", "thumbs.db", ".ds_store"}


def slugify(text: str, fallback: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    s = re.sub(r"-{2,}", "-", s)[:60].strip("-")
    s = s or fallback
    return s + "-file" if s in RESERVED else s


def is_ignored(f: Path) -> bool:
    """Dotfiles, our own log, unfinished downloads, Office lock files, Windows hidden/system files."""
    name = f.name.lower()
    if name.startswith((".", "~$")) or name == LOG_NAME or name in SKIP_NAMES or f.suffix.lower() in SKIP_EXT:
        return True
    try:
        attrs = getattr(f.stat(), "st_file_attributes", 0)   # only exists on Windows
    except OSError:
        return True
    return bool(attrs & 0x2 or attrs & 0x4)                  # hidden or system


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
def plan(root: Path, host: str, model: str, use_ai: bool, min_age: float = 0.0, on_warn=None) -> list[Move]:
    moves: list[Move] = []
    taken: set[Path] = set()
    ai_alive = use_ai
    now = time.time()
    for f in sorted(root.iterdir()):
        if not f.is_file() or is_ignored(f):
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
            except (socket.timeout, TimeoutError):
                # one slow file (e.g. the model is still loading): use rules for this file only
                if on_warn:
                    on_warn(f"The model was too slow for {f.name}; used simple rules for it.")
            except (urllib.error.URLError, ConnectionError, OSError):
                msg = "Ollama not reachable, falling back to simple rules."
                if on_warn:
                    on_warn(msg)
                else:
                    print("! " + msg, file=sys.stderr)
                ai_alive = False
            except (KeyError, ValueError, TypeError):
                pass  # bad model output for this file: keep rule-based result
        dst = unique(root / category / f"{stem}{f.suffix.lower()}", taken)
        taken.add(dst)
        moves.append(Move(f, dst, category, used_ai))
    return moves


def _read_log(log_path: Path) -> list:
    try:
        return json.loads(log_path.read_text(encoding="utf-8")) if log_path.exists() else []
    except (OSError, ValueError):
        return []


def apply(root: Path, moves: list[Move]):
    """Move the files. Returns (moved, failed).

    Failures (a file open in another program, a permissions error...) are
    skipped, not fatal, and everything that did move is logged so Undo works."""
    done: list[Move] = []
    failed: list[tuple[str, str]] = []
    for m in moves:
        try:
            m.dst.parent.mkdir(exist_ok=True)
            m.src.rename(m.dst)
            done.append(m)
        except OSError as e:
            failed.append((m.src.name, e.strerror or str(e)))
    if done:
        log_path = root / LOG_NAME
        history = _read_log(log_path)
        history.append({"at": time.strftime("%Y-%m-%d %H:%M:%S"),
                        "moves": [{"from": str(m.src), "to": str(m.dst)} for m in done]})
        log_path.write_text(json.dumps(history, indent=2), encoding="utf-8")
    return done, failed


def undo(root: Path) -> int:
    log_path = root / LOG_NAME
    history = _read_log(log_path)
    if not history:
        print("Nothing to undo.")
        return 0
    run = history.pop()
    restored, kept = 0, []
    for mv in reversed(run["moves"]):
        src, dst = Path(mv["to"]), Path(mv["from"])
        if not src.exists():
            continue                      # file was moved or deleted since: nothing to restore
        if dst.exists():
            kept.append(mv)               # something new is in the way: keep it in the log
            continue
        try:
            src.rename(dst)
        except OSError:
            kept.append(mv)               # locked right now: keep it so Undo can be retried
            continue
        restored += 1
        try:
            src.parent.rmdir()            # remove the category folder if now empty
        except OSError:
            pass
    if kept:
        history.append({"at": run.get("at", ""), "moves": list(reversed(kept))})
    log_path.write_text(json.dumps(history, indent=2), encoding="utf-8")
    msg = f"Restored {restored} file(s)."
    if kept:
        msg += f" {len(kept)} could not be restored (in use or name taken); run undo again later."
    print(msg)
    return restored


def show(root: Path, moves: list[Move], applied: bool, failed=()) -> None:
    if not moves:
        print("Already tidy. Nothing to do. (Sloth approves.)")
        return
    for m in moves:
        tag = "AI " if m.used_ai else "rule"
        print(f"[{tag}] {m.src.name}  ->  {m.dst.relative_to(root)}")
    verb = "Moved" if applied else "Would move"
    print(f"\n{verb} {len(moves)} file(s)." + ("" if applied else "  Re-run with --apply to do it."))
    for name, why in failed:
        print(f"! Skipped {name}: {why} (is it open in another program?)")


# -------------------------------------------------------------------- gui
PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>sloth-tidy</title>
<style>
:root{--bg:#f6f3ec;--card:#fffdf8;--ink:#2b2a26;--mute:#7a7568;--line:#e4dfd2;--accent:#52744a;--accent-ink:#fff;--warn:#a85a14;--chip:#ece7d8;--ai:#dfeadb}
@media (prefers-color-scheme:dark){:root{--bg:#171815;--card:#20211d;--ink:#ece9df;--mute:#9a9686;--line:#33342d;--accent:#8fb37e;--accent-ink:#14150f;--warn:#e0a060;--chip:#2c2d26;--ai:#2a3a27}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
main{max-width:860px;margin:0 auto;padding:32px 16px 80px}
h1{font-size:1.9rem;margin:0}
.tag{color:var(--mute);margin:2px 0 24px}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:18px;margin-bottom:16px}
label.l{display:block;font-size:.8rem;text-transform:uppercase;letter-spacing:.06em;color:var(--mute);margin-bottom:6px}
.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
input[type=text],select{flex:1;min-width:0;padding:10px 12px;border:1px solid var(--line);border-radius:10px;background:var(--bg);color:var(--ink);font:inherit}
button{padding:10px 16px;border-radius:10px;border:1px solid var(--line);background:var(--chip);color:var(--ink);font:inherit;cursor:pointer}
button:hover{border-color:var(--accent)}
button.primary{background:var(--accent);color:var(--accent-ink);border-color:var(--accent);font-weight:600}
button:disabled{opacity:.5;cursor:default}
.quick{margin-top:8px;display:flex;gap:6px;flex-wrap:wrap}
.quick button{padding:4px 10px;font-size:.85rem}
.opts{margin-top:14px;display:grid;grid-template-columns:1fr auto;gap:12px;align-items:end}
.tog{display:flex;gap:8px;align-items:center;padding-bottom:10px;color:var(--mute);font-size:.9rem}
.cat{margin-top:18px}
.cat h3{margin:0 0 6px;font-size:1rem;display:flex;gap:8px;align-items:center}
.cat h3 span{font-weight:400;color:var(--mute);font-size:.85rem}
.item{display:flex;gap:10px;align-items:center;padding:7px 4px;border-top:1px solid var(--line);font-size:.95rem}
.item .nm{flex:1;min-width:0;overflow-wrap:anywhere}
.item .old{color:var(--mute);text-decoration:line-through;text-decoration-color:var(--line)}
.item .arrow{color:var(--mute);margin:0 6px}
.badge{font-size:.7rem;padding:2px 8px;border-radius:99px;background:var(--chip);color:var(--mute);flex:none}
.badge.ai{background:var(--ai);color:var(--ink)}
.bar{display:flex;gap:10px;align-items:center;justify-content:space-between;flex-wrap:wrap;margin-top:18px;padding-top:14px;border-top:1px solid var(--line)}
.note{color:var(--warn);font-size:.9rem;margin-top:10px}
.msg{padding:12px 16px;border-radius:10px;background:var(--ai);margin-bottom:16px}
.msg.err{background:#f3d6cf;color:#5a1d10}
@media (prefers-color-scheme:dark){.msg.err{background:#4a2219;color:#f3d6cf}}
a{color:var(--accent)}
.empty{color:var(--mute);text-align:center;padding:24px}
.spin{display:inline-block;width:14px;height:14px;border:2px solid var(--line);border-top-color:var(--accent);border-radius:50%;animation:s 1s linear infinite;vertical-align:-2px;margin-right:8px}
@keyframes s{to{transform:rotate(360deg)}}
dialog{border:1px solid var(--line);border-radius:14px;background:var(--card);color:var(--ink);width:min(560px,92vw);padding:18px}
dialog::backdrop{background:rgba(0,0,0,.4)}
.dirs{max-height:280px;overflow:auto;margin:10px 0;border:1px solid var(--line);border-radius:10px}
.dirs div{padding:8px 12px;cursor:pointer;border-top:1px solid var(--line)}
.dirs div:first-child{border-top:0}
.dirs div:hover{background:var(--chip)}
.cur{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:.85rem;overflow-wrap:anywhere;color:var(--mute)}
</style></head>
<body><main>
<h1>sloth-tidy 🦥</h1>
<p class="tag">Slow and steady. A local model tidies your folder; nothing leaves this machine.</p>

<div id="msg" hidden></div>

<div class="card">
  <label class="l" for="folder">Folder to tidy</label>
  <div class="row">
    <input type="text" id="folder" placeholder="Paste a path, or press Browse" autocomplete="off">
    <button id="browse">Browse…</button>
  </div>
  <div class="quick" id="quick"></div>
  <div class="opts">
    <div><label class="l" for="model">Model (from your Ollama)</label>
      <select id="model"></select></div>
    <label class="tog"><input type="checkbox" id="noai"> Skip the model (extension rules only)</label>
  </div>
  <div class="row" style="margin-top:16px">
    <button class="primary" id="go">Take a look (nothing moves yet)</button>
    <button id="undo">Undo last tidy</button>
  </div>
</div>

<div class="card" id="result" hidden>
  <div id="summary"></div>
  <div id="plan"></div>
  <div class="note" id="warn" hidden></div>
  <div class="bar" id="actions" hidden>
    <span><a href="#" id="all">all</a> / <a href="#" id="none">none</a> &nbsp; <span id="count"></span></span>
    <button class="primary" id="apply">Move the selected files</button>
  </div>
</div>

<dialog id="dlg">
  <strong>Pick a folder</strong>
  <div class="cur" id="cur"></div>
  <div class="dirs" id="dirs"></div>
  <div class="row" style="justify-content:flex-end">
    <button id="cancel">Cancel</button>
    <button id="up">Up one level</button>
    <button class="primary" id="use">Use this folder</button>
  </div>
</dialog>
</main>
<script>
const TOKEN="__TOKEN__", DEF=__DEFAULTS__;
const $=s=>document.querySelector(s);
let current=null, curDir="";
function el(tag,cls,text){const e=document.createElement(tag);if(cls)e.className=cls;if(text!==undefined)e.textContent=text;return e}
async function api(path,body){
  const o={headers:{"X-Token":TOKEN}};
  if(body){o.method="POST";o.headers["Content-Type"]="application/json";o.body=JSON.stringify(body)}
  const r=await fetch(path,o);let j={};try{j=await r.json()}catch(e){}
  if(!r.ok)throw new Error(j.error||r.statusText);return j;
}
function say(text,err){const m=$("#msg");m.className="msg"+(err?" err":"");m.textContent=text;m.hidden=!text}
let tick=null;
function busy(on,label){["go","undo","apply","browse"].forEach(i=>{const b=$("#"+i);if(b)b.disabled=on});
  clearInterval(tick);
  if(on){const m=$("#msg");m.className="msg";const t=document.createTextNode(label),t0=Date.now();
    m.replaceChildren(el("span","spin"),t);m.hidden=false;
    tick=setInterval(()=>{t.textContent=label+" ("+Math.round((Date.now()-t0)/1000)+" s)"},1000)}}
async function loadModels(){
  const sel=$("#model");sel.replaceChildren();
  try{const j=await api("/api/models?host="+encodeURIComponent(DEF.host));
    j.models.forEach(n=>{const o=el("option","",n);o.value=n;sel.append(o)});
    if(j.models.includes(DEF.model))sel.value=DEF.model;
    if(!j.models.length)throw new Error("none")}
  catch(e){const o=el("option","",DEF.model+" (Ollama not detected)");o.value=DEF.model;sel.append(o)}
}
async function ls(path){
  const j=await api("/api/ls?path="+encodeURIComponent(path||""));
  curDir=j.path;$("#cur").textContent=j.path;$("#up").disabled=!j.parent;$("#up").dataset.p=j.parent||"";
  const d=$("#dirs");d.replaceChildren();
  if(!j.dirs.length)d.append(el("div","","(no sub-folders)"));
  j.dirs.forEach(n=>{const x=el("div","","📁 "+n);x.onclick=()=>ls(j.path+j.sep+n);d.append(x)});
  return j;
}
$("#browse").onclick=async()=>{await ls($("#folder").value.trim());$("#dlg").showModal()};
$("#cancel").onclick=()=>$("#dlg").close();
$("#up").onclick=()=>ls($("#up").dataset.p);
$("#use").onclick=()=>{$("#folder").value=curDir;$("#dlg").close()};
function updateCount(){const n=document.querySelectorAll(".item input:checked").length;$("#count").textContent=n+" selected";$("#apply").disabled=!n}
function render(p){
  $("#result").hidden=false;const box=$("#plan");box.replaceChildren();
  $("#warn").hidden=!p.warnings.length;$("#warn").textContent=p.warnings.join(" ");
  if(!p.moves.length){$("#summary").textContent="";box.append(el("div","empty","Already tidy. Nothing to do. The sloth approves."));$("#actions").hidden=true;return}
  const ai=p.moves.filter(m=>m.used_ai).length;
  $("#summary").textContent=p.moves.length+" file(s) would move. "+ai+" decided by the model, "+(p.moves.length-ai)+" by simple rules.";
  const groups={};p.moves.forEach(m=>(groups[m.category]=groups[m.category]||[]).push(m));
  Object.keys(groups).sort().forEach(c=>{
    const g=el("div","cat"),h=el("h3","",c);h.append(el("span","",groups[c].length+" file(s)"));g.append(h);
    groups[c].forEach(m=>{
      const row=el("label","item"),cb=el("input");cb.type="checkbox";cb.checked=true;cb.onchange=updateCount;
      cb._m={name:m.name,category:m.category,new_name:m.new_name};
      const nm=el("span","nm");nm.append(el("span","old",m.name),el("span","arrow","→"),el("strong","",m.new_name));
      row.append(cb,nm,el("span","badge"+(m.used_ai?" ai":""),m.used_ai?"AI":"rule"));g.append(row)});
    box.append(g)});
  $("#actions").hidden=false;updateCount();
}
$("#go").onclick=async()=>{
  const folder=$("#folder").value.trim();if(!folder){say("Pick a folder first.",true);return}
  busy(true,"The sloth is reading your files. Slow and steady, this can take a while…");
  try{current=await api("/api/plan",{folder,model:$("#model").value,host:DEF.host,no_ai:$("#noai").checked});
    say("");render(current)}catch(e){say(e.message,true);$("#result").hidden=true}
  busy(false);if($("#msg").textContent.trim()===""||$("#msg").querySelector(".spin"))say("")
};
$("#apply").onclick=async()=>{
  const moves=[...document.querySelectorAll(".item input:checked")].map(c=>c._m);
  busy(true,"Moving files…");
  try{const j=await api("/api/apply",{folder:$("#folder").value.trim(),moves});
    $("#result").hidden=true;
    let t="Moved "+j.moved+" file(s). Changed your mind? Press “Undo last tidy”.";
    if(j.failed&&j.failed.length)t+=" Skipped "+j.failed.length+" (probably open in another program): "+j.failed.map(f=>f.name).join(", ");
    say(t,j.failed&&j.failed.length>0)}
  catch(e){say(e.message,true)}busy(false)
};
$("#undo").onclick=async()=>{
  if(!$("#folder").value.trim()){say("Pick a folder first.",true);return}
  busy(true,"Putting things back…");
  try{const j=await api("/api/undo",{folder:$("#folder").value.trim()});
    $("#result").hidden=true;say(j.restored?"Restored "+j.restored+" file(s).":"Nothing to undo in this folder.")}
  catch(e){say(e.message,true)}busy(false)
};
$("#all").onclick=e=>{e.preventDefault();document.querySelectorAll(".item input").forEach(c=>c.checked=true);updateCount()};
$("#none").onclick=e=>{e.preventDefault();document.querySelectorAll(".item input").forEach(c=>c.checked=false);updateCount()};
$("#noai").checked=DEF.no_ai;
(async()=>{
  await loadModels();
  const j=await api("/api/ls?path=");
  const q=$("#quick");
  j.quick.forEach(f=>{const b=el("button","",f.label);b.onclick=()=>{$("#folder").value=f.path};q.append(b)});
})();
</script></body></html>
"""


def _safe_root(raw: str) -> Path:
    root = Path(raw).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"Not a folder: {root}")
    if root == Path(root.anchor):
        raise ValueError("Refusing to tidy a filesystem root.")
    return root


def make_gui_server(port: int, host: str, model: str, use_ai: bool):
    """Build the local web UI server. Returns (server, token)."""
    token = secrets.token_urlsafe(16)
    defaults = {"host": host, "model": model, "no_ai": not use_ai}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code: int, payload, ctype="application/json"):
            data = payload.encode() if isinstance(payload, str) else json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype + "; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _guard(self, api: bool) -> bool:
            # Only answer requests addressed to loopback (defeats DNS rebinding)
            # and, for the API, only callers that hold this session's token.
            if self.headers.get("Host", "").rsplit(":", 1)[0] not in ("127.0.0.1", "localhost"):
                self._send(403, {"error": "forbidden host"})
                return False
            if api and self.headers.get("X-Token") != token:
                self._send(403, {"error": "bad token"})
                return False
            return True

        def do_GET(self):
            url = urllib.parse.urlparse(self.path)
            if url.path == "/":
                if self._guard(False):
                    page = PAGE.replace("__TOKEN__", token).replace("__DEFAULTS__", json.dumps(defaults))
                    self._send(200, page, "text/html")
                return
            if not self._guard(True):
                return
            q = urllib.parse.parse_qs(url.query)
            try:
                if url.path == "/api/models":
                    h = (q.get("host") or [host])[0]
                    try:
                        with urllib.request.urlopen(h.rstrip("/") + "/api/tags", timeout=3) as r:
                            names = [m["name"] for m in json.load(r).get("models", [])]
                    except (OSError, ValueError, KeyError):
                        names = []
                    self._send(200, {"models": names})
                elif url.path == "/api/ls":
                    raw = (q.get("path") or [""])[0].strip()
                    p = Path(raw).expanduser() if raw else Path.home()
                    p = p.resolve()
                    if not p.is_dir():
                        p = Path.home()
                    dirs = []
                    try:
                        dirs = sorted(c.name for c in p.iterdir() if c.is_dir() and not c.name.startswith("."))
                    except OSError:
                        pass
                    quick = [{"label": n, "path": str(Path.home() / n)}
                             for n in ("Downloads", "Desktop", "Documents") if (Path.home() / n).is_dir()]
                    parent = str(p.parent) if p.parent != p else ""
                    sep = "" if str(p).endswith(("/", "\\")) else ("\\" if "\\" in str(p) else "/")
                    self._send(200, {"path": str(p), "parent": parent, "dirs": dirs, "sep": sep, "quick": quick})
                else:
                    self._send(404, {"error": "not found"})
            except Exception as e:  # noqa: BLE001
                self._send(400, {"error": str(e)})

        def do_POST(self):
            if not self._guard(True):
                return
            try:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                root = _safe_root(str(body.get("folder", "")))
                if self.path == "/api/plan":
                    warnings: list[str] = []
                    moves = plan(root, str(body.get("host") or host), str(body.get("model") or model),
                                 not body.get("no_ai"), on_warn=warnings.append)
                    self._send(200, {
                        "warnings": warnings,
                        "moves": [{"name": m.src.name, "category": m.category,
                                   "new_name": m.dst.name, "used_ai": m.used_ai} for m in moves],
                    })
                elif self.path == "/api/apply":
                    taken: set[Path] = set()
                    todo: list[Move] = []
                    for item in body.get("moves", []):
                        name, cat = str(item.get("name", "")), str(item.get("category", ""))
                        src = root / name
                        if Path(name).name != name or name.startswith(".") or not src.is_file():
                            raise ValueError(f"Skipping unknown file: {name}")
                        if cat not in CATEGORIES:
                            raise ValueError(f"Unknown category: {cat}")
                        stem = slugify(Path(str(item.get("new_name", ""))).stem, slugify(src.stem, "file"))
                        dst = unique(root / cat / f"{stem}{src.suffix.lower()}", taken)
                        taken.add(dst)
                        todo.append(Move(src, dst, cat, False))
                    ok, failed = apply(root, todo) if todo else ([], [])
                    self._send(200, {"moved": len(ok), "failed": [{"name": n, "error": e} for n, e in failed]})
                elif self.path == "/api/undo":
                    self._send(200, {"restored": undo(root)})
                else:
                    self._send(404, {"error": "not found"})
            except ValueError as e:
                self._send(400, {"error": str(e)})
            except Exception as e:  # noqa: BLE001
                self._send(500, {"error": f"{type(e).__name__}: {e}"})

    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    return httpd, token


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
    g = sub.add_parser("gui", help="open the graphical interface in your browser")
    g.add_argument("--port", type=int, default=8765)
    g.add_argument("--no-browser", action="store_true", help="don't open a browser tab automatically")
    w = sub.add_parser("watch", help="tidy a folder forever, slowly")
    w.add_argument("folder", type=Path)
    w.add_argument("--every", type=float, default=300.0, help="seconds between naps")
    w.add_argument("--settle", type=float, default=10.0, help="ignore files newer than this many seconds")
    args = p.parse_args(argv)

    if args.cmd == "gui":
        httpd, _ = make_gui_server(args.port, args.host, args.model, not args.no_ai)
        url = f"http://127.0.0.1:{httpd.server_port}/"
        print(f"sloth-tidy GUI running at {url}  (Ctrl+C to stop)")
        if not args.no_browser:
            threading.Timer(0.5, webbrowser.open, args=(url,)).start()
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nBack to sleep.")
        return 0
    root = args.folder.expanduser().resolve()
    if not root.is_dir():
        print(f"Not a folder: {root}", file=sys.stderr)
        return 2
    if args.cmd == "undo":
        undo(root)
        return 0
    if args.cmd == "tidy":
        moves = plan(root, args.host, args.model, not args.no_ai)
        failed: list = []
        if args.apply and moves:
            moves, failed = apply(root, moves)
        show(root, moves, args.apply, failed)
        return 0
    # watch
    print(f"Napping on {root}. Ctrl+C to wake me.")
    try:
        while True:
            try:
                moves = plan(root, args.host, args.model, not args.no_ai, min_age=args.settle)
                if moves:
                    moves, failed = apply(root, moves)
                    show(root, moves, True, failed)
            except Exception as e:  # noqa: BLE001  (a nap must never be fatal)
                print(f"! {type(e).__name__}: {e}. Trying again next time.", file=sys.stderr)
            time.sleep(args.every)
    except KeyboardInterrupt:
        print("\nBack to sleep.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
