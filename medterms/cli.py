"""Command line entry point.

  medterms init   --db sqlite:///terms.db
  medterms load icd10cm --db sqlite:///terms.db data/icd10cm/
  medterms load umls    --db sqlite:///terms.db data/umls/2026AA/
  medterms load icd9cm  --db sqlite:///terms.db data/icd9cm/
  medterms load fees  --db sqlite:///terms.db --payer NS_MSI ns_fees.csv
  medterms load units --db sqlite:///terms.db --payer NS_MSI ns_units.csv
  medterms load codes --db sqlite:///terms.db --vocabulary ON_OHIP_DX dx.csv --maps-to-vocabulary ICD9CM
  medterms lookup --db sqlite:///terms.db "heart attack" --to ICD10CM
  medterms extract ns_msi_fees Physicians-Manual.pdf -o ns_fees.csv --report ns_fees.md
"""

import argparse
import sys
from pathlib import Path

from medterms.db import Database
from medterms.loaders import fees, icd9cm, icd10cm, umls
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
    elif args.vocabulary == "icd9cm":
        stats = icd9cm.load(db, [Path(p) for p in args.paths])
        label = "ICD-9-CM"
    elif args.vocabulary == "fees":
        if not args.payer or len(args.paths) != 1:
            sys.exit("load fees takes --payer and one CSV file")
        stats = fees.load_fees(db, args.payer, Path(args.paths[0]), effective=args.effective)
        label = f"{args.payer} fees"
    elif args.vocabulary == "units":
        if not args.payer or len(args.paths) != 1:
            sys.exit("load units takes --payer and one CSV file")
        stats = fees.load_units(db, args.payer, Path(args.paths[0]))
        label = f"{args.payer} unit values"
    else:
        if not args.target_vocabulary or len(args.paths) != 1:
            sys.exit("load codes takes --vocabulary and one CSV file")
        stats = fees.load_codes(db, args.target_vocabulary, Path(args.paths[0]), args.maps_to_vocabulary)
        label = args.target_vocabulary
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


def cmd_extract(args):
    from medterms.extract import fees as extract_fees
    from medterms.extract.pdftext import read_lines

    profile = extract_fees.Profile.load(args.profile)
    pdf = Path(args.pdf)
    result = extract_fees.extract(read_lines(pdf, args.pages or profile.pages), profile)
    out = Path(args.output or pdf.with_suffix(".csv").name)
    extract_fees.write_csv(result, out, profile)
    report = Path(args.report or out.with_suffix(".report.md"))
    extract_fees.write_report(result, profile, pdf.name, report)
    print(f"{out}: {len(result.rows)} rows, {len({r['code'] for r in result.rows})} codes; "
          f"{len(result.no_fee)} records without a fee, {len(result.leftover)} leftover lines, "
          f"{len(result.duplicates)} conflicting duplicates (see {report})")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="medterms")
    parser.add_argument("--db", default=DEFAULT_DB, help=f"database url (default {DEFAULT_DB}; postgresql://... also works)")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="create tables").set_defaults(func=cmd_init)

    p = sub.add_parser("load", help="load a vocabulary release")
    p.add_argument("vocabulary", choices=["icd10cm", "umls", "icd9cm", "fees", "units", "codes"])
    p.add_argument("paths", nargs="+", help="release zip files, directories or CSV files")
    p.add_argument("--year", type=int, help="icd10cm: fiscal year, if it can't be read from the file names")
    p.add_argument("--sabs", help=f"umls: comma-separated source vocabularies (default {','.join(umls.DEFAULT_SABS)})")
    p.add_argument("--payer", help="fees, units: payer_id, e.g. NS_MSI")
    p.add_argument("--effective", help="fees: effective_start (YYYY-MM-DD) for rows that don't have one")
    p.add_argument("--vocabulary", dest="target_vocabulary", help="codes: vocabulary_id to load the list into")
    p.add_argument("--maps-to-vocabulary", help="codes: vocabulary of the optional maps_to column, e.g. ICD9CM")
    p.set_defaults(func=cmd_load)

    p = sub.add_parser("lookup", help="find concepts by term (exact, then prefix match)")
    p.add_argument("term")
    p.add_argument("--to", metavar="VOCABULARY", help="follow mappings to this vocabulary, e.g. ICD10CM")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_lookup)

    p = sub.add_parser("extract", help="extract a fee schedule or code list from a PDF into CSV (needs medterms[pdf])")
    p.add_argument("profile", help="profile TOML path, or a name from medterms/profiles/ such as ns_msi_fees")
    p.add_argument("pdf")
    p.add_argument("-o", "--output", help="CSV to write (default: <pdf name>.csv in the current directory)")
    p.add_argument("--report", help="Markdown report to write (default: <output>.report.md)")
    p.add_argument("--pages", help="page ranges to read instead of the profile's, e.g. 216-230")
    p.set_defaults(func=cmd_extract)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
