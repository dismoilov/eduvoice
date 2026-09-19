"""The regulations themselves, clause by clause, searched locally.

The FAQ answers six questions. Everything else used to end with "I did not understand
you" and a transfer, even when the answer was written down in a decree the ministry
publishes itself. This module keeps those decrees in the same database and finds the
clause that answers a question, so the model can be made to answer *from the law* and
cite it, rather than from memory.

The search is local on purpose. lex.uz is downloaded once by `make lex-fetch`; at the
defence there may be no internet, a caller cannot wait for a web page to load, and a
citation nobody can reproduce is worth nothing. What is in the database is exactly what
was published, and `element` makes every citation a link to that clause on lex.uz.
"""

from __future__ import annotations

import html
import logging
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from store.db import connect, now, transaction

log = logging.getLogger("store.lex")

DOC_URL = "https://lex.uz/uz/docs/{doc_id}"

# Every apostrophe Uzbek is written with, plus the ones keyboards and recognisers
# produce. They are removed rather than unified: the full-text tokenizer treats an
# apostrophe as a word break, so "taʼtil" would be indexed as two words and never found.
APOSTROPHES = (
    "'`"
    "\u2018\u2019"  # curly quotes, which is what a phone keyboard inserts
    "\u02bb\u02bc"  # oʻ/gʻ and the tutuq belgisi — how lex.uz actually writes Uzbek
    "\u00b4"  # acute accent, seen in text pasted from Word
)

# Words that carry no meaning for retrieval. A question is mostly these, and searching
# for them returns the whole corpus ranked by nothing.
_STOPWORDS = """
    qanday qachon nima nimaga qaysi qancha necha kim kimga uchun bilan haqida boyicha
    boladi bolsa boladimi bormi kerak mumkin emas yoki ham ammo lekin agar shu bu
    ushbu men meni mening sizga siz ular ularni bir har hamma yana faqat endi
    qilib qilish etish berish olish togrisida menga aytib ayting iltimos
    """
STOPWORDS = frozenset(_STOPWORDS.split())

# What a caller says against what a decree writes. "How much does it cost" — "qancha
# turadi" — contains no word a decree about payment uses: "qancha" is in every second
# question and "turadi" is not in the law at all, so the clause on the cost of a hall
# of residence lost to three clauses about allocating one, and the caller was told the
# assistant did not understand. Each entry adds the decree's own words for the idea.
# Measured on live calls from the venue, 19.09; keep it short and keep it about ideas.
# "Qancha" itself is not here: it is "how much" but also "how long" and "how many",
# and expanding it made "how long does this take" find a clause on contract payment.
EXPANSIONS: dict[str, tuple[str, ...]] = {
    "narx": ("tolov", "miqdor"),  # price -> payment, amount
    "narxi": ("tolov", "miqdor"),
    "turadi": ("tolov", "miqdor"),  # (it) costs
    "summa": ("tolov", "miqdor"),
    "pul": ("tolov", "miqdor"),
    "pulni": ("tolov", "miqdor"),
    "yotoqxona": ("turar", "joyi"),  # dormitory -> "talabalar turar joyi"
    "yotoqxonaga": ("turar", "joyi"),
    "yotoqxonada": ("turar", "joyi"),
    "qachon": ("muddat",),  # when -> deadline, term
    "qachongacha": ("muddat",),
}

# How much of a word is treated as its stem. Uzbek glues its endings on, and the caller
# uses a different one from the decree: they ask about "taʼtilda" where the decree says
# "taʼtil", about "magistraturaga" where it says "magistratura". A prefix search only
# works from the short form to the long one, so the *question* is cut back to the stem
# and matched as a prefix. Five characters is what separates "stipendiya/stipendiyalar"
# and "toʻlanadi/toʻlanadimi" without merging words that mean different things; the
# two-word floor below and the model's own refusal catch what slips through.
STEM_CHARS = 5

# A clause has to share at least this many meaningful words with the question before it
# is offered to the model. Without a floor the search always returns *something*: the
# query is an OR of every word, and ranking alone would hand over the best of a bad lot,
# which is how a confident answer to a question nobody asked gets read out on the phone.
MIN_MATCHING_TERMS = 2

# A word is distinctive if it appears in no more than this share of the corpus. Above it
# the word is grammar, not subject matter.
RARE_FRACTION = 0.10

# How many following elements may be pulled in when a clause ends in a colon, i.e. when
# it is the opening line of a list whose items are separate elements.
CONTINUATION_CLAUSES = 6

# Clauses shorter than this are headings and numbering ("3-bob.", "ILOVA"), not rules.
MIN_CLAUSE_CHARS = 40

RELOAD_AFTER_S = 60.0


def fold(text: str) -> str:
    """Lower case, apostrophes removed: the one spelling both sides are compared in."""
    lowered = text.lower()
    return "".join(ch for ch in lowered if ch not in APOSTROPHES)


def terms(question: str) -> list[str]:
    """The stems of a question's meaningful words, in the spelling the index uses.

    Plus the decree's words for ideas the caller put differently — see `EXPANSIONS`.
    """
    words = re.findall(r"[\w]+", fold(question), flags=re.UNICODE)
    stems = [w[:STEM_CHARS] for w in words if len(w) >= 4 and w not in STOPWORDS]
    for word in words:
        stems.extend(extra[:STEM_CHARS] for extra in EXPANSIONS.get(word, ()))
    return list(dict.fromkeys(stems))  # in order, without repeats


@dataclass(slots=True, frozen=True)
class Clause:
    """One numbered clause of one decree, with everything needed to quote it."""

    number: str  # "3807-son"
    title: str
    doc_id: str
    element: str  # lex.uz's own anchor for this clause
    band: str  # "36" — the clause number as printed
    text: str

    @property
    def link(self) -> str:
        """Straight to this clause on lex.uz, not to the top of a hundred-page decree."""
        url = DOC_URL.format(doc_id=self.doc_id)
        return f"{url}#{self.element}" if self.element else url

    @property
    def citation(self) -> str:
        """What the assistant says and the CRM shows: decree and clause."""
        band = f", {self.band}-band" if self.band else ""
        return f"{self.number}{band}" if self.number else self.title[:60]


@dataclass(slots=True)
class Document:
    doc_id: str
    number: str
    title: str
    adopted_at: str
    clauses: list[tuple[str, str, str]]  # element, band, text


_CLAUSE = re.compile(r'<div name="(-?\d+)" id="\1"[^>]*>(.*?)</div>', re.S)
_TAGS = re.compile(r"(?s)<[^>]+>")
_SPACES = re.compile(r"\s+")
_TITLE = re.compile(r"(?is)<title>(.*?)</title>")
# "3807-сон 03.04.2026." or "OʻRQ-637-сон 23.09.2020." — the registration number and
# date lex.uz puts in the page title. The letters in front matter: "OʻRQ" is a law and
# "PQ" a presidential decree, and a citation that drops them names the wrong document.
_NUMBER = re.compile(
    r"((?:[A-Za-z\u02bb\u02bc\u2018\u2019']{1,6}-)?\d+)"
    r"\s*[-\u2011]?\s*(?:\u0441\u043e\u043d|son)\s*([\d.]*)"
)
# A clause opens with its own number: "36. Kunduzgi ta'lim shaklida..."
_BAND = re.compile(r"^(\d+)\s*[.)]")


def _plain(fragment: str) -> str:
    return _SPACES.sub(" ", html.unescape(_TAGS.sub(" ", fragment))).strip()


def parse_document(page: str, doc_id: str) -> Document:
    """Pulls the clauses out of a lex.uz document page.

    lex.uz wraps every element of an act in `<div name="ID" id="ID">`, where ID is the
    anchor its own "link to this element" button uses. That is the natural unit: one
    numbered rule, already delimited by the publisher, with a permanent address.
    """
    heading = _TITLE.search(page)
    raw_title = _plain(heading.group(1)) if heading else ""
    number = date = ""
    if stamp := _NUMBER.search(raw_title):
        number, date = f"{stamp.group(1)}-son", stamp.group(2).strip(".")
    title = _NUMBER.sub("", raw_title).strip(" .")

    clauses: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for element, inner in _CLAUSE.findall(page):
        text = _plain(inner)
        if len(text) < MIN_CLAUSE_CHARS or text in seen:
            continue
        seen.add(text)
        numbered = _BAND.match(text)
        band = numbered.group(1) if numbered else ""
        clauses.append((element, band, text))
    return Document(doc_id=doc_id, number=number, title=title, adopted_at=date, clauses=clauses)


def save_document(connection: sqlite3.Connection, document: Document) -> int:
    """Stores a document, replacing whatever was there for the same lex.uz id.

    Re-fetching must not leave the old wording behind next to the new one: the assistant
    would then be able to quote a clause that has been amended.
    """
    with transaction(connection):
        existing = connection.execute(
            "SELECT id FROM lex_documents WHERE doc_id = ?", (document.doc_id,)
        ).fetchone()
        if existing:
            connection.execute("DELETE FROM lex_clauses WHERE document_id = ?", (existing["id"],))
            connection.execute("DELETE FROM lex_documents WHERE id = ?", (existing["id"],))
        cursor = connection.execute(
            "INSERT INTO lex_documents (doc_id, number, title, url, adopted_at, fetched_at,"
            " clauses) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                document.doc_id,
                document.number,
                document.title,
                DOC_URL.format(doc_id=document.doc_id),
                document.adopted_at,
                now(),
                len(document.clauses),
            ),
        )
        document_id = int(cursor.lastrowid or 0)
        connection.executemany(
            "INSERT INTO lex_clauses (document_id, element, band, position, text, search_text)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            [
                (document_id, element, band, position, text, fold(text))
                for position, (element, band, text) in enumerate(document.clauses)
            ],
        )
    return len(document.clauses)


def _rare(connection: sqlite3.Connection, words: list[str], clauses: int) -> set[str]:
    """Which of these stems actually say something about the subject.

    "Davom etadi" — "lasts" — appears in a third of the decree, so a question about how
    long a football match lasts shares two words with the clause on how long a bachelor's
    degree lasts, and the two-word floor lets it through. A word that is everywhere
    identifies nothing; a clause has to share a *rare* word with the question.
    """
    ceiling = max(1, int(clauses * RARE_FRACTION))
    rare = set()
    for word in words:
        try:
            seen = connection.execute(
                "SELECT count(*) FROM lex_fts WHERE lex_fts MATCH ?", (f"{word}*",)
            ).fetchone()[0]
        except sqlite3.Error:
            continue
        if 0 < seen <= ceiling:
            rare.add(word)
    return rare


def _whole_rule(connection: sqlite3.Connection, row: sqlite3.Row) -> str:
    """The clause plus the items that belong to it, when it is only the stem of a list.

    Uzbek decrees are written as "8. Part of the monthly rent is compensated:" followed
    by the cases, each a separate element. On its own that clause names a subject and
    answers nothing, and the model — correctly — refuses to answer from it. Handing over
    the stem without its branches is what makes a whole rule invisible.
    """
    text = str(row["text"])
    if not text.rstrip().endswith(":"):
        return text
    items = connection.execute(
        "SELECT text FROM lex_clauses WHERE document_id = ? AND position > ?"
        " ORDER BY position LIMIT ?",
        (row["document_id"], row["position"], CONTINUATION_CLAUSES),
    ).fetchall()
    for item in items:
        following = str(item["text"])
        text = f"{text} {following}"
        if not following.rstrip().endswith((":", ";")):
            break  # the list has ended with a full stop
    return text


def search(connection: sqlite3.Connection, question: str, limit: int = 3) -> list[Clause]:
    """The clauses most likely to answer the question, best first.

    Prefix matching on stems, because Uzbek glues its endings on: the decree says
    "taʼtil", the caller says "taʼtilda". Ranking is bm25, but ranking alone decides
    nothing — every search returns its best row, however bad the lot, and on a telephone
    that is a confident answer to a question nobody asked. A clause is offered only if it
    shares at least two meaningful words with the question, one of them rare.
    """
    words = terms(question)
    if not words:
        return []
    query = " OR ".join(f"{word}*" for word in words)
    try:
        clauses = int(connection.execute("SELECT count(*) FROM lex_clauses").fetchone()[0])
        rows = connection.execute(
            "SELECT c.element, c.band, c.text, c.document_id, c.position,"
            "       d.number, d.title, d.doc_id"
            "  FROM lex_fts f"
            "  JOIN lex_clauses c ON c.id = f.rowid"
            "  JOIN lex_documents d ON d.id = c.document_id"
            " WHERE lex_fts MATCH ? ORDER BY bm25(lex_fts) LIMIT ?",
            (query, limit * 8),
        ).fetchall()
    except sqlite3.Error as exc:  # a malformed MATCH must never end a call
        log.warning("law search failed for %r: %s", question[:60], exc)
        return []
    if not rows:
        return []
    rare = _rare(connection, words, clauses)
    if not rare:
        # An early exit and a diagnostic, nothing more: with no distinctive word at all,
        # the rule below would reject every row anyway. Deleting these three lines
        # changes only the speed and the log, which is why no test pins them.
        log.info("law search: %r has no distinctive word in the corpus", question[:60])
        return []

    needed = min(MIN_MATCHING_TERMS, len(words))
    found: list[Clause] = []
    for row in rows:
        folded = fold(row["text"])
        matched = {word for word in words if word in folded}
        if len(matched) < needed or not (matched & rare):
            continue
        found.append(
            Clause(
                number=row["number"],
                title=row["title"],
                doc_id=row["doc_id"],
                element=row["element"],
                band=row["band"],
                text=_whole_rule(connection, row),
            )
        )
        if len(found) == limit:
            break
    return found


class LexLibrary:
    """The bridge's view of the corpus: read-only, opened per question, never fatal.

    The connection is not held open between calls. A search is a handful of milliseconds
    on a corpus of a few thousand clauses, and a bridge that keeps no handle on the file
    cannot stop the CRM from writing to it.
    """

    def __init__(self, path: Path | str, limit: int = 3) -> None:
        self._path = Path(path)
        self._limit = limit
        self._checked_at = 0.0
        self._clauses = 0

    def clauses(self) -> int:
        """How many clauses are indexed; 0 means the feature is simply not in use."""
        if time.monotonic() - self._checked_at < RELOAD_AFTER_S:
            return self._clauses
        self._checked_at = time.monotonic()
        try:
            connection = connect(self._path)
            try:
                self._clauses = int(
                    connection.execute("SELECT count(*) FROM lex_clauses").fetchone()[0]
                )
            finally:
                connection.close()
        except Exception as exc:
            log.warning("could not read the law corpus: %s", exc)
            self._clauses = 0
        return self._clauses

    def find(self, question: str) -> list[Clause]:
        """Never raises: a failure here means no clauses, and the call goes on as before."""
        if not self.clauses():
            return []
        try:
            connection = connect(self._path)
            try:
                return search(connection, question, self._limit)
            finally:
                connection.close()
        except Exception as exc:
            log.warning("law search failed: %s", exc)
            return []
