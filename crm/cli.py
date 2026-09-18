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
    author_id = int(author[0]["id"]) if author else None
    added = 0
    with transaction(db):
        for faq_id, item in raw.items():
            if db.execute("SELECT 1 FROM knowledge WHERE faq_id = ?", (faq_id,)).fetchone():
                continue
            db.execute(
                "INSERT INTO knowledge (faq_id, question, answer, keywords, source, status,"
                " version, author_id, created_at, updated_at)"
                " VALUES (?, '', ?, ?, ?, ?, 1, ?, ?, ?)",
                (
                    faq_id,
                    " ".join(str(item.get("answer", "")).split()),
                    ", ".join(str(word) for word in item.get("keywords", [])),
                    str(item.get("source", "")),
                    "published" if args.publish else "draft",
                    author_id,
                    now(),
                    now(),
                ),
            )
            added += 1
    print(f"imported {added} answers ({'published' if args.publish else 'draft'})")
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
    knowledge.set_defaults(run=import_knowledge)

    copy = commands.add_parser("backup", help="copy the database to a file")
    copy.add_argument("target")
    copy.set_defaults(run=backup)

    args = parser.parse_args(argv)
    return int(args.run(args))


if __name__ == "__main__":
    sys.exit(main())
