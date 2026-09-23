-- A handful of hand-entered rows showing how the tables fit together.
-- Real data comes from the loaders, not from this file.

INSERT INTO vocabulary (vocabulary_id, name, version, publisher, source_url, license, redistributable) VALUES
    ('ICD10CM', 'ICD-10-CM', 'FY2027', 'CDC / NCHS', 'https://www.cdc.gov/nchs/icd/icd-10-cm/', 'public domain', 1),
    ('LAY',     'Colloquial / consumer terms', '0.1', 'this project', NULL, 'CC0', 1);

INSERT INTO concept (concept_id, vocabulary_id, code, name, domain, concept_class, is_billable) VALUES
    (1, 'ICD10CM', 'F41',   'Other anxiety disorders',              'condition', 'category', 0),
    (2, 'ICD10CM', 'F41.1', 'Generalized anxiety disorder',         'condition', 'code',     1),
    (3, 'ICD10CM', 'F41.9', 'Anxiety disorder, unspecified',        'condition', 'code',     1),
    (4, 'ICD10CM', 'F41.0', 'Panic disorder [episodic paroxysmal anxiety]', 'condition', 'code', 1),
    (5, 'ICD10CM', 'R45.0', 'Nervousness',                          'symptom',   'code',     1),
    (6, 'ICD10CM', 'I21.9', 'Acute myocardial infarction, unspecified', 'condition', 'code', 1),
    -- Lay terms are concepts of their own so that one colloquial phrase can point at several candidates.
    (1000001, 'LAY', 'anxiety',      'anxiety',      'condition', 'lay_term', 0),
    (1000002, 'LAY', 'heart attack', 'heart attack', 'condition', 'lay_term', 0);

INSERT INTO concept_synonym (concept_id, term, term_normalized, term_type, source) VALUES
    (1000001, 'nerves',       'nerves',       'lay',          'manual'),
    (1000001, 'anxious',      'anxious',      'lay',          'manual'),
    (1000002, 'MI',           'mi',           'abbreviation', 'manual'),
    (6,       'Heart attack', 'heart attack', 'lay',          'manual'),
    (2,       'GAD',          'gad',          'abbreviation', 'manual');

INSERT INTO concept_relationship (concept_id_1, concept_id_2, relationship_id, map_priority, confidence, source) VALUES
    (2, 1, 'is_a', NULL, NULL, 'ICD10CM'),
    (3, 1, 'is_a', NULL, NULL, 'ICD10CM'),
    (4, 1, 'is_a', NULL, NULL, 'ICD10CM'),
    -- "anxiety" alone does not justify GAD; unspecified is the safe default, specific diagnoses are candidates.
    (1000001, 3, 'may_be', 1, 0.60, 'manual'),
    (1000001, 2, 'may_be', 2, 0.25, 'manual'),
    (1000001, 5, 'may_be', 3, 0.10, 'manual'),
    (1000001, 4, 'may_be', 4, 0.05, 'manual'),
    (1000002, 6, 'may_be', 1, 0.90, 'manual');
