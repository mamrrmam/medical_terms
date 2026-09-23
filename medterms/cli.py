"""Command line entry point.

  medterms init   --db sqlite:///terms.db
  medterms load icd10cm --db sqlite:///terms.db data/icd10cm/
  medterms lookup --db sqlite:///terms.db "generalized anxiety"
"""

import argparse
import sys
from pathlib import Path

from medterms.db import Database
from medterms.loaders import icd10cm
from medterms.normalize import normalize_term

DEFAULT_DB = "sqlite:///terms.db"


def cmd_init(args):
    db = Database(args.db)
    db.init_schema()
    print(f"schema ready in {args.db}")


def cmd_load(args):
    db = Database(args.db)
    if args.vocabulary == "icd10cm":
        release = icd10cm.read_release([Path(p) for p in args.paths], year=args.year)
        stats = icd10cm.load(db, release)
        print(f"ICD-10-CM FY{release.year}: " + ", ".join(f"{k}={v}" for k, v in stats.items()))


def cmd_lookup(args):
    db = Database(args.db)
    norm = normalize_term(args.term)
    rows = db.query(
        "SELECT c.vocabulary_id, c.code, c.name, s.term, s.term_type, c.is_billable "
        "FROM concept_synonym s JOIN concept c ON c.concept_id = s.concept_id "
        "WHERE s.term_normalized = ? OR s.term_normalized LIKE ? "
        "ORDER BY CASE WHEN s.term_normalized = ? THEN 0 ELSE 1 END, c.is_billable DESC, c.code "
        "LIMIT ?",
        (norm, norm + " %", norm, args.limit),
    )
    for vocab, code, name, term, term_type, billable in rows:
        flag = "*" if billable else " "
        print(f"{vocab:8} {code:9}{flag} {name}   <- {term} [{term_type}]")
    if not rows:
        print("no matches", file=sys.stderr)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="medterms")
    parser.add_argument("--db", default=DEFAULT_DB, help=f"database url (default {DEFAULT_DB}; postgresql://... also works)")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="create tables").set_defaults(func=cmd_init)

    p = sub.add_parser("load", help="load a vocabulary release")
    p.add_argument("vocabulary", choices=["icd10cm"])
    p.add_argument("paths", nargs="+", help="release zip files or directories")
    p.add_argument("--year", type=int, help="fiscal year, if it can't be read from the file names")
    p.set_defaults(func=cmd_load)

    p = sub.add_parser("lookup", help="find concepts by term (exact, then prefix match)")
    p.add_argument("term")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_lookup)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
