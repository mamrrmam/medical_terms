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
| `medterms/cli.py` | `medterms init / load / lookup` |
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
| NS MSI Physician's Manual | Nova Scotia fee codes and MSU values | No open licence stated | **No** |
| CPT (AMA) | Outpatient procedure and billing codes | Paid AMA license | **No** |
| OHDSI Athena | All of the above, already normalized into OMOP tables | Per vocabulary | **No** |

## Billing (Nova Scotia first)

Canada has no national physician fee schedule; each province runs its own. Migration 002 adds
payer-neutral billing tables (`payer`, `unit_value`, `fee_schedule`) and a `NS_MSI` payer:

- **Diagnoses on NS MSI claims are ICD-9.** The `icd9cm` loader brings in ICD-9-CM and the CMS GEMs to
  ICD-10-CM, which connects NS claim diagnoses to everything else. NS may accept only a subset of these
  codes or use its own descriptions; that has not been checked against an MSI list.
- **Fee codes are MSI Health Service Codes** (e.g. `03.03`, with qualifiers and modifiers such as `RO=HDIN`),
  priced in MSU (Medical Service Units). They live in the MSI Physician's Manual, which seems to be published
  only as a PDF (https://msi.medavie.bluecross.ca/physicians-manual/). There is no loader for it yet, and
  no open licence is stated, so extracted fee data stays out of this repo.
- Hospital coding in Canada uses ICD-10-CA and CCI, which are licensed by CIHI and can't be redistributed.

This repo is CC0, so it can hold only public-domain data plus our own curated lay mappings. For licensed
vocabularies, commit only the loader scripts and have each user download the source files with their own license.
