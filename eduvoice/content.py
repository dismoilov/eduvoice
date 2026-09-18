"""Call-centre content: the FAQ answers the bot is allowed to give.

Answers live in content/faq.yaml so a non-programmer can edit them. Each entry may list
keywords: an obvious match is answered without calling the language model, which removes
about a second of delay.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import yaml

log = logging.getLogger("eduvoice.content")

# Uzbek writes oʻ/gʻ and the tutuq belgisi with U+02BB/U+02BC, but recognition returns a
# plain apostrophe and people type yet another one. For matching they are all the same
# letter, so every variant is folded before keywords are compared.
APOSTROPHES = str.maketrans({"'": "ʼ", "\u2018": "ʼ", "\u2019": "ʼ", "\u02bb": "ʼ", "`": "ʼ"})


def fold(text: str) -> str:
    """Lower case with every apostrophe variant folded to one."""
    return text.lower().translate(APOSTROPHES)


@dataclass(slots=True)
class FaqEntry:
    faq_id: str
    answer: str
    keywords: list[str] = field(default_factory=list)
    source: str = ""


class Faq:
    def __init__(self, entries: dict[str, FaqEntry]) -> None:
        self._entries = entries

    @classmethod
    def load(cls, content_dir: Path) -> Faq:
        path = content_dir / "faq.yaml"
        if not path.exists():
            log.warning("no FAQ file at %s: the bot will rely on the model only", path)
            return cls({})
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        entries = {
            faq_id: FaqEntry(
                faq_id=faq_id,
                answer=" ".join(str(item.get("answer", "")).split()),
                keywords=[fold(str(k)) for k in item.get("keywords", [])],
                source=str(item.get("source", "")),
            )
            for faq_id, item in raw.items()
        }
        log.info("loaded %d FAQ answers", len(entries))
        return cls(entries)

    def answers(self) -> dict[str, str]:
        return {faq_id: entry.answer for faq_id, entry in self._entries.items()}

    def entry(self, faq_id: str) -> FaqEntry | None:
        """The whole entry, including the document the answer is based on."""
        return self._entries.get(faq_id)

    def match(self, question: str) -> FaqEntry | None:
        """Keyword shortcut: the fastest possible answer, no model involved.

        The longest matching keyword wins, not the first one found. Order used to decide
        it, and the general answer is always written before the particular one, so
        "akademik taʼtilda stipendiya toʻlanadimi" — is a stipend paid during academic
        leave — matched the plain keyword "stipendiya" and the caller was read the general
        rule about stipends instead of the rule that answers their question. Length is a
        fair measure of how specific a keyword is: "akademik taʼtilda stipendiya" says
        more than "stipendiya", and it says it about the same question.
        """
        text = fold(question)
        best: FaqEntry | None = None
        best_length = 0
        for entry in self._entries.values():
            for keyword in entry.keywords:
                if keyword and keyword in text and len(keyword) > best_length:
                    best, best_length = entry, len(keyword)
        return best

    @classmethod
    def from_records(cls, records: dict[str, dict]) -> Faq:
        """Answers published in the CRM: the same shape as the YAML file, from the database."""
        return cls(
            {
                faq_id: FaqEntry(
                    faq_id=faq_id,
                    answer=" ".join(str(record.get("answer", "")).split()),
                    keywords=[fold(str(word)) for word in record.get("keywords", [])],
                    source=str(record.get("source", "")),
                )
                for faq_id, record in records.items()
            }
        )

    @classmethod
    def from_answers(cls, answers: dict[str, str]) -> Faq:
        return cls({faq_id: FaqEntry(faq_id, answer) for faq_id, answer in answers.items()})

    def __len__(self) -> int:
        return len(self._entries)
