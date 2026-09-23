"""Command line entry point.

  medterms init   --db sqlite:///terms.db
  medterms load icd10cm --db sqlite:///terms.db data/icd10cm/
  medterms load umls    --db sqlite:///terms.db data/umls/2026AA/
  medterms load icd9cm  --db sqlite:///terms.db data/icd9cm/
  medterms lookup --db sqlite:///terms.db "heart attack" --to ICD10CM
"""

import argparse
import sys
from pathlib import Path

from medterms.db import Database
from medterms.loaders import icd9cm, icd10cm, umls
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
        label = f"ICD-10-CM FY{release.year}"
    elif args.vocabulary == "umls":
        if len(args.paths) != 1:
            sys.exit("load umls takes one release directory")
        stats = umls.load(db, Path(args.paths[0]), sabs=args.sabs.split(",") if args.sabs else None)
        label = "UMLS"
    else:
        stats = icd9cm.load(db, [Path(p) for p in args.paths])
        label = "ICD-9-CM"
    print(f"{label}: " + ", ".join(f"{k}={v}" for k, v in stats.items()))


# Hops followed by `lookup --to`: lay/clinical term on a UMLS concept -> its codes,
# curated lay mappings, and cross-vocabulary maps such as the ICD-9-CM GEMs.
MAPPING_RELATIONSHIPS = ("umls_cui_of", "may_be", "maps_to", "maps_to_approx")


def cmd_lookup(args):
    db = Database(args.db)
    norm = normalize_term(args.term)
    match = "(s.term_normalized = ? OR s.term_normalized LIKE ?)"
    params = [norm, norm + " %"]
    if args.to:
        hops = ", ".join("?" * len(MAPPING_RELATIONSHIPS))
        sql = (
            "SELECT DISTINCT t.vocabulary_id, t.code, t.name, s.term, s.term_type, t.is_billable, "
            "CASE WHEN s.term_normalized = ? THEN 0 ELSE 1 END AS exact "
            "FROM concept_synonym s JOIN concept c ON c.concept_id = s.concept_id "
            "JOIN concept_relationship r ON r.concept_id_1 = c.concept_id "
            f"AND r.relationship_id IN ({hops}) "
            "JOIN concept t ON t.concept_id = r.concept_id_2 AND t.vocabulary_id = ? "
            f"WHERE {match} "
            "UNION "
            "SELECT c.vocabulary_id, c.code, c.name, s.term, s.term_type, c.is_billable, "
            "CASE WHEN s.term_normalized = ? THEN 0 ELSE 1 END "
            "FROM concept_synonym s JOIN concept c ON c.concept_id = s.concept_id AND c.vocabulary_id = ? "
            f"WHERE {match} "
            "ORDER BY 7, 6 DESC, 2 LIMIT ?"
        )
        params = [norm, *MAPPING_RELATIONSHIPS, args.to, *params, norm, args.to, *params, args.limit]
    else:
        sql = (
            "SELECT c.vocabulary_id, c.code, c.name, s.term, s.term_type, c.is_billable "
            "FROM concept_synonym s JOIN concept c ON c.concept_id = s.concept_id "
            f"WHERE {match} "
            "ORDER BY CASE WHEN s.term_normalized = ? THEN 0 ELSE 1 END, c.is_billable DESC, c.code "
            "LIMIT ?"
        )
        params = [*params, norm, args.limit]
    rows = db.query(sql, params)
    for vocab, code, name, term, term_type, billable, *_ in rows:
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
    p.add_argument("vocabulary", choices=["icd10cm", "umls", "icd9cm"])
    p.add_argument("paths", nargs="+", help="release zip files or directories")
    p.add_argument("--year", type=int, help="icd10cm: fiscal year, if it can't be read from the file names")
    p.add_argument("--sabs", help=f"umls: comma-separated source vocabularies (default {','.join(umls.DEFAULT_SABS)})")
    p.set_defaults(func=cmd_load)

    p = sub.add_parser("lookup", help="find concepts by term (exact, then prefix match)")
    p.add_argument("term")
    p.add_argument("--to", metavar="VOCABULARY", help="follow mappings to this vocabulary, e.g. ICD10CM")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_lookup)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
