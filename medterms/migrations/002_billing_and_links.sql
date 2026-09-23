-- Links used by the UMLS and ICD-9-CM loaders, and tables for payer fee schedules.

INSERT INTO relationship (relationship_id, name, reverse_relationship_id) VALUES
    ('has_umls_cui',    'Has UMLS concept',                    NULL),
    ('umls_cui_of',     'UMLS concept of',                     NULL),
    ('maps_to_approx',  'Maps to (approximate)',               NULL),
    ('approx_mapped_from', 'Mapped from (approximate)',        NULL);
UPDATE relationship SET reverse_relationship_id = 'umls_cui_of'        WHERE relationship_id = 'has_umls_cui';
UPDATE relationship SET reverse_relationship_id = 'has_umls_cui'       WHERE relationship_id = 'umls_cui_of';
UPDATE relationship SET reverse_relationship_id = 'approx_mapped_from' WHERE relationship_id = 'maps_to_approx';
UPDATE relationship SET reverse_relationship_id = 'maps_to_approx'     WHERE relationship_id = 'approx_mapped_from';

-- Who pays, and in what unit fees are expressed.
CREATE TABLE payer (
    payer_id        VARCHAR(20)  NOT NULL PRIMARY KEY,   -- 'NS_MSI', 'ON_OHIP', 'US_CMS_PFS'
    name            VARCHAR(255) NOT NULL,
    jurisdiction    VARCHAR(10)  NOT NULL,               -- ISO 3166-2: 'CA-NS', 'CA-ON', 'US'
    fee_vocabulary_id VARCHAR(20) REFERENCES vocabulary (vocabulary_id),  -- where its fee codes live as concepts
    diagnosis_vocabulary_id VARCHAR(20) REFERENCES vocabulary (vocabulary_id),  -- code set required on claims
    unit_name       VARCHAR(20),                         -- 'MSU' for Nova Scotia; NULL when fees are in dollars
    currency        VARCHAR(3)   NOT NULL
);

-- Dollar value of one fee unit over time (e.g. the NS MSU rate).
CREATE TABLE unit_value (
    payer_id        VARCHAR(20)  NOT NULL REFERENCES payer (payer_id),
    effective_start DATE         NOT NULL,
    effective_end   DATE,
    amount_per_unit DECIMAL(10, 4) NOT NULL,
    PRIMARY KEY (payer_id, effective_start)
);

-- One row per fee code (a concept in the payer's fee vocabulary), modifier and period.
-- Fees are in units, in dollars, or both, depending on the payer.
CREATE TABLE fee_schedule (
    payer_id        VARCHAR(20)  NOT NULL REFERENCES payer (payer_id),
    concept_id      INTEGER      NOT NULL REFERENCES concept (concept_id),
    modifier        VARCHAR(50)  NOT NULL DEFAULT '',  -- '' = base fee; 'RO=HDIN' etc. for modified fees
    locality        VARCHAR(20)  NOT NULL DEFAULT '',  -- '' = whole jurisdiction
    effective_start DATE         NOT NULL,
    effective_end   DATE,
    units           DECIMAL(10, 2),
    amount          DECIMAL(10, 2),
    PRIMARY KEY (payer_id, concept_id, modifier, locality, effective_start)
);

-- Which diagnosis a fee code may be billed with, and similar pairings, go in
-- concept_relationship; fee rules that don't fit a table stay in the payer's manual.

-- Nova Scotia physician billing: MSI Health Service Codes (fees in MSU), ICD-9 diagnoses on claims.
INSERT INTO vocabulary (vocabulary_id, name, version, publisher, source_url, license, redistributable) VALUES
    ('ICD9CM', 'ICD-9-CM diagnoses', NULL, 'CMS / NCHS',
     'https://www.cms.gov/medicare/coding-billing/icd-10-codes', 'public domain', 1),
    ('NS_MSI', 'Nova Scotia MSI Health Service Codes', NULL, 'Medavie Blue Cross for NS Department of Health and Wellness',
     'https://msi.medavie.bluecross.ca/physicians-manual/', 'no open licence stated', 0);
INSERT INTO payer (payer_id, name, jurisdiction, fee_vocabulary_id, diagnosis_vocabulary_id, unit_name, currency) VALUES
    ('NS_MSI', 'Nova Scotia Medical Services Insurance', 'CA-NS', 'NS_MSI', 'ICD9CM', 'MSU', 'CAD');
