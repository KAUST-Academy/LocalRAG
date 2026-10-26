"""Measure every embedding model in the catalog on the lab's questions, as section 5 and 7 of the notebook do:
how many of the 14 answers land in the top 3, and where the similarity gate should sit (in the gap between the
best similarity of questions the handbook answers and of questions it cannot answer).

    .venv\\Scripts\\python tools\\calibrate.py [--chunking sections]

The thresholds it suggests go into rag_engine.EMBEDDINGS.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import rag_engine as rag  # noqa: E402

EVAL = [  # (question, text that must appear in the retrieved context), from Day2_RAG_Pipeline.ipynb
    ("How many days of time off should I take at minimum each year?", "minimum 25 days"),
    ("What percentage of my salary does the company pay into the UK pension?", ["GitLab: 4%", "GitLab contributes 4%"]),
    ("Which private medical insurance provider covers team members in Ireland?", "through VHI"),
    ("What is the daily limit for meals while traveling for work?", "$120 USD"),
    ("How many weeks of paid parental leave do team members get?", "16 weeks"),
    ("What is the minimum password length?", "12 characters"),
    ("How long can a personal access token be valid before it expires?", "Personal Access Tokens - 30 days"),
    ("Can I install Chrome extensions on my work laptop?", "Chrome Extensions"),
    ("What is the maximum value of a gift to a US government official per event?", "$20 USD"),
    ("How do I return my laptop when I leave?", "prepaid shipping label"),
    ("How long are production change records retained?", "Production changes | 3 years"),
    ("Are interns eligible for referral bonuses?", "not eligible for referral bonuses"),
    ("How many vacation days are Netherlands team members entitled to?", "20 vacation days"),
    ("What is the reimbursement limit for internet while traveling?", "up to $50 USD"),
]
OUT_OF_SCOPE = [  # the lab's four, plus six more so one odd question cannot set the gate
    "Who won the 2022 World Cup?", "How do I fix a flat bicycle tire?", "Best pizza recipe?",
    "What is the capital of France?", "How tall is Mount Everest?", "Who wrote Romeo and Juliet?",
    "How do I bake sourdough bread?", "What is the speed of light?", "How do I train for a marathon?",
    "Which planet has the most moons?",
]


def has_answer(text, ans):
    return any(a.lower() in text.lower() for a in ([ans] if isinstance(ans, str) else ans))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--chunking", default="sections", choices=list(rag.CHUNKERS))
    args = parser.parse_args()
    docs = Path(__file__).resolve().parent.parent / "sample_docs" / "gitlab_handbook"
    chunks, n_files, _ = rag.chunk_folder(docs, args.chunking)
    print(f"{len(chunks)} chunks from {n_files} files, chunking={args.chunking}\n")
    print(f"{'model':<40} {'top-3 hits':>10} {'in-scope best':>15} {'out-of-scope best':>18} {'gap':>11}  suggest")
    for choice in rag.EMBEDDINGS:
        emb = rag.Embedder(choice)
        vectors = emb.embed([c.text for c in chunks], choice.doc_prompt)        # unit vectors: dot = cosine

        def top(question, k=3):
            sims = vectors @ emb.embed_query(question)
            return [(float(sims[i]), chunks[i].text) for i in sims.argsort()[::-1][:k]]

        found = sum(any(has_answer(text, ans) for _, text in top(q)) for q, ans in EVAL)
        inside = sorted(top(q, 1)[0][0] for q, _ in EVAL)
        outside = sorted(top(q, 1)[0][0] for q in OUT_OF_SCOPE)
        low_in, high_out = inside[0], outside[-1]
        gate = round((low_in + high_out) / 2 / 0.05) * 0.05 if low_in > high_out else high_out + 0.01
        print(f"{choice.id:<40} {found:>7}/14 {low_in:>7.2f}-{inside[-1]:.2f} {outside[0]:>10.2f}-{high_out:.2f} "
              f"{high_out:>5.2f}-{low_in:.2f}  {gate:.2f}{'' if low_in > high_out else '  (no gap!)'}")
        print(f"{'':<40} in:  {' '.join(f'{s:.2f}' for s in inside)}")
        print(f"{'':<40} out: {' '.join(f'{s:.2f}' for s in outside)}")
        del emb, vectors


if __name__ == "__main__":
    main()
