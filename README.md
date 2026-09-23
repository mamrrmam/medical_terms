# medical_terms

A terminology database for mapping:

1. **colloquial → professional** terms (`"nerves"` → *Anxiety disorder, unspecified*, *Generalized anxiety disorder*, …)
2. **professional terms → ICD-10-CM** (diagnoses), ICD-10-PCS / CPT / HCPCS (procedures), RxNorm (drugs)
3. later, **codes → billing** (fee schedules, DRGs, NCCI edits)

## Quick start

```sh
pip install -e ".[dev]"            # add ",postgres" for PostgreSQL support

# Download the ICD-10-CM release zips from https://www.cdc.gov/nchs/icd/icd-10-cm/files.html
# ("Code Descriptions in Tabular Order" and "Tabular and Index" files) into data/icd10cm/, then:
medterms --db sqlite:///terms.db load icd10cm data/icd10cm/
medterms --db sqlite:///terms.db lookup "anxiety"
```

`--db postgresql://user:pass@host/dbname` works the same way. `data/` and `*.db` are git-ignored.

## Layout

| Path | What |
|---|---|
| `medterms/schema.sql` | Tables: `vocabulary`, `concept`, `concept_synonym`, `relationship`, `concept_relationship` |
| `medterms/loaders/icd10cm.py` | ICD-10-CM loader: codes, chapters and blocks, hierarchy, inclusion terms, Alphabetic Index entries |
| `medterms/cli.py` | `medterms init / load / lookup` |
| `schema/example_seed.sql`, `schema/example_lookup.sql` | Hand-written rows showing how lay terms map to ranked ICD-10-CM candidates (load into an empty database only, because they use fixed ids) |
| `tests/` | pytest suite; set `MEDTERMS_TEST_PG=postgresql://.../postgres` to run it on PostgreSQL too |

The schema uses SQL that runs unchanged on SQLite, PostgreSQL and MySQL 8, and it follows the
[OMOP vocabulary model](https://ohdsi.github.io/CommonDataModel/) in simplified form. The loaders use
`INSERT ... ON CONFLICT`, which SQLite and PostgreSQL support but MySQL does not.

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
| CPT (AMA) | Outpatient procedure and billing codes | Paid AMA license | **No** |
| OHDSI Athena | All of the above, already normalized into OMOP tables | Per vocabulary | **No** |

This repo is CC0, so it can hold only public-domain data plus our own curated lay mappings. For licensed
vocabularies, commit only the loader scripts and have each user download the source files with their own license.
