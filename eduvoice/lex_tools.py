"""Downloads the regulations from lex.uz into the local index, and lets you query it.

    uv run python -m eduvoice.lex_tools fetch          # everything in content/lex.yaml
    uv run python -m eduvoice.lex_tools fetch -8117474 # one document
    uv run python -m eduvoice.lex_tools list           # what is indexed
    uv run python -m eduvoice.lex_tools ask "Akademik taʼtilda stipendiya toʻlanadimi?"

This is the only place that talks to lex.uz, and it is never run during a call: the
bridge reads the database. Fetching again replaces a document wholesale, so an amended
clause cannot survive next to its replacement.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import httpx
import yaml

from eduvoice.config import settings
from store.db import open_database
from store.lex import DOC_URL, parse_document, save_document, search

# lex.uz serves the whole act as one page; without a browser-like agent it answers 403.
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; EduVoice/1.0; ministry call centre)"}
TIMEOUT_S = 40.0


def _wanted(args: argparse.Namespace) -> list[tuple[str, str]]:
    """The documents to fetch: the ones named on the command line, or the whole list."""
    if args.documents:
        return [(doc_id, "") for doc_id in args.documents]
    path = Path(args.file or settings.content_dir / "lex.yaml")
    if not path.exists():
        print(f"no document list at {path}")
        return []
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return [(str(item["id"]), str(item.get("note", ""))) for item in raw.get("documents", [])]


def fetch(args: argparse.Namespace) -> int:
    documents = _wanted(args)
    if not documents:
        return 1
    db = open_database(settings.db_path)
    total = failed = 0
    with httpx.Client(headers=HEADERS, timeout=TIMEOUT_S, follow_redirects=True) as client:
        for doc_id, note in documents:
            url = DOC_URL.format(doc_id=doc_id)
            try:
                response = client.get(url)
                response.raise_for_status()
            except Exception as exc:
                failed += 1
                print(f"  XATO {doc_id}: {exc}")
                continue
            document = parse_document(response.text, doc_id)
            if not document.clauses:
                # A document with no clauses means the page changed shape, not that the
                # decree is empty. Saying so is the whole point: silently indexing nothing
                # would leave the assistant quietly unable to answer anything.
                failed += 1
                print(f"  XATO {doc_id}: no clauses found — has the page layout changed?")
                continue
            saved = save_document(db, document)
            total += saved
            label = document.number or note or doc_id
            print(f"  {label:>14}  {saved:4d} band  {document.title[:56]}")
    print(f"\n  hujjatlar: {len(documents) - failed}, bandlar: {total}, xatolar: {failed}")
    print(f"  baza: {settings.db_path}")
    return 1 if failed else 0


def show(args: argparse.Namespace) -> int:
    db = open_database(settings.db_path)
    rows = db.execute(
        "SELECT number, title, clauses, adopted_at, fetched_at, url"
        " FROM lex_documents ORDER BY number"
    ).fetchall()
    if not rows:
        print("nothing indexed yet — run `make lex-fetch`")
        return 1
    for row in rows:
        print(f"  {row['number']:>14}  {row['clauses']:4d} band  {row['title'][:60]}")
        print(f"                  {row['url']}  (fetched {row['fetched_at']})")
    total = db.execute("SELECT count(*) FROM lex_clauses").fetchone()[0]
    print(f"\n  {len(rows)} hujjat, {total} band")
    return 0


def ask(args: argparse.Namespace) -> int:
    """What the assistant would be given for this question — the retrieval, not the answer.

    The question may come on standard input instead of the command line. Uzbek is full of
    apostrophes — "toʻlov", "taʼtil" — and an apostrophe inside a quoted argument sent
    over ssh closes the quoting and mangles the command, so `make lex-ask` pipes it in.
    """
    question = args.question if args.question is not None else sys.stdin.read()
    question = question.strip()
    if not question:
        print("no question given")
        return 1
    db = open_database(settings.db_path)
    found = search(db, question, limit=args.limit)
    if not found:
        print("  hech narsa topilmadi -> operator")
        return 1
    for clause in found:
        print(f"\n  {clause.citation}  {clause.link}")
        print(f"  {clause.text[:400]}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="eduvoice.lex_tools")
    commands = parser.add_subparsers(dest="command", required=True)

    download = commands.add_parser("fetch", help="download the regulations from lex.uz")
    download.add_argument("documents", nargs="*", help="lex.uz ids; default: content/lex.yaml")
    download.add_argument("--file", help="a different document list")
    download.set_defaults(run=fetch)

    listing = commands.add_parser("list", help="what is indexed")
    listing.set_defaults(run=show)

    query = commands.add_parser("ask", help="which clauses a question would find")
    query.add_argument("question", nargs="?", help="omit it to read the question from stdin")
    query.add_argument("--limit", type=int, default=3)
    query.set_defaults(run=ask)

    args = parser.parse_args(argv)
    return int(args.run(args))


if __name__ == "__main__":
    sys.exit(main())
