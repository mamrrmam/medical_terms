# medical_terms

A terminology database for mapping:

1. **colloquial → professional** terms (`"nerves"` → *Anxiety disorder, unspecified*, *Generalized anxiety disorder*, …)
2. **professional terms → ICD-10-CM** (diagnoses), ICD-10-PCS / CPT / HCPCS (procedures), RxNorm (drugs)
3. later, **codes → billing** (fee schedules, DRGs, NCCI edits)

## Layout

| Path | What |
|---|---|
| `schema/schema.sql` | Tables: `vocabulary`, `concept`, `concept_synonym`, `relationship`, `concept_relationship` |
| `schema/example_seed.sql` | A few hand-written rows showing how the tables connect |
| `schema/example_lookup.sql` | Example query: lay term → ranked ICD-10-CM candidates |

The schema uses SQL that runs unchanged on SQLite, PostgreSQL and MySQL 8, and it follows the
[OMOP vocabulary model](https://ohdsi.github.io/CommonDataModel/) in simplified form.

```sh
sqlite3 terms.db < schema/schema.sql
sqlite3 terms.db < schema/example_seed.sql
sqlite3 terms.db < schema/example_lookup.sql
```

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
