# medical_terms

A terminology database for mapping:

1. **colloquial → professional** terms (`"nerves"` → *Anxiety disorder, unspecified*, *Generalized anxiety disorder*, …)
2. **professional terms → ICD-10-CM** (diagnoses), ICD-10-PCS / CPT / HCPCS (procedures), RxNorm (drugs)
3. later, **codes → billing** (fee schedules, DRGs, NCCI edits)

## Quick start

```sh
pip install -e ".[dev]"            # add ",postgres" for PostgreSQL, ",pdf" for the PDF extractor

# ICD-10-CM from CDC (https://ftp.cdc.gov/pub/Health_Statistics/NCHS/Publications/ICD10CM/2027/):
# icd10cm-code-descriptions-2027.zip and icd10cm-table-and-index-2027.zip into data/icd10cm/
medterms --db sqlite:///terms.db load icd10cm data/icd10cm/

# ICD-9-CM v32 descriptions (diagnoses and procedures) and the 2018 GEMs (both directions) from CMS
# (icd-9-cm-v32-master-descriptions.zip, 2018-icd-10-cm-general-equivalence-mappings.zip) into data/icd9cm/
medterms --db sqlite:///terms.db load icd9cm data/icd9cm/

# UMLS (license required). The release zip is read in place; nothing needs unpacking.
medterms --db sqlite:///terms.db load umls data/umls/umls-2026AA-metathesaurus-level0.zip

# Optional: Nova Scotia fee codes (see "Nova Scotia from the Physician's Manual PDF" below)
medterms --db sqlite:///terms.db load fees --payer NS_MSI ns_fees.csv

# Link concepts to codes by name, then look terms up and measure coverage
medterms --db sqlite:///terms.db link
medterms --db sqlite:///terms.db lookup "heart attack" --to ICD10CM
medterms --db sqlite:///terms.db evaluate eval/lay_terms.csv

# Find concepts in running text
medterms --db sqlite:///terms.db annotate "Hx of MI. Now has chest pain and is worried about another heart attack."
```

Load ICD-10-CM and ICD-9-CM before UMLS: the UMLS loader links to existing ICD codes but never creates them.
Run `link` last, and again after loading anything new.

If the project sits in an iCloud-synced folder (such as `~/Documents` with Desktop & Documents sync on), iCloud
marks files in `.venv` as hidden and Python stops finding the installed package. Keep the environment in a folder
iCloud skips, e.g. `uv venv .venv.nosync && ln -s .venv.nosync .venv`.

`--db postgresql://user:pass@host/dbname` works the same way. `data/` and `*.db` are git-ignored.

## Layout

| Path | What |
|---|---|
| `medterms/migrations/` | Numbered SQL migrations, applied in order by `Database.init_schema()` |
| `medterms/loaders/icd10cm.py` | ICD-10-CM (CDC): codes, chapters and blocks, hierarchy, inclusion terms, Alphabetic Index entries |
| `medterms/loaders/umls.py` | UMLS Metathesaurus, read from the release zip: one concept per CUI with its English strings (CHV and MedlinePlus as lay terms), SNOMED CT / MeSH codes, and links from CUIs to ICD-10-CM and ICD-9-CM |
| `medterms/loaders/icd9cm.py` | ICD-9-CM diagnoses and procedures, and the CMS GEMs in both directions |
| `medterms/linker.py` | `medterms link`: approximate links from UMLS concepts to ICD-10-CM, ICD-9-CM and NS fee codes by name, and across the GEMs |
| `medterms/lookup.py` | Term lookup ranked by link confidence, and `medterms evaluate` |
| `medterms/annotate.py` | `medterms annotate`: find clinical concepts in running text (transcripts, dictation) and link them to codes |
| `medterms/lexicon.py`, `medterms/lexicon_data/` | Curated lay lexicon: everyday phrases, body-part patterns and clinical abbreviations (`patterns.csv`, `body_parts.csv`), sense cues for ambiguous terms (`senses.csv`), and family / personal history codes (`context_codes.csv`) |
| `eval/annotate_sentences.csv` | 30 transcript-style sentences with the mentions each should yield and words that must not be tagged |
| `eval/lay_terms.csv` | 61 everyday conditions with expected ICD-10-CM / ICD-9-CM code prefixes, and 15 procedures that should reach an NS fee code |
| `medterms/loaders/fees.py` | Payer fee schedules, unit values and payer code lists from a normalized CSV |
| `medterms/extract/` | PDF fee schedule and code list extractor, driven by a per-payer profile; writes the normalized CSV plus a report of lines it couldn't place |
| `medterms/extract/bulletins.py` | Physician's Bulletin extractor: fee announcements as a dated change log (new, updated, terminated) |
| `medterms/profiles/` | Extractor profiles: `ns_msi_fees`, `ns_msi_modifiers`, `ns_msi_explanatory`, `ns_msi_bulletins` |
| `medterms/cli.py` | `medterms init / load / extract / link / lookup / annotate / evaluate` |
| `schema/example_seed.sql`, `schema/example_lookup.sql` | Hand-written rows showing how lay terms map to ranked ICD-10-CM candidates (load into an empty database only, because they use fixed ids) |
| `tests/` | pytest suite, run against sample files written in each source's format; set `MEDTERMS_TEST_PG=postgresql://.../postgres` to run it on PostgreSQL too |

The schema uses SQL that runs unchanged on SQLite, PostgreSQL and MySQL 8, and it follows the
[OMOP vocabulary model](https://ohdsi.github.io/CommonDataModel/) in simplified form. The loaders use
`INSERT ... ON CONFLICT`, which SQLite and PostgreSQL support but MySQL does not.

### Which UMLS sources, and why

The default sources serve lay term → clinical concept → diagnosis, procedure and billing codes; anything
else only adds size. RXNORM and LNC can be added with `--sabs`.

| Source | Contributes |
|---|---|
| CHV, MEDLINEPLUS | Lay terms ("heart attack", "pink eye"), stored as `lay` synonyms |
| MTH | UMLS's own concept names |
| ICD10CM, ICD9CM | Links from concepts to codes loaded from CDC / CMS (the Level 0 subset has ICD-9-CM only) |
| SNOMEDCT_US | Clinical synonyms and SNOMED codes (Full Subset only) |
| MSH | Headings and entry terms only; its ~700k supplementary chemical names are skipped |
| NCI, HPO | Terms on condition, symptom and procedure concepts only |

With the Level 0 subset (2026AA) this loads 381k concepts and 754k terms in about 35 seconds, and the
database with ICD-10-CM, ICD-9-CM, UMLS, NS fees and links is about 400 MB.

### Linking concepts to codes

UMLS puts lay terms on general concepts (Myocardial infarction) while billable codes sit on narrower ones,
and the Level 0 subset has no ICD-10-CM atoms, so only ~5% of lay concepts reach a code through UMLS alone.
`medterms link` adds approximate links (`maps_to_approx`, with a confidence and source `MATCH/<how>`):

| How | Example | Confidence |
|---|---|---|
| name | concept name equals a code's title, inclusion term or index entry | ≤ 1.0 |
| words | same words in any order, ignoring NOS/unspecified, parentheses, "without …", disk/disc | ≤ 0.95 |
| contained | concept's words plus up to two more, unless they change context (pregnancy, postprocedural) | ≤ 0.6 |
| broader | all but one of the concept's words, leaving a disease name (infective cystitis → Cystitis) | ≤ 0.55 |
| gem | an ICD-9-CM link carried to ICD-10-CM through the GEMs, or the reverse | × 0.7–0.8 |

The 2018 GEMs only cover billable ICD-10-CM codes that existed in 2018, so `link` first derives the
missing ICD-10-CM ↔ ICD-9-CM mappings from the ICD-10-CM hierarchy (source `GEM_DERIVED`): a category
takes the ICD-9-CM codes its billable descendants map to when at least 20% agree, counting initial
encounters only and open fractures at half weight (S72.00 "fracture of neck of femur" → 820.8), and a
code added after 2018 borrows from its "unspecified" sibling (F32.A "Depression, unspecified" → 311 /
296.20 via F32.9). `lookup` follows these too.

When a term names one concept and is only a listed synonym of another, the named concept ranks first
("hip fracture" is the name of Hip Fractures; Fracture of pelvis merely lists it).

Diagnosis concepts link only to diagnosis codes and procedure concepts only to procedure codes. Every
link, UMLS's own included, is halved when the code's title adds context the concept doesn't have
("Complications …, hypertension" or "Postprocedural hypertension" for Hypertensive disease) or is a
manifestation code ICD doesn't allow as a primary diagnosis ("… in diseases classified elsewhere").

`medterms evaluate eval/lay_terms.csv` on the Level 0 subset, after linking (a development set too):

| Target | Reached a code | Expected code in top 3 |
|---|---|---|
| ICD-10-CM | 61 / 61 | 61 / 61 |
| ICD-9-CM (NS claim diagnoses) | 61 / 61 | 61 / 61 |
| NS fee codes (procedures) | 15 / 15 | 15 / 15 |

Linking alone reached 93% / 90% / 60%; the rest comes from the curated lexicon ("tummy ache", "ear
infection", "broken arm", "morning sickness", "tubes tied", "stitches"). "Hip fracture" and
"depression" reach ICD-9-CM through derived mappings (below).

### Annotating text

```python
from medterms.db import Database
from medterms.annotate import Annotator

annotator = Annotator(Database("sqlite:///terms.db"))   # loads ~470k terms once: a few seconds, ~250 MB
for m in annotator.annotate("hx of MI, now c/o chest pain and she's always thirsty"):
    print(m.start, m.end, m.text, m.best.name, m.best.key, m.best.codes["ICD10CM"][0].code)
# 6 8 MI Myocardial Infarction umls:C0027051 I21.9
# 18 28 chest pain Chest Pain umls:C0008031 R07.9
# 39 53 always thirsty Polydipsia umls:C0085602 R63.1
```

The annotator scans for the longest run of words (up to 10) that matches a known term, using the
same normalization as the database. Matches never cross punctuation. When the text doesn't match as
written it also tries a plural last word ("heart attacks"), number words ("type two diabetes") and
dropped articles ("blood in the stool"); terms are also indexed without their bracketed parts.

Before the dictionary it tries the **curated lay lexicon**, which covers how people actually talk
and wins over a dictionary match of the same length:

| Said | Tagged as |
|---|---|
| can't put weight on it | Difficulty walking (R26.2 / 719.7) |
| broke his left wrist, cracked a rib | Fracture of carpal bone (S62.10 / 814.00), Rib fractures (S22.3 / 807.00) |
| my knee hurts, pain in her lower back, tummy aches | Knee pain, Low back pain, Abdominal pain |
| sprained her ankle, ankle is swollen | Sprain of ankle, Effusion of ankle |
| quit smoking / smokes a pack a day / doesn't smoke | Z87.891 + V15.82 / Z72.0 + 305.1 / Z72.0 negated |
| can't sleep, not eating, peeing a lot, sugars all over the place | Insomnia, Loss of appetite, Polyuria, Hyperglycemia |
| ear infection, stomach ache, morning sickness | Otitis media, Abdominal pain, Vomiting of pregnancy |
| stitches, tubes tied, wart removal, gallbladder out | with NS fee codes 98.22D, 78.39A, 98.12W; cholecystectomy |

The lexicon is two CSV files in `medterms/lexicon_data/`: `patterns.csv` (phrases with alternatives,
optional words and `<part>` / `<det>` / `<obj>` slots, each mapped to terms or codes) and
`body_parts.csv` (lay words for body parts, e.g. tummy → abdomen, with their usual pain terms). A
target is an ordinary term the dictionary resolves ("fracture of {part}"), a code (`icd10cm:Z87.891`),
codes combined with `+`, or `-` to suppress junk (CHV's "put weight" → Failure to gain weight). When
several targets resolve, the one reaching the most code sets wins. Add lines to grow it, and run
`medterms annotate --check-lexicon` to see which targets resolve in your database (for body-part
patterns, which parts they work for). `medterms lookup` uses the same lexicon when a phrase covers
the whole term. The lexicon is our own content and is committed; nothing in it depends on a UMLS release.

It skips:
- concepts that aren't conditions, symptoms, procedures or findings ("patient", "daughter")
- common conversational words that happen to be UMLS strings ("said", "but")
- side synonyms of words that mainly name something non-clinical: UMLS lists "blood" as a synonym of leukemia
- mentions that reach no code, unless they are a condition, a symptom, or have a clinical word in their name ("Mastectomy", "Chemotherapy")
- abbreviations not written in capitals ("MI" yes, "me" no)

"X and Y" splits into two mentions when both halves are terms.

Each mention also gets context, ConText-style (NegEx extended), from cue words in the same clause:

| Field | Values | Cues, for example |
|---|---|---|
| `assertion` | `negated` / `hypothetical` / `uncertain` | "no", "denies X, Y, or Z", "was ruled out" / "call if", "risk of" / "possible", "rule out", "suspected" |
| `experiencer` | `family` | "mom", "dad", "grandma", "family history of", "runs in the family" (not son / daughter / baby: in a pediatric visit the parent is describing the patient) |
| `temporality` | `history` | "history of", "hx", "s/p", "prior", "years ago", "as a child", "in 2019" |

A cue reaches to the end of its clause (up to 12 words; lists like "denies X, Y, or Z" included); a
contrast or a new subject ends it ("possible kidney infection, **she** has flank pain").

For a relative's or a past condition the codes change, because they are different codes:

| Said | Codes |
|---|---|
| her mom had a stroke | Z82.3 / V17.1 family history of stroke |
| dad had colon cancer and a heart attack | Z80.0 / V16.0, Z82.49 / V17.3 |
| history of breast cancer | Z85.3 / V10.3 personal history of malignant neoplasm of breast |
| hx of MI in 2019 | I25.2 / 412 old myocardial infarction |
| had a stroke two years ago | Z86.73 / V12.54 |
| history of hypertension | I10 / 401.9 unchanged: there's no personal history code, the condition is still there |

The history code comes from `lexicon_data/context_codes.csv` (curated, for conditions filed under a
broader heading: a heart attack under "ischemic heart disease") or else by name ("Family history of …",
"Personal history of …", "Old …"). A relative's condition with no specific code gets the generic
family history code (Z84.89 / V19.8). The condition's own codes stay in `mention.condition_codes`.
Negated, hypothetical and uncertain mentions keep their codes and are only flagged: whether to bill a
"rule out" diagnosis is the caller's decision.

**Ambiguous words and abbreviations.** Clinical abbreviations in the lexicon match only in capitals and
take their usual sense in clinical speech (CP → chest pain, SOB, HTN, DM / T2DM, CAD, CHF, OSA, PNA,
LBP, "high BP" → hypertension, "knee OA", "wrist fx"). Everyday wording settles most of "shot" ("got a
shot in her arm" → injection, "was shot" → gunshot wound). When a mention's top candidates still mean
different things, `lexicon_data/senses.csv` lists cue words for each sense, looked for anywhere else in
the text: "MS" with "numbness", "vision" → multiple sclerosis, with "murmur", "echo" → mitral stenosis;
"PE" with "heparin" → pulmonary embolism, with "vitals", "unremarkable" → physical examination. Without
cue words, the candidate whose ICD-10 chapter the other mentions share wins; otherwise the mention is
flagged `ambiguous` with its best guess first, and `annotate(ambiguous="drop")` leaves such mentions out.
The sense chosen and why is in `mention.cues`.

After loading (2–5 s, ~250 MB), a 5,000-word transcript takes about 0.1–0.2 s.

Candidates carry a canonical `key` ("umls:C0027051", "icd10cm:I21.9"), and with `pip install -e ".[brain]"`
a `node_id`: the first 16 bytes of `blake3(key)`, the same content addressing Brain uses for graph nodes.

`medterms annotate --evaluate eval/annotate_sentences.csv` (67 sentences) finds all 109 expected mentions
with the right context (negated, family, history, uncertain, hypothetical) and sense, and tags none of
42 forbidden phrases. That is a development set: the rules and
lexicon were written while looking at it, so it shows the current state, not accuracy on unseen text.
Known gaps: ambiguity with no cue words or chapter overlap (flagged, not resolved), lower-case
abbreviations ("cp and sob" is left alone), and any everyday wording the lexicon doesn't cover yet.

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

# Physician's Bulletins (2021-present compilation): new, updated and terminated fees with effective dates
medterms extract ns_msi_bulletins MSIPhysiciansBulletin.pdf -o ns_bulletins.csv --compare ns_fees.csv
medterms load fees --payer NS_MSI ns_bulletins.csv  # load after the manual
```

Each extract also writes `<output>.report.md`, which lists records printed without a fee, keys that
appear twice with different fees, and lines that matched nothing. Review it before loading. The
profiles are tuned on the 2026 manual (last updated July 2026); for another edition, check `pages`
first. One code has many rows: 03.03 is priced separately for each schedule section (specialty) and
modifier set, so `fee_schedule` is keyed by code, `section` and `modifier`. Fees printed with
qualifiers (`62+MU`, `4+T`, `Time Only`) keep the number in `units` / `anaesthesia_units` and the
printed text in `fee_note`.

The bulletins fill two gaps in the manual: codes it doesn't list yet (interim fees such as 03.08B,
03.09K/L, NPIV1, TPR1, and the facility on-call codes F1001–F3041), and earlier fees with their
effective dates. Each bulletin row records its issue, announcement heading, `action` and
`effective_start` (from the nearest "Effective <date>" sentence, else the issue date). Loading them
adds dated `fee_schedule` rows with `section = ''` (bulletins don't name a schedule section), sets
`valid_end` on terminated codes, and never renames codes the manual already named. Placeholder
codes (`TBD`, `TBA`) and code mentions without a fee are listed in the report and not loaded.
Explanatory codes announced in bulletins are nearly all in the manual's Appendix I already, so
they aren't extracted from bulletins.

To price a fee on a date, join `fee_schedule` to `unit_value` on the unit (MSU for `units`, AU for
`anaesthesia_units`) with `effective_start <= date` and the latest start per code, section and modifier.

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
