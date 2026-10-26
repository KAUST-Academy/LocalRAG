# PyInstaller recipe for LocalRAG.exe: a one-folder, windowed build.
# Build with build.ps1, or by hand:   .venv\Scripts\python -m PyInstaller LocalRAG.spec --noconfirm
import os
import re

from PyInstaller.utils.hooks import collect_all, collect_submodules, copy_metadata

datas = [("assets", "assets")]     # (folder in the project, folder inside the bundle): icon, logos, brand fonts
binaries = []
hiddenimports = []

# chromadb picks its parts at run time from strings in its settings ("chromadb.api.rust.RustBindingsAPI", ...),
# which static analysis cannot follow: collect the whole package (modules, data files, DLLs).
d, b, h = collect_all("chromadb")
datas, binaries, hiddenimports = datas + d, binaries + b, hiddenimports + h

# transformers imports each model's code lazily, by name, when a model is loaded. The app accepts any model id,
# so every model family goes in (a few MB of .py files).
hiddenimports += collect_submodules("transformers")
hiddenimports += collect_submodules("sentence_transformers")

# Packages that look themselves up with importlib.metadata at run time need their metadata folder too.
# (opentelemetry, used by chromadb, finds its context implementation through an entry point.)
for dist in ("chromadb", "opentelemetry-api", "opentelemetry-sdk", "transformers", "sentence-transformers",
             "huggingface-hub", "tokenizers", "safetensors", "accelerate", "torch"):
    datas += copy_metadata(dist)

a = Analysis(
    ["local_rag.py"],
    datas=datas,
    binaries=binaries,
    hiddenimports=hiddenimports,
    excludes=[
        # Never imported on the app's path, but reachable from chromadb's unused server, cloud and default-ONNX code.
        # (The frozen self-test proves the app runs without them.)
        "kubernetes", "onnxruntime",
        # Development tools that some packages mention.
        "IPython", "jupyter_client", "notebook", "matplotlib", "pytest", "tensorboard", "torch.utils.tensorboard",
    ],
    noarchive=False,
)
# cuDNN's engine and operation libraries (nine cudnn_*64_9.dll, 436 MB) only speed up convolutions; without them the
# CUDA build's zip fits GitHub's 2 GiB file limit. rag_engine.cuda_ok() switches cuDNN off so they are never loaded.
# The loader cudnn64_9.dll (linked by torch_cuda.dll) and its core cudnn_graph64_9.dll stay: PyTorch still asks the
# loader for cuDNN's version, and without the core library the program aborts inside cudnn64_9.dll.
a.binaries = [b for b in a.binaries if not re.match(r"cudnn_(?!graph)\w+64_9\.dll$", os.path.basename(b[0]), re.I)]
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,             # one-folder build: DLLs stay next to the .exe instead of unpacking on each start
    name="LocalRAG",
    icon="assets/icon.ico",
    version="version_info.txt",        # what Windows shows under Properties > Details
    console=False,                     # a window app: no black console behind it
    upx=False,                         # UPX-packed DLLs break CUDA and set off antivirus software
)
coll = COLLECT(exe, a.binaries, a.datas, name="LocalRAG", upx=False)
