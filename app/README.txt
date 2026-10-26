LocalRAG 1.0.0: ask your documents                                         KAUST Academy
====================================

The Day 2 RAG lab as a Windows program. Build a vector database from a folder of your documents, then ask
questions that are answered from those documents, with the sources cited. Every step is shown and logged.


START
  1. Unzip the whole folder first. LocalRAG.exe needs the _internal folder next to it.
  2. Double-click LocalRAG.exe.
     If Windows says "Windows protected your PC", click "More info", then "Run anyway".
     (The program is not code-signed, so Windows does not know it yet.)


USE
  01 Knowledge base: pick a folder of documents (.pdf .docx .txt .md .csv .html) and click "Build DB",
     or open a DB you built before. The sample_docs folder holds the 38 GitLab handbook pages from the lab.
  02 Language model: pick one and click "Load model". The first time, it downloads (Qwen3.5-4B: 9.3 GB);
     after that it loads from this PC, also without internet. One model is in memory at a time.
  03 Ask. Answers cite passages like [1]; click a source to open the file or web page.
     Below the similarity gate the program answers "I don't have that information" instead of guessing.


WHAT YOU CAN SEE
  Conversation       questions, answers, sources, the retrieved passages, timings
  Retrieved context  every passage the DB returned, its similarity, and the gate decision
  Prompt             the exact prompt the language model received, after its chat template
  Log                everything the program does, live

  Each session is also written to %LOCALAPPDATA%\LocalRAG\sessions as a readable .md file (questions,
  passages, prompts, answers, timings) and a .jsonl file for analysis. File > Save this session's log as...


WHAT YOUR PC NEEDS
  Windows 10 or 11, 64-bit.
  Qwen3.5-4B: 16 GB of RAM, or an NVIDIA GPU with 12 GB or more.
  Qwen3.5-0.8B and 2B: 8 GB of RAM. Qwen3.5-2B fits on an 8 GB NVIDIA GPU.
  An NVIDIA GPU is used automatically when the model fits in its memory; otherwise the CPU is used.
  Disk space for the models you download (2-20 GB each).


WHERE THINGS GO
  Models             %USERPROFILE%\.cache\huggingface   (shared with other Hugging Face tools)
  Databases          where you choose (default: Documents\LocalRAG databases)
  Session logs       %LOCALAPPDATA%\LocalRAG\sessions
  Settings, logs     %LOCALAPPDATA%\LocalRAG
  No internet?       Put a model folder in models\<name> next to LocalRAG.exe, for example
                     models\Qwen3.5-4B, and the program uses it instead of downloading.
                     The embedding model of the lab (all-MiniLM-L6-v2) is already there.


Sample documents: GitLab Handbook (https://handbook.gitlab.com), CC BY-SA 4.0.
Fonts: Raleway and Open Sans, SIL Open Font License (assets\fonts in the _internal folder).
