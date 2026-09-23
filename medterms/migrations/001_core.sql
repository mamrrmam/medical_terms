-- Medical terminology schema.
--
-- Written in the common subset of SQLite, PostgreSQL and MySQL 8:
--   * VARCHAR(n) instead of bare TEXT for anything indexed (MySQL can't index TEXT without a prefix length)
--   * SMALLINT 0/1 instead of BOOLEAN (Postgres rejects 0/1 literals for BOOLEAN)
--   * explicit integer ids assigned by the loaders (no AUTOINCREMENT / SERIAL / AUTO_INCREMENT)
--   * ISO dates stored as DATE
--
-- The design is a simplified version of the OHDSI OMOP vocabulary tables
-- (VOCABULARY, CONCEPT, CONCEPT_SYNONYM, CONCEPT_RELATIONSHIP), so OMOP/Athena
-- exports can be loaded later with a straightforward column mapping.

-- One row per source code system and release: ICD10CM, ICD10PCS, SNOMED, RXNORM, CPT, HCPCS, LOINC, LAY, ...
CREATE TABLE vocabulary (
    vocabulary_id   VARCHAR(20)  NOT NULL PRIMARY KEY,
    name            VARCHAR(255) NOT NULL,
    version         VARCHAR(50),               -- e.g. 'FY2027' for ICD-10-CM
    publisher       VARCHAR(255),
    source_url      VARCHAR(1000),
    license         VARCHAR(255),              -- e.g. 'public domain', 'UMLS license', 'AMA license'
    redistributable SMALLINT     NOT NULL DEFAULT 0 CHECK (redistributable IN (0, 1))
);

-- A code in some vocabulary, with its official/preferred name.
CREATE TABLE concept (
    concept_id      INTEGER      NOT NULL PRIMARY KEY,
    vocabulary_id   VARCHAR(20)  NOT NULL REFERENCES vocabulary (vocabulary_id),
    code            VARCHAR(50)  NOT NULL,     -- 'F41.1', '197480003', '313782', '99213'
    name            VARCHAR(1000) NOT NULL,    -- preferred/official description
    domain          VARCHAR(20)  NOT NULL      -- what kind of thing this is
        CHECK (domain IN ('condition', 'symptom', 'drug', 'procedure', 'observation',
                          'measurement', 'device', 'anatomy', 'other')),
    concept_class   VARCHAR(50),               -- vocabulary-specific: 'chapter', 'category', 'ingredient', ...
    is_billable     SMALLINT     NOT NULL DEFAULT 0 CHECK (is_billable IN (0, 1)),  -- valid for claims (ICD-10-CM leaf codes, CPT, HCPCS)
    valid_start     DATE,
    valid_end       DATE,                      -- NULL = still valid
    UNIQUE (vocabulary_id, code)
);
CREATE INDEX idx_concept_name ON concept (name);

-- Alternate names for a concept. This is where colloquial terms live:
-- 'anxiety', 'nerves', 'heart attack', 'water pill', 'tylenol' ...
CREATE TABLE concept_synonym (
    concept_id      INTEGER      NOT NULL REFERENCES concept (concept_id),
    term            VARCHAR(1000) NOT NULL,    -- as written
    term_normalized VARCHAR(1000) NOT NULL,    -- lowercased, punctuation/whitespace collapsed; used for lookup
    term_type       VARCHAR(20)  NOT NULL
        CHECK (term_type IN ('preferred', 'clinical', 'lay', 'abbreviation', 'brand', 'misspelling', 'index')),
    language        VARCHAR(10)  NOT NULL DEFAULT 'en',
    source          VARCHAR(50),               -- where the synonym came from: 'ICD10CM_INDEX', 'CHV', 'MEDLINEPLUS', 'manual', ...
    PRIMARY KEY (concept_id, term_normalized, term_type)
);
CREATE INDEX idx_synonym_term ON concept_synonym (term_normalized);

-- Relationship types, each with its inverse.
CREATE TABLE relationship (
    relationship_id         VARCHAR(30)  NOT NULL PRIMARY KEY,
    name                    VARCHAR(255) NOT NULL,
    reverse_relationship_id VARCHAR(30)  REFERENCES relationship (relationship_id)
);

-- Directed links between concepts, within or across vocabularies:
--   SNOMED 'Generalized anxiety disorder'  --maps_to-->  ICD10CM F41.1
--   ICD10CM F41.1                          --is_a------> ICD10CM F41
--   RxNorm brand 'Tylenol'                 --has_ingredient--> RxNorm 'acetaminophen'
-- Mappings are many-to-many and can be ambiguous, so they carry a type and confidence.
CREATE TABLE concept_relationship (
    concept_id_1    INTEGER      NOT NULL REFERENCES concept (concept_id),
    concept_id_2    INTEGER      NOT NULL REFERENCES concept (concept_id),
    relationship_id VARCHAR(30)  NOT NULL REFERENCES relationship (relationship_id),
    map_priority    SMALLINT,                  -- 1 = default choice when several targets exist
    confidence      REAL,                      -- 0..1, for curated / ML-derived lay mappings
    source          VARCHAR(50),               -- 'NLM_SNOMED_ICD10CM_MAP', 'OMOP', 'manual', ...
    valid_start     DATE,
    valid_end       DATE,
    PRIMARY KEY (concept_id_1, concept_id_2, relationship_id)
);
CREATE INDEX idx_rel_target ON concept_relationship (concept_id_2, relationship_id);

INSERT INTO relationship (relationship_id, name, reverse_relationship_id) VALUES
    ('is_a',            'Is a (child of)',                 NULL),
    ('subsumes',        'Subsumes (parent of)',            NULL),
    ('maps_to',         'Maps to (cross-vocabulary)',      NULL),
    ('mapped_from',     'Mapped from (cross-vocabulary)',  NULL),
    ('may_be',          'Lay term may mean (ambiguous)',   NULL),
    ('may_be_from',     'Possible lay term for',           NULL),
    ('has_ingredient',  'Has ingredient',                  NULL),
    ('ingredient_of',   'Ingredient of',                   NULL);
UPDATE relationship SET reverse_relationship_id = 'subsumes'       WHERE relationship_id = 'is_a';
UPDATE relationship SET reverse_relationship_id = 'is_a'           WHERE relationship_id = 'subsumes';
UPDATE relationship SET reverse_relationship_id = 'mapped_from'    WHERE relationship_id = 'maps_to';
UPDATE relationship SET reverse_relationship_id = 'maps_to'        WHERE relationship_id = 'mapped_from';
UPDATE relationship SET reverse_relationship_id = 'may_be_from'    WHERE relationship_id = 'may_be';
UPDATE relationship SET reverse_relationship_id = 'may_be'         WHERE relationship_id = 'may_be_from';
UPDATE relationship SET reverse_relationship_id = 'ingredient_of'  WHERE relationship_id = 'has_ingredient';
UPDATE relationship SET reverse_relationship_id = 'has_ingredient' WHERE relationship_id = 'ingredient_of';

