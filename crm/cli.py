"""Command line helpers: create the first user, import the old call journals.

uv run python -m crm.cli user admin "Bosh administrator" admin --password secret
uv run python -m crm.cli import-history logs/
uv run python -m crm.cli import-knowledge content/faq.yaml
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

from crm import repo
from crm.config import settings
from crm.security import hash_password
from store.db import now, open_database, transaction
from store.write import import_jsonl


def create_user(args: argparse.Namespace) -> int:
    db = open_database(settings.db_path)
    if repo.user_by_login(db, args.login):
        print(f"user {args.login} already exists")
        return 1
    user_id = repo.create_user(
        db, args.login, args.name, args.role, hash_password(args.password), args.extension
    )
    print(f"created {args.role} #{user_id}: {args.login}")
    return 0


def import_history(args: argparse.Namespace) -> int:
    db = open_database(settings.db_path)
    imported = import_jsonl(Path(args.logs), db)
    calls = db.execute("SELECT count(*) FROM calls").fetchone()[0]
    contacts = db.execute("SELECT count(*) FROM contacts").fetchone()[0]
    print(f"imported {imported} journal lines: {calls} calls, {contacts} citizens")
    return 0


def import_knowledge(args: argparse.Namespace) -> int:
    """Moves content/faq.yaml into the database so the answers become editable."""
    db = open_database(settings.db_path)
    raw = yaml.safe_load(Path(args.file).read_text(encoding="utf-8")) or {}
    author = repo.users(db)
    author_id = int(author[0]["id"]) if author else 0  # 0 = imported before anyone signed in
    added = updated = 0
    with transaction(db):
        for faq_id, item in raw.items():
            answer = " ".join(str(item.get("answer", "")).split())
            keywords = ", ".join(str(word) for word in item.get("keywords", []))
            source = str(item.get("source", ""))
            existing = db.execute(
                "SELECT id, answer, keywords, source FROM knowledge WHERE faq_id = ?", (faq_id,)
            ).fetchone()
            if existing:
                if not args.update:
                    continue
                if (existing["answer"], existing["keywords"], existing["source"]) == (
                    answer,
                    keywords,
                    source,
                ):
                    continue
                repo.update_knowledge(
                    db,
                    int(existing["id"]),
                    author_id,
                    answer=answer,
                    keywords=keywords,
                    source=source,
                    status="published" if args.publish else "draft",
                )
                updated += 1
                continue
            db.execute(
                "INSERT INTO knowledge (faq_id, question, answer, keywords, source, status,"
                " version, author_id, created_at, updated_at)"
                " VALUES (?, '', ?, ?, ?, ?, 1, ?, ?, ?)",
                (
                    faq_id,
                    answer,
                    keywords,
                    source,
                    "published" if args.publish else "draft",
                    author_id,
                    now(),
                    now(),
                ),
            )
            added += 1
    state = "published" if args.publish else "draft"
    print(f"imported {added} new and updated {updated} answers ({state})")
    return 0


def backup(args: argparse.Namespace) -> int:
    """A consistent copy of the database, safe to run while people are working."""
    import sqlite3

    source = open_database(settings.db_path)
    target = sqlite3.connect(args.target)
    with target:
        source.backup(target)
    target.close()
    size = Path(args.target).stat().st_size
    print(f"backup written to {args.target} ({size / 1024:.0f} KB)")
    return 0


def sql(args: argparse.Namespace) -> int:
    """Runs one query against the database, with SQLite itself enforcing read-only.

    The system `sqlite3` on this server is from 2013 and understands neither full-text
    search nor indexes built on expressions; it answers "malformed database schema" for a
    database that is perfectly healthy. This command uses the SQLite that ships with the
    project's Python, so there is always a safe way to look inside.

    Read-only is the connection, not a check on the text. Refusing queries that merely
    start with the wrong word is no defence: SQLite happily runs `WITH x AS (…) DELETE …`,
    and a single `PRAGMA user_version = 0` makes the file unopenable by anything, which
    would silently stop the bridge recording calls. Opened `mode=ro`, the database itself
    refuses every one of those.
    """
    import sqlite3

    try:
        db = sqlite3.connect(f"file:{settings.db_path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        print(f"could not open {settings.db_path}: {exc}")
        return 1
    db.row_factory = sqlite3.Row
    try:
        rows = db.execute(args.query).fetchall()
    except sqlite3.Error as exc:
        print(f"{exc} (this connection is read-only by design)")
        return 1
    finally:
        db.close()

    if not rows:
        print("(no rows)")
        return 0
    columns = rows[0].keys()
    print(" | ".join(columns))
    for row in rows:
        print(" | ".join("" if row[c] is None else str(row[c]) for c in columns))
    print(f"({len(rows)} rows)")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="crm")
    commands = parser.add_subparsers(dest="command", required=True)

    user = commands.add_parser("user", help="create a user")
    user.add_argument("login")
    user.add_argument("name")
    user.add_argument("role", choices=repo.ROLES)
    user.add_argument("--password", required=True)
    user.add_argument("--extension", default="")
    user.set_defaults(run=create_user)

    history = commands.add_parser("import-history", help="load logs/*.jsonl into the database")
    history.add_argument("logs")
    history.set_defaults(run=import_history)

    knowledge = commands.add_parser("import-knowledge", help="load content/faq.yaml")
    knowledge.add_argument("file")
    knowledge.add_argument("--publish", action="store_true")
    knowledge.add_argument(
        "--update", action="store_true", help="also refresh answers that already exist"
    )
    knowledge.set_defaults(run=import_knowledge)

    copy = commands.add_parser("backup", help="copy the database to a file")
    copy.add_argument("target")
    copy.set_defaults(run=backup)

    query = commands.add_parser(
        "sql", help="run one read-only query (the system sqlite3 is too old)"
    )
    query.add_argument("query")
    query.set_defaults(run=sql)

    args = parser.parse_args(argv)
    return int(args.run(args))


if __name__ == "__main__":
    sys.exit(main())
