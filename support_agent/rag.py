"""RAG simplu și transparent: chunk-uri pe secțiuni + BM25 cu normalizare pentru română.

Fără embeddings sau servicii externe: rezultatele sunt deterministe, ușor de evaluat,
iar fiecare chunk are un `source_id` stabil (fișier#secțiune) folosit pentru citări.
"""
import math
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from . import config

STOPWORDS = set("""
a ai al ale am ar are as au ca care ce cel cea cei cele cu cum da dar de din dupa e este
eu fi fie in la le li lor lui ma mai mi ne nu o or ori pe pentru prin sa se si sunt
te tu un una unei unui va vor vreau iar daca cand unde cat cate catre doar poate pot
""".split())

# Expansiune de interogare: cuvintele clienților -> vocabularul documentației.
SYNONYMS = {
    "bani": "rambursare", "banii": "rambursare", "refund": "rambursare", "inapoi": "rambursare",
    "laptop": "electronice", "telefon": "electronice", "tableta": "electronice", "casti": "electronice",
    "zgarieturi": "uzura", "zgariat": "uzura", "folosit": "uzura",
    "apa": "lichide", "varsat": "lichide", "udat": "lichide",
    "reparat": "service", "repara": "service", "stricat": "defect",
    "intarzie": "intarziat", "ajuns": "colet", "spart": "deteriorat",
    "orar": "program", "deschis": "program", "ajunge": "livrare", "timp": "dureaza",
}

# Sub acest scor / această acoperire considerăm că documentația nu acoperă întrebarea.
MIN_SCORE = 1.5
MIN_COVERAGE = 0.4
TITLE_BONUS = 1.5  # fracțiunea din termenii întrebării care apar în chunk


@dataclass
class Chunk:
    source_id: str
    title: str
    text: str


@dataclass
class Hit:
    chunk: Chunk
    score: float

    def as_dict(self) -> dict:
        return {"source_id": self.chunk.source_id, "title": self.chunk.title,
                "text": self.chunk.text, "score": round(self.score, 2)}


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in text if not unicodedata.combining(c))


def tokenize(text: str) -> list[str]:
    # Stemming grosier prin prefix: „rambursare”, „rambursarea”, „rambursez” -> „rambu”.
    return [t[:5] for t in re.findall(r"\w+", normalize(text)) if t not in STOPWORDS and len(t) > 1]


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", normalize(text)).strip("-")


def load_chunks(kb_dir: Path) -> list[Chunk]:
    chunks = []
    for path in sorted(kb_dir.glob("*.md")):
        doc_title = path.stem
        section, lines = None, []

        def flush():
            body = " ".join(l.strip() for l in lines if l.strip())
            if section and body:
                chunks.append(Chunk(f"{path.stem}#{slugify(section)}", f"{doc_title} › {section}", body))

        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("# "):
                doc_title = line[2:].strip()
            elif line.startswith("## "):
                flush()
                section, lines = line[3:].strip(), []
            else:
                lines.append(line)
        flush()
    return chunks


class KnowledgeBase:
    def __init__(self, kb_dir: Path | None = None, k1: float = 1.5, b: float = 0.75):
        self.chunks = load_chunks(kb_dir or config.KB_DIR)
        self.k1, self.b = k1, b
        # Titlul secțiunii contează dublu: e cel mai dens semnal.
        self.docs = [Counter(tokenize(c.title) * 2 + tokenize(c.text)) for c in self.chunks]
        self.lengths = [sum(d.values()) for d in self.docs]
        self.section_titles = [set(tokenize(c.title.split("›")[-1])) for c in self.chunks]
        self.avgdl = sum(self.lengths) / max(len(self.lengths), 1)
        df = Counter(t for d in self.docs for t in d)
        n = len(self.docs)
        self.idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}
        self.by_id = {c.source_id: c for c in self.chunks}

    def search(self, query: str, k: int = 3) -> list[Hit]:
        # fiecare cuvânt din întrebare = un grup {stem, stem-ul sinonimului}
        words = [w for w in re.findall(r"\w+", normalize(query)) if w not in STOPWORDS and len(w) > 1]
        groups = {}
        for w in words:
            groups.setdefault(w[:5], {w[:5]}).update(tokenize(SYNONYMS.get(w, "")))
        hits = []
        for chunk, doc, dl, stitle in zip(self.chunks, self.docs, self.lengths, self.section_titles):
            score, covered = 0.0, 0
            for alts in groups.values():
                best = 0.0
                for t in alts:
                    f = doc.get(t, 0)
                    if f:
                        best = max(best, self.idf[t] * f * (self.k1 + 1)
                                   / (f + self.k1 * (1 - self.b + self.b * dl / self.avgdl)))
                score += best
                covered += best > 0
            coverage = covered / max(len(groups), 1)
            # bonus când întrebarea „numește” secțiunea (ex. „termene de livrare”)
            if stitle:
                score += TITLE_BONUS * sum(1 for alts in groups.values() if alts & stitle) / len(stitle)
            if score >= MIN_SCORE and coverage >= MIN_COVERAGE:
                hits.append(Hit(chunk, score))
        hits.sort(key=lambda h: h.score, reverse=True)
        return hits[:k]


def best_sentences(text: str, query: str, n: int = 2) -> str:
    """Răspuns extractiv: propozițiile cu cea mai mare suprapunere cu întrebarea, în ordinea originală."""
    sentences = re.split(r"(?<=[.!?])\s+", text)
    q = set(tokenize(query))
    ranked = sorted(range(len(sentences)), key=lambda i: (-len(q & set(tokenize(sentences[i]))), i))
    return " ".join(sentences[i] for i in sorted(ranked[:n]))
