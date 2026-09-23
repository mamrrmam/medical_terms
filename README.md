# medical_terms

A terminology database for mapping:

1. **colloquial → professional** terms (`"nerves"` → *Anxiety disorder, unspecified*, *Generalized anxiety disorder*, …)
2. **professional terms → ICD-10-CM** (diagnoses), ICD-10-PCS / CPT / HCPCS (procedures), RxNorm (drugs)
3. later, **codes → billing** (fee schedules, DRGs, NCCI edits)

## Quick start

```sh
pip install -e ".[dev]"            # add ",postgres" for PostgreSQL, ",pdf" for the PDF extractor

# Download the ICD-10-CM release zips from https://www.cdc.gov/nchs/icd/icd-10-cm/files.html
# ("Code Descriptions in Tabular Order" and "Tabular and Index" files) into data/icd10cm/, then:
medterms --db sqlite:///terms.db load icd10cm data/icd10cm/

# After your UMLS license is approved: download the Metathesaurus Full Subset from
# https://www.nlm.nih.gov/research/umls/licensedcontent/umlsknowledgesources.html
medterms --db sqlite:///terms.db load umls data/umls/2026AA/

# ICD-9-CM v32 descriptions and the 2018 diagnosis GEMs from CMS (for Nova Scotia claim diagnoses)
medterms --db sqlite:///terms.db load icd9cm data/icd9cm/

medterms --db sqlite:///terms.db lookup "heart attack" --to ICD10CM
```

Load ICD-10-CM first: the UMLS and ICD-9-CM loaders link to existing ICD-10-CM codes but never create them.

`--db postgresql://user:pass@host/dbname` works the same way. `data/` and `*.db` are git-ignored.

## Layout

| Path | What |
|---|---|
| `medterms/migrations/` | Numbered SQL migrations, applied in order by `Database.init_schema()` |
| `medterms/loaders/icd10cm.py` | ICD-10-CM (CDC): codes, chapters and blocks, hierarchy, inclusion terms, Alphabetic Index entries |
| `medterms/loaders/umls.py` | UMLS Metathesaurus: one concept per CUI with all English strings (CHV and MedlinePlus strings as lay terms), SNOMED CT / RxNorm / MeSH / LOINC codes, and links from CUIs to ICD-10-CM |
| `medterms/loaders/icd9cm.py` | ICD-9-CM diagnoses and the CMS ICD-9 → ICD-10-CM GEMs |
| `medterms/loaders/fees.py` | Payer fee schedules, unit values and payer code lists from a normalized CSV |
| `medterms/extract/` | PDF fee schedule and code list extractor, driven by a per-payer profile; writes the normalized CSV plus a report of lines it couldn't place |
| `medterms/profiles/` | Extractor profiles: `ns_msi_fees`, `ns_msi_modifiers`, `ns_msi_explanatory` |
| `medterms/cli.py` | `medterms init / load / extract / lookup` |
| `schema/example_seed.sql`, `schema/example_lookup.sql` | Hand-written rows showing how lay terms map to ranked ICD-10-CM candidates (load into an empty database only, because they use fixed ids) |
| `tests/` | pytest suite, run against sample files written in each source's format; set `MEDTERMS_TEST_PG=postgresql://.../postgres` to run it on PostgreSQL too |

The schema uses SQL that runs unchanged on SQLite, PostgreSQL and MySQL 8, and it follows the
[OMOP vocabulary model](https://ohdsi.github.io/CommonDataModel/) in simplified form. The loaders use
`INSERT ... ON CONFLICT`, which SQLite and PostgreSQL support but MySQL does not.

### How a lay term reaches a code (example from the test data)

```
"heart attack"  --synonym-->  UMLS C0027051  --umls_cui_of-->  ICD10CM I21.9  --approx_mapped_from-->  ICD9CM 410.90
  (CHV string)                                                     (US diagnosis)                         (NS MSI claim diagnosis)
```

### What the ICD-10-CM loader produces

| Table | Rows |
|---|---|
| `concept` | Every code in the order file, plus chapters (`CH05`) and blocks (`F40-F48`) from the tabular file |
| `concept_synonym` | Official long name (`preferred`), tabular inclusion terms (`clinical`, e.g. "Anxiety state" for F41.1), index entries (`index`, e.g. "Anxiety, generalized") |
| `concept_relationship` | `is_a` / `subsumes`: code, then category, then block, then chapter |

Loading a newer fiscal year keeps existing `concept_id`s, updates names, sets `valid_start` on new codes,
and sets `valid_end` on codes that were dropped.

## Data sources

| Source | Covers | License | Put it in git? |
|---|---|---|---|
| ICD-10-CM (CDC/NCHS) | Diagnoses, with an alphabetic index of synonyms | Public domain | Yes |
| ICD-10-PCS (CMS) | Inpatient procedures | Public domain | Yes |
| HCPCS Level II (CMS) | Supplies, drugs and services billed to Medicare | Public domain | Yes |
| MeSH (NLM) | Topics, with entry-term synonyms | Free | Yes |
| RxNorm (NLM) | Drug ingredients, brands, doses | Prescribable subset is free; the full release needs a UMLS license | Prescribable subset only |
| FDA NDC Directory | Package-level drug codes | Public domain | Yes |
| LOINC | Lab tests and observations | Free with registration | Check the terms |
| SNOMED CT (US edition) + NLM SNOMED→ICD-10-CM map | Clinical concepts with many synonyms | UMLS license (free for US use) | **No** |
| UMLS Metathesaurus, including the Consumer Health Vocabulary (CHV) | Links all of the above; CHV links lay terms to concepts | UMLS license | **No** |
| ICD-9-CM + GEMs (CMS) | Legacy US diagnoses; used on NS MSI claims | Public domain | Yes |
| Provincial fee schedules and diagnostic lists | Physician billing codes and fees | Crown copyright or attribution licences | **No** |
| CPT (AMA) | Outpatient procedure and billing codes | Paid AMA license | **No** |
| OHDSI Athena | All of the above, already normalized into OMOP tables | Per vocabulary | **No** |

## Billing (Canadian provinces)

Canada has no national physician fee schedule; each province runs its own. The billing tables
(`payer`, `unit_value`, `fee_schedule`) are payer-neutral, and migration 003 registers every
province plus Yukon, each with its own fee-code vocabulary. Northwest Territories and Nunavut are left
out because their physicians are mostly salaried.

Fee schedules load from one normalized CSV (see `medterms/loaders/fees.py`):

```sh
medterms load fees --payer ON_OHIP on_fees.csv          # code, description, units/amount, modifier, effective_start, ...
medterms load codes --vocabulary ON_OHIP_DX on_dx.csv --maps-to-vocabulary ICD9CM   # payer diagnostic lists
```

### Nova Scotia from the Physician's Manual PDF

```sh
pip install -e ".[pdf]"
medterms extract ns_msi_fees        Physicians-Manual.pdf -o ns_fees.csv          # Section 8, ~6,400 rows
medterms extract ns_msi_modifiers   Physicians-Manual.pdf -o ns_modifiers.csv     # Appendix H (AG=OV65 ...)
medterms extract ns_msi_explanatory Physicians-Manual.pdf -o ns_explanatory.csv   # Appendix I (AD001 ...)
medterms load fees  --payer NS_MSI ns_fees.csv
medterms load codes --vocabulary NS_MSI_MOD  ns_modifiers.csv
medterms load codes --vocabulary NS_MSI_EXPL ns_explanatory.csv
medterms load units --payer NS_MSI ns_units.csv    # unit_name,effective_start,effective_end,amount_per_unit (MSU and AU, Section 4)
```

Each extract also writes `<output>.report.md`, which lists records printed without a fee, keys that
appear twice with different fees, and lines that matched nothing. Review it before loading. The
profiles are tuned on the 2026 manual (last updated July 2026); for another edition, check `pages`
first. One code has many rows: 03.03 is priced separately for each schedule section (specialty) and
modifier set, so `fee_schedule` is keyed by code, `section` and `modifier`. Fees printed with
qualifiers (`62+MU`, `4+T`, `Time Only`) keep the number in `units` / `anaesthesia_units` and the
printed text in `fee_note`.

What each province publishes, from web research. Only Nova Scotia's files have been seen so far; each other
province needs its own profile (or converter to the CSV above) once its files have been checked.

| Payer | Fee codes | Published as | Claim diagnoses |
|---|---|---|---|
| `BC_MSP` | 5-digit fee items (`00100`) | PDF | MSP ICD-9 list with BC-specific codes (PDF by chapter) |
| `AB_AHCIP` | Health service codes (`03.03A`) + modifiers (`CMGP01`) | PDF on open.alberta.ca (attribution licence) | Alberta ICD-9-based list (PDF) |
| `SK_MSB` | Number + letter (`5B`) | PDF; vendor rate file not public | ICD-9 |
| `MB_HEALTH` | Prefix + tariff (`8540`) | PDF; vendor tariff rate file (layout in ECPIM) | ICD-9-CM, 4+ digits, no decimal |
| `ON_OHIP` | `A007` + suffix A/B/C | PDF; Fee Schedule Master text file for vendors | OHIP 3-digit codes (ICD-8 based, PDF) |
| `QC_RAMQ` | 5-digit codes | PDF manuals (French) | RAMQ CIM-9 and CIM-10 lists (XLSX) |
| `NB_MEDICARE` | Service codes | PDF | Unclear (ICD-9 or ICD-10) |
| `NS_MSI` | Health service codes (`03.03`) + modifiers (`RO=HDIN`), in MSU (AU for anaesthesia) | PDF; extractor profiles included | ICD-9 (`medterms load icd9cm`) |
| `PE_HPEI` | Tariff of Fees (codes changed April 2025) | PDF | ICD-9 style |
| `NL_MCP` | Fee codes (`522`) | PDF | ICD-9 |
| `YT_YHCIP` | Payment schedule | PDF / web lookup | ICD-9 |

Hospital coding in Canada uses ICD-10-CA and CCI, which are licensed by CIHI and can't be redistributed.
No provincial fee schedule carries a licence compatible with CC0, so fee data stays local like UMLS data.

This repo is CC0, so it can hold only public-domain data plus our own curated lay mappings. For licensed
vocabularies, commit only the loader scripts and have each user download the source files with their own license.
