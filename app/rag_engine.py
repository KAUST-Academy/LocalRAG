"""LocalRAG engine: the pipeline of Day2_RAG_Pipeline.ipynb, as a library for the desktop app.

index once:    folder of files -> read -> clean -> chunk -> embed -> ChromaDB on disk (+ metadata, + localrag.json)
per question:  question -> embed -> top-k chunks -> similarity gate -> prompt -> streamed answer citing [1], [2], ...

torch, transformers, sentence-transformers and chromadb are imported inside the functions that use them:
importing them takes seconds, and the window should not wait for that.
"""
from __future__ import annotations

import functools
import gc
import html
import json
import logging
import os
import re
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

log = logging.getLogger("localrag")

APP_NAME = "LocalRAG"
APP_VERSION = "1.0.0"
COLLECTION = "documents"
MANIFEST = "localrag.json"        # sits next to ChromaDB's files and records how the DB was built


def app_dir() -> Path:
    """The folder of LocalRAG.exe when frozen by PyInstaller, else the folder of this file."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def data_dir() -> Path:
    """Per-user writable folder for settings and logs: %LOCALAPPDATA%\\LocalRAG (never the program folder)."""
    d = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / APP_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


class Cancelled(Exception):
    pass


# ----------------------------------------------------------------------------------------------- model catalogs

@dataclass(frozen=True)
class EmbeddingChoice:
    id: str                  # Hugging Face model id
    label: str               # what the dropdown shows
    note: str                # size, languages, speed
    threshold: float         # default similarity gate, measured with tools/calibrate.py
    query_prompt: str = ""   # text the model was trained to see in front of a question
    doc_prompt: str = ""     # text the model was trained to see in front of a passage


BGE_QUERY = "Represent this sentence for searching relevant passages: "
QWEN_QUERY = "Instruct: Given a web search query, retrieve relevant passages that answer the query\nQuery:"

# "lab n/14" = answers found in the top 3 for the lab's 14 questions (sections chunking); the gates sit in the gap
# between the best similarity of those questions and of 10 questions the handbook cannot answer (tools/calibrate.py).
EMBEDDINGS = [
    EmbeddingChoice("sentence-transformers/all-MiniLM-L6-v2", "MiniLM-L6 (the lab's model)",
                    "22M parameters · English · fastest · 0.09 GB · lab 10/14", 0.40),
    EmbeddingChoice("BAAI/bge-small-en-v1.5", "BGE small (English)",
                    "33M parameters · English · fast · 0.13 GB · lab 12/14", 0.62, query_prompt=BGE_QUERY),
    EmbeddingChoice("BAAI/bge-base-en-v1.5", "BGE base (English)",
                    "109M parameters · English · 0.44 GB · lab 13/14", 0.55, query_prompt=BGE_QUERY),
    EmbeddingChoice("intfloat/multilingual-e5-small", "E5 small (multilingual)",
                    "118M parameters · Arabic + 100 languages · 0.49 GB · lab 13/14", 0.82,
                    query_prompt="query: ", doc_prompt="passage: "),
    EmbeddingChoice("Qwen/Qwen3-Embedding-0.6B", "Qwen3-Embedding 0.6B (multilingual)",
                    "596M parameters · 100+ languages · slow · 1.2 GB · lab 13/14", 0.47,
                    query_prompt=QWEN_QUERY),
]


def embedding_by_id(model_id: str) -> EmbeddingChoice:
    """The catalog entry for a model id; an unknown id (a DB built by a newer version) gets plain settings."""
    for choice in EMBEDDINGS:
        if choice.id == model_id:
            return choice
    return EmbeddingChoice(model_id, model_id.split("/")[-1], "", 0.40)


@dataclass(frozen=True)
class LLMChoice:
    id: str
    label: str
    note: str


LLMS = [
    LLMChoice("Qwen/Qwen3.5-4B", "Qwen3.5-4B (the lab's model)", "4.7B parameters · 9.3 GB download"),
    LLMChoice("Qwen/Qwen3.5-0.8B", "Qwen3.5-0.8B (fastest)", "0.9B parameters · 1.8 GB download · runs on any laptop"),
    LLMChoice("Qwen/Qwen3.5-2B", "Qwen3.5-2B", "2.3B parameters · 4.6 GB download"),
    LLMChoice("Qwen/Qwen3.5-9B", "Qwen3.5-9B (large)", "9.7B parameters · 19.3 GB download · big GPU or 32 GB RAM"),
    LLMChoice("Qwen/Qwen3-4B-Instruct-2507", "Qwen3-4B-Instruct-2507", "4.0B parameters · 8.1 GB download · text only"),
    LLMChoice("microsoft/Phi-4-mini-instruct", "Phi-4-mini-instruct (Microsoft)", "3.8B parameters · 7.7 GB download"),
    LLMChoice("HuggingFaceTB/SmolLM3-3B", "SmolLM3-3B (Hugging Face)", "3.1B parameters · 6.2 GB download"),
]


def short_name(model_id: str) -> str:
    return Path(model_id).name if Path(model_id).is_dir() else model_id.split("/")[-1]


def gb(n_bytes: float) -> str:
    return f"{n_bytes / 1e9:.1f} GB"


# ----------------------------------------------------------------------------------------------- downloads

def _hub_folder(repo_id: str) -> Path:
    from huggingface_hub import constants
    return Path(constants.HF_HUB_CACHE) / ("models--" + repo_id.replace("/", "--"))


def _files_to_fetch(repo_id: str) -> list[tuple[str, int]]:
    """[(file name, bytes)] that a model needs: configs, tokenizer, and weights as .safetensors (.bin only where a
    folder has no .safetensors). Skips the ONNX / OpenVINO / TensorFlow / GGUF copies some repos also carry."""
    from huggingface_hub import HfApi
    siblings = HfApi().model_info(repo_id, files_metadata=True).siblings
    safetensors_dirs = {str(Path(s.rfilename).parent) for s in siblings if s.rfilename.endswith(".safetensors")}
    skip_dirs = ("onnx/", "openvino/", "coreml/", "tf/", "flax/", "gguf/")
    skip_ext = (".onnx", ".h5", ".msgpack", ".ot", ".gguf", ".tflite", ".pt", ".pth", ".ckpt",
                ".png", ".jpg", ".jpeg", ".gif", ".md", ".py")
    keep = []
    for s in siblings:
        name = s.rfilename
        if name.startswith(skip_dirs) or name.lower().endswith(skip_ext) or name == ".gitattributes":
            continue
        if name.endswith(".bin") and str(Path(name).parent) in safetensors_dirs:
            continue
        keep.append((name, s.size or 0))
    return keep


def fetch_model(model_id: str, progress=None) -> str:
    """A model id or a folder -> a local folder with the model's files, downloading what is missing (once, into the
    Hugging Face cache). A folder models\\<name> next to the .exe wins, so a class can share models on a USB stick."""
    if Path(model_id).is_dir():
        return model_id
    bundled = app_dir() / "models" / short_name(model_id)
    if bundled.is_dir():
        return str(bundled)
    from huggingface_hub import snapshot_download
    try:
        files = _files_to_fetch(model_id)
    except Exception as err:                                  # offline, or the Hub is down: use the cache if it has it
        log.warning("model_info(%s) failed (%s); trying the local cache", model_id, err)
        try:
            return snapshot_download(model_id, local_files_only=True)
        except Exception:
            raise RuntimeError(f"Could not download {model_id} and it is not in the local cache.\n"
                               f"Check the internet connection (or the model name), then try again.\n\n{err}") from err
    total = sum(size for _, size in files)
    outcome = {}

    def download():
        try:
            outcome["path"] = snapshot_download(model_id, allow_patterns=[name for name, _ in files])
        except Exception as err:
            outcome["error"] = err

    worker = threading.Thread(target=download, daemon=True)
    worker.start()
    blobs = _hub_folder(model_id) / "blobs"
    while worker.is_alive():                                  # report progress by watching the cache folder grow
        worker.join(0.5)
        if progress and total:
            done = 0
            for f in blobs.glob("*") if blobs.is_dir() else []:
                try:
                    done += f.stat().st_size
                except OSError:                               # a file renamed between glob() and stat()
                    pass
            progress(min(done / total, 1.0), f"Downloading {short_name(model_id)}: {gb(min(done, total))} of {gb(total)}")
    if "error" in outcome:
        raise outcome["error"]
    return outcome["path"]


# ----------------------------------------------------------------------------------------------- hardware

@functools.lru_cache(maxsize=1)
def cuda_ok() -> bool:
    """True when torch can really run on the GPU: an NVIDIA GPU, a new enough driver, and a GPU generation this
    torch build supports. Anything else -> the CPU, instead of a crash in the middle of a question."""
    import torch
    if not torch.cuda.is_available():
        return False
    # The CUDA build leaves out cuDNN's engine libraries (436 MB; LocalRAG.spec) so its zip fits GitHub's 2 GiB file
    # limit. cuDNN only speeds up convolutions, which text generation barely uses: PyTorch's own kernels do them.
    torch.backends.cudnn.enabled = False
    torch.backends.cuda.enable_cudnn_sdp(False)
    try:
        return (torch.ones(1, device="cuda") + 1).item() == 2
    except Exception as err:
        log.warning("CUDA is present but unusable, falling back to the CPU: %s", err)
        return False


def hardware_summary() -> str:
    import psutil
    import torch
    ram = f"{psutil.virtual_memory().total / 2**30:.0f} GB RAM"
    if cuda_ok():
        props = torch.cuda.get_device_properties(0)
        return f"GPU: {props.name} ({props.total_memory / 2**30:.0f} GB) · {ram}"
    return f"No usable NVIDIA GPU: models run on the CPU · {ram}"


def _placement(weight_bytes: int) -> tuple[dict, str]:
    """from_pretrained() arguments that fit the model on this machine, and a line saying where it went.

    Measured with Qwen3.5-4B (9.3 GB) on a laptop with an 8 GB GPU: the CPU alone in bfloat16 wrote 4.2 tokens/s,
    the model split between GPU and RAM only 1.6 (accelerate copies the RAM layers to the GPU for every token),
    and float32 on the CPU 3.2 at twice the RAM. So: all on the GPU if it fits, else the CPU, else the split."""
    import psutil
    import torch
    free_ram = psutil.virtual_memory().available
    free_gpu, name = 0, ""
    if cuda_ok():
        dtype = torch.bfloat16 if torch.cuda.is_bf16_supported() else torch.float16
        free_gpu, _ = torch.cuda.mem_get_info()
        name = torch.cuda.get_device_name(0)
        if weight_bytes + 1.5e9 < free_gpu:                   # weights + room for activations and the KV cache
            return {"dtype": dtype, "device_map": {"": 0}}, f"GPU · {name}"
    if weight_bytes + 1.5e9 < free_ram:
        why = f" (too big for the {gb(free_gpu)} free on the GPU)" if free_gpu else ""
        return {"dtype": torch.bfloat16}, f"CPU · bfloat16{why}"
    if free_gpu and weight_bytes + 3e9 < free_gpu + free_ram:
        gpu_gib = max(int((free_gpu - 1.5e9) / 2**30), 1)
        return ({"dtype": dtype, "device_map": "auto",
                 "max_memory": {0: f"{gpu_gib}GiB", "cpu": f"{int((free_ram - 1e9) / 2**30)}GiB"}},
                f"GPU + CPU · {gpu_gib} GB on {name}, the rest in RAM (slow)")
    raise MemoryError(f"This model needs about {gb(weight_bytes + 1.5e9)} of memory and this PC has "
                      f"{gb(free_ram)} of RAM free{f' and {gb(free_gpu)} on the GPU' if free_gpu else ''}. "
                      "Close other programs or pick a smaller model.")


# ----------------------------------------------------------------------------------------------- reading files

SUPPORTED = (".pdf", ".docx", ".txt", ".md", ".markdown", ".csv", ".html", ".htm")


def find_files(folder) -> list[Path]:
    """Every supported file in the folder and its subfolders (skipping hidden folders and Office lock files)."""
    folder = Path(folder)
    return sorted(p for p in folder.rglob("*")
                  if p.is_file() and p.suffix.lower() in SUPPORTED and not p.name.startswith("~$")
                  and not any(part.startswith(".") for part in p.relative_to(folder).parts))


def read_text(path: Path) -> str:
    raw = path.read_bytes()
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16")
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("cp1252", errors="replace")           # older Windows text files


def read_docx(path: Path) -> str:
    """Word -> text in reading order; Word headings become markdown headings so the sections chunker can use them."""
    import docx
    lines = []
    for item in docx.Document(str(path)).iter_inner_content():
        if hasattr(item, "rows"):                                # a table: one line per row
            lines += [" | ".join(cell.text.strip() for cell in row.cells) for row in item.rows]
            continue
        text = item.text.strip()
        if not text:
            continue
        style = item.style.name if item.style is not None else ""
        level = re.match(r"Heading (\d)", style or "")
        lines.append("#" * int(level.group(1)) + " " + text if level else ("# " + text if style == "Title" else text))
    return "\n".join(lines)


def read_file(path: Path) -> tuple[dict, list[tuple[int, str]]]:
    """-> ({title, source}, [(page, text), ...]). PDFs keep their pages (for citations); other formats are page 0."""
    meta, pages = _read_file(path)
    # Windows files end lines with \r\n; the chunkers expect \n (a heading ending in \r never matches its own text)
    return meta, [(page, text.replace("\r\n", "\n").replace("\r", "\n")) for page, text in pages]


def _read_file(path: Path) -> tuple[dict, list[tuple[int, str]]]:
    ext = path.suffix.lower()
    meta = {"title": path.stem, "source": str(path)}
    if ext == ".pdf":
        from pypdf import PdfReader
        return meta, [(n, page.extract_text() or "") for n, page in enumerate(PdfReader(str(path)).pages, 1)]
    if ext == ".docx":
        return meta, [(0, read_docx(path))]
    text = read_text(path).replace("\r\n", "\n")
    if ext in (".html", ".htm"):
        text = html.unescape(re.sub(r"(?is)<(script|style)\b.*?</\1>", "", text))
    if ext in (".md", ".markdown") and text.startswith("---"):    # YAML front matter: keep its title and source
        front, sep, body = text[3:].partition("\n---\n")
        if sep:
            for key in ("title", "source"):
                found = re.search(rf'^{key}:\s*"?(.*?)"?\s*$', front, re.M)
                if found and found.group(1):
                    meta[key] = found.group(1)
            text = body
    return meta, [(0, text)]


# ----------------------------------------------------------------------------------------------- clean + chunk (the lab's code)

def clean(text):
    text = re.sub(r"\{\{[<%].*?[>%]\}\}|<!--.*?-->|<[^>\n]+>", "", text, flags=re.S)    # template shortcodes, HTML comments and tags
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)                                 # [link text](url) -> link text
    return re.sub(r"[ \t]+", " ", text).strip()                                          # collapse runs of spaces


def chunk_fixed(text, size=300):
    """Split into pieces of about `size` characters, on word boundaries."""
    pieces, current = [], ""
    for word in text.split():
        if len(current) + len(word) > size:
            pieces.append(current.strip()); current = ""
        current += word + " "
    return pieces + [current.strip()]


def chunk_sections(text, max_size=800):
    """Split on markdown headings; keep the heading with its text; cut very long sections with chunk_fixed."""
    pieces = []
    for section in re.split(r"\n(?=#{1,4} )", text):
        heading = section.split("\n")[0] if section.startswith("#") else ""
        pieces += [p if p.startswith(heading) else heading + "\n" + p for p in chunk_fixed(section, max_size)]
    return [p for p in pieces if len(p) > 60]


CHUNKERS = {   # key -> (dropdown label, one-argument chunker), the four strategies of the lab
    "sections": ("sections: split on headings, max 800 characters", chunk_sections),
    "medium": ("medium: 300 characters", lambda t: chunk_fixed(t, 300)),
    "tiny": ("tiny: 100 characters", lambda t: chunk_fixed(t, 100)),
    "huge": ("huge: 3,000 characters", lambda t: chunk_fixed(t, 3000)),
}


@dataclass
class Chunk:
    id: str
    text: str
    meta: dict


def chunk_folder(folder, chunking="sections", progress=None, cancel=None) -> tuple[list[Chunk], int, list[str]]:
    """Read every supported file under `folder` -> (chunks, number of files, problems worth showing the user)."""
    folder = Path(folder)
    files = find_files(folder)
    if not files:
        raise ValueError(f"No supported files in {folder}.\nLocalRAG reads: {' '.join(SUPPORTED)}")
    chunker = CHUNKERS[chunking][1]
    chunks, problems = [], []
    for n, path in enumerate(files):
        if cancel is not None and cancel.is_set():
            raise Cancelled()
        if progress:
            progress(n / len(files), f"Reading {path.name} ({n + 1} of {len(files)})")
        try:
            meta, pages = read_file(path)
        except Exception as err:                                   # a broken or password-protected file: skip it
            problems.append(f"{path.name}: could not be read ({err})")
            continue
        updated = int(datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y%m%d"))
        before = len(chunks)
        for page, text in pages:
            for i, piece in enumerate(p for p in chunker(clean(text)) if p.strip()):
                chunks.append(Chunk(f"{n}-{page}-{i}", piece,
                                    {"title": meta["title"], "source": meta["source"], "page": page,
                                     "file": str(path.relative_to(folder)), "updated": updated}))
        if len(chunks) == before:
            problems.append(f"{path.name}: no text found (a scanned PDF needs OCR first)")
    return chunks, len(files), problems


# ----------------------------------------------------------------------------------------------- embeddings + vector DB

class Embedder:
    def __init__(self, choice: EmbeddingChoice, progress=None):
        path = fetch_model(choice.id, progress)
        if progress:
            progress(None, f"Loading {choice.label}...")
        from sentence_transformers import SentenceTransformer
        self.choice = choice
        self.model = SentenceTransformer(path, device="cuda" if cuda_ok() else "cpu")

    def embed(self, texts, prompt="", batch_size=32):
        """Unit-length vectors, so cosine similarity = dot product."""
        return self.model.encode(list(texts), prompt=prompt, batch_size=batch_size, normalize_embeddings=True,
                                 convert_to_numpy=True, show_progress_bar=False)

    def embed_query(self, question):
        return self.embed([question], self.choice.query_prompt)[0]


def _chroma(db_dir):
    import chromadb
    from chromadb.config import Settings
    return chromadb.PersistentClient(path=str(db_dir), settings=Settings(anonymized_telemetry=False))


@dataclass
class Hit:
    score: float     # cosine similarity, 1 = same meaning
    meta: dict
    text: str
    id: str = ""     # the chunk's id in ChromaDB: <file number>-<page>-<chunk number>


class KnowledgeBase:
    """A ChromaDB collection on disk plus the embedding model it was built with."""

    def __init__(self, db_dir: Path, manifest: dict, collection, embedder: Embedder):
        self.db_dir, self.manifest, self.collection, self.embedder = Path(db_dir), manifest, collection, embedder

    @property
    def count(self) -> int:
        return self.collection.count()

    def summary(self) -> str:
        m = self.manifest
        return (f"{m['chunks']:,} chunks from {m['files']:,} files · {self.embedder.choice.label} · "
                f"chunking: {m['chunking']} · built {m['created'][:10]}")

    @classmethod
    def build(cls, docs_folder, db_dir, embedding: EmbeddingChoice, chunking="sections", progress=None, cancel=None):
        docs_folder, db_dir = Path(docs_folder).resolve(), Path(db_dir).resolve()
        if db_dir == docs_folder or docs_folder in db_dir.parents:
            raise ValueError("Save the DB outside the documents folder, so it is not read as a document next time.")
        chunks, n_files, problems = chunk_folder(docs_folder, chunking, progress, cancel)
        if not chunks:
            raise ValueError("No text found in those files.\n\n" + "\n".join(problems[:20]))
        embedder = Embedder(embedding, progress)
        db_dir.mkdir(parents=True, exist_ok=True)
        client = _chroma(db_dir)
        try:
            client.delete_collection(COLLECTION)                     # rebuilding into the same folder replaces the DB
        except Exception:
            pass
        collection = client.create_collection(COLLECTION, metadata={"hnsw:space": "cosine"},   # cosine: direction, not length
                                              embedding_function=None)                         # we pass our own vectors
        batch = 64
        for start in range(0, len(chunks), batch):
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            part = chunks[start:start + batch]
            collection.add(ids=[c.id for c in part], documents=[c.text for c in part], metadatas=[c.meta for c in part],
                           embeddings=embedder.embed([c.text for c in part], embedding.doc_prompt))
            if progress:
                done = start + len(part)
                progress(done / len(chunks), f"Embedding chunks: {done:,} of {len(chunks):,}")
        manifest = {"app": APP_NAME, "app_version": APP_VERSION, "embedding_model": embedding.id, "chunking": chunking,
                    "source_folder": str(docs_folder), "files": n_files, "chunks": len(chunks),
                    "created": datetime.now().isoformat(timespec="seconds"), "problems": problems}
        (db_dir / MANIFEST).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        log.info("built %s: %s", db_dir, manifest)
        return cls(db_dir, manifest, collection, embedder)

    @classmethod
    def open(cls, db_dir, progress=None):
        db_dir = Path(db_dir).resolve()
        if not (db_dir / MANIFEST).exists():
            raise ValueError(f"{db_dir}\nis not a LocalRAG DB: it has no {MANIFEST}.\n"
                             "Pick the folder you saved the DB to when you built it.")
        manifest = json.loads((db_dir / MANIFEST).read_text(encoding="utf-8"))
        collection = _chroma(db_dir).get_collection(COLLECTION)
        embedder = Embedder(embedding_by_id(manifest["embedding_model"]), progress)   # questions must use the DB's own model
        return cls(db_dir, manifest, collection, embedder)

    def search(self, question, k=3) -> list[Hit]:
        """The k chunks closest in meaning to the question."""
        r = self.collection.query(query_embeddings=[self.embedder.embed_query(question)],
                                  n_results=max(1, min(k, self.count)), include=["documents", "metadatas", "distances"])
        return [Hit(round(1 - dist, 3), meta, doc, chunk_id)
                for chunk_id, doc, meta, dist in zip(r["ids"][0], r["documents"][0], r["metadatas"][0], r["distances"][0])]


# ----------------------------------------------------------------------------------------------- the language model

SYSTEM_PROMPT = ("Answer ONLY from the context passages. Cite them like [1]. "
                 "If the context does not contain the answer, say so.")


def prompt_messages(question, hits):
    """The lab's grounded prompt: numbered passages, then the question."""
    context = "\n\n".join(f"[{i}] ({h.meta['title']}) {h.text}" for i, h in enumerate(hits, 1))
    return [{"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"Context:\n{context}\n\nQuestion: {question}"}]


class ChatModel:
    """One language model in memory at a time: load() frees the previous model before the next one arrives."""

    def __init__(self):
        self.model = self.tok = None
        self.name = self.where = ""
        self.last = {}           # the last generation: the exact prompt the model read, token counts, timings

    @property
    def ready(self) -> bool:
        return self.model is not None

    def unload(self):
        if self.model is None:
            return
        import torch
        self.model = self.tok = None
        self.name = self.where = ""
        gc.collect()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()

    def load(self, model_id: str, progress=None):
        path = fetch_model(model_id, progress)
        self.unload()
        if progress:
            progress(None, f"Loading {short_name(model_id)} into memory...")
        from transformers import AutoConfig, AutoModelForCausalLM, AutoModelForImageTextToText, AutoTokenizer
        config = AutoConfig.from_pretrained(path)
        # Qwen3.5 is an image-text-to-text model, loaded as in the lab; the other models are text-only
        auto = AutoModelForImageTextToText if getattr(config, "vision_config", None) is not None else AutoModelForCausalLM
        weights = sum(f.stat().st_size for f in Path(path).rglob("*.safetensors")) or \
            sum(f.stat().st_size for f in Path(path).rglob("*.bin"))
        kwargs, where = _placement(weights)
        log.info("loading %s from %s with %s", model_id, path, kwargs)
        self.tok = AutoTokenizer.from_pretrained(path)
        self.model = auto.from_pretrained(path, **kwargs).eval()
        self.name, self.where = short_name(model_id), where

    def stream(self, messages, max_new_tokens=512, stop: threading.Event | None = None):
        """Yield the reply piece by piece. Greedy decoding, like the lab's chat() at temperature 0.
        Afterwards self.last holds the prompt exactly as the model read it, the token counts and the timings."""
        import torch
        from transformers import StoppingCriteria, StoppingCriteriaList, TextIteratorStreamer
        prompt = self.tok.apply_chat_template(messages, tokenize=False, add_generation_prompt=True, enable_thinking=False)
        inputs = self.tok(prompt, return_tensors="pt").to(self.model.device)
        n_prompt = int(inputs["input_ids"].shape[1])
        self.last = {"model": self.name, "where": self.where, "prompt": prompt, "prompt_tokens": n_prompt,
                     "max_new_tokens": max_new_tokens, "decoding": "greedy (do_sample=False)", "new_tokens": 0}
        streamer = TextIteratorStreamer(self.tok, skip_prompt=True, skip_special_tokens=True)

        class StopButton(StoppingCriteria):
            def __call__(self, input_ids, scores, **kwargs):
                return torch.full((input_ids.shape[0],), stop is not None and stop.is_set(), dtype=torch.bool,
                                  device=input_ids.device)

        failure = []

        def generate():
            try:
                with torch.no_grad():
                    out = self.model.generate(**inputs, max_new_tokens=max_new_tokens, do_sample=False,
                                              temperature=None, top_p=None, top_k=None, streamer=streamer,
                                              pad_token_id=self.tok.pad_token_id if self.tok.pad_token_id is not None
                                              else self.tok.eos_token_id,
                                              stopping_criteria=StoppingCriteriaList([StopButton()]))
                self.last["new_tokens"] = int(out.shape[1] - n_prompt)
            except Exception as err:
                failure.append(err)
                streamer.end()                                   # unblock the loop below

        start, first = time.perf_counter(), None
        worker = threading.Thread(target=generate, daemon=True)
        worker.start()
        for piece in streamer:
            if first is None and piece.strip():
                first = time.perf_counter() - start
            yield piece
        worker.join()
        total = time.perf_counter() - start
        first = total if first is None else first
        self.last.update(first_token_s=round(first, 2), total_s=round(total, 2),
                         tokens_per_s=round(self.last["new_tokens"] / max(total - first, 1e-6), 2))
        if failure:
            raise failure[0]
