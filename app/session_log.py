"""Everything a LocalRAG session does, written down twice: a Markdown file people can read (or hand in), and a
JSONL file with one record per event for analysis. Both live in %LOCALAPPDATA%\\LocalRAG\\sessions."""
import json
import threading
from datetime import datetime
from pathlib import Path


class SessionLog:
    def __init__(self, folder: Path, header: dict):
        folder.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        self.md_path = folder / f"session_{stamp}.md"
        self.jsonl_path = folder / f"session_{stamp}.jsonl"
        self.questions = 0
        self._lock = threading.Lock()
        self.write("session", header)

    def write(self, event: str, data: dict):
        record = {"time": datetime.now().isoformat(timespec="seconds"), "event": event, **data}
        if event == "question":
            self.questions += 1
            record["number"] = self.questions
        with self._lock:
            with self.jsonl_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
            with self.md_path.open("a", encoding="utf-8") as f:
                f.write(_markdown(record))


def _fence(text: str) -> str:
    """A code fence that cannot be closed early by backticks inside the text."""
    ticks = "```"
    while ticks in text:
        ticks += "`"
    return f"{ticks}text\n{text.rstrip()}\n{ticks}\n"


def _table(rows) -> str:
    lines = ["| | |", "|---|---|"]
    lines += [f"| {key} | {str(value).replace('|', '/')} |" for key, value in rows if value not in (None, "")]
    return "\n".join(lines) + "\n"


def _markdown(r: dict) -> str:
    clock = r["time"][11:19]
    kind = r["event"]
    if kind == "session":
        return (f"# LocalRAG session, {r['time'][:10]} {clock}\n\n"
                + _table([("Program", f"LocalRAG {r.get('version')}"), ("Python", r.get("python")),
                          ("Frozen .exe", r.get("frozen")), ("Program folder", r.get("app_dir")),
                          ("This log", r.get("md_path"))]) + "\n")
    if kind == "hardware":
        return f"_{clock} · {r['summary']}_\n\n"
    if kind in ("db_built", "db_opened"):
        title = "Knowledge base built" if kind == "db_built" else "Knowledge base opened"
        out = (f"## {clock} · {title}\n\n"
               + _table([("DB folder", r.get("db_dir")), ("Documents folder", r.get("source_folder")),
                         ("Embedding model", r.get("embedding")), ("Chunking", r.get("chunking")),
                         ("Files", r.get("files")), ("Chunks", r.get("chunks")), ("Built on", r.get("created")),
                         ("Similarity gate set to", r.get("threshold")), ("Time", f"{r.get('seconds', 0):.1f} s")]))
        if r.get("problems"):
            out += "\nFiles that gave no text:\n\n" + "".join(f"- {p}\n" for p in r["problems"])
        return out + "\n"
    if kind == "model_loaded":
        return (f"## {clock} · Language model loaded\n\n"
                + _table([("Model", r.get("model")), ("Runs on", r.get("where")),
                          ("Load time", f"{r.get('seconds', 0):.1f} s (includes the download, if any)")]) + "\n")
    if kind == "model_unloaded":
        return f"## {clock} · Language model unloaded: {r.get('model')}\n\n"
    if kind == "error":
        return f"## {clock} · Error: {r.get('task')}\n\n{_fence(r.get('error', ''))}\n"
    if kind == "question":
        return _question(r, clock)
    return f"## {clock} · {kind}\n\n{_fence(json.dumps(r, ensure_ascii=False, indent=2, default=str))}\n"


def _question(r: dict, clock: str) -> str:
    out = [f"## {clock} · Question {r['number']}\n\n> {r['question']}\n\n",
           f"**Settings:** k = {r['k']} · similarity gate {r['threshold']:.2f} · embedding {r['embedding']} · "
           f"DB {r['db_dir']}\n\n",
           f"### Retrieved context ({len(r['passages'])} passages, search {r['search_ms']:.0f} ms)\n\n",
           "| # | Similarity | Document | Chunk id |\n|---|---|---|---|\n"]
    for p in r["passages"]:
        page = f", page {p['page']}" if p.get("page") else ""
        out.append(f"| {p['rank']} | {p['score']:.2f} | {p['title']}{page} | {p['id']} |\n")
    out.append("\n")
    for p in r["passages"]:
        out.append(f"**[{p['rank']}] {p['title']}** · {p['source']}\n\n{_fence(p['text'])}\n")
    best = r["passages"][0]["score"] if r["passages"] else 0.0
    if r["outcome"] == "refused":
        out.append(f"**Gate:** best similarity {best:.2f} is below {r['threshold']:.2f}, so the app answered "
                   "\"I don't have that information\" and sent nothing to a language model.\n\n")
    elif r["outcome"] == "passages":
        out.append(f"**Gate:** passed ({best:.2f} ≥ {r['threshold']:.2f}), but no language model was loaded: "
                   "the passages were shown instead of an answer.\n\n")
    else:
        g = r.get("generation", {})
        out.append(f"**Gate:** passed ({best:.2f} ≥ {r['threshold']:.2f}).\n\n")
        out.append(f"### Prompt sent to {g.get('model')} ({g.get('prompt_tokens')} tokens after its chat template)\n\n")
        out.append(_fence(g.get("prompt", "")) + "\n")
        out.append(f"### Answer{' (stopped)' if r['outcome'] == 'stopped' else ''}\n\n{r.get('answer', '').strip()}\n\n")
        out.append(f"_{g.get('new_tokens')} tokens · first token after {g.get('first_token_s', 0):.1f} s · "
                   f"{g.get('tokens_per_s', 0):.1f} tokens/s · {g.get('total_s', 0):.1f} s · {g.get('where')} · "
                   f"{g.get('decoding')}_\n\n")
    return "".join(out) + "---\n\n"
