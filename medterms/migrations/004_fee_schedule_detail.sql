-- Fee schedule detail needed for schedules like Nova Scotia's, where one fee code
-- (03.03) carries different fees by schedule section (specialty), modifier set and
-- category, and fees are printed as '62+MU', '20 | 4+T' or 'Time Only'.
--
-- fee_schedule and unit_value are rebuilt (SQLite can't change a primary key in place).

CREATE TABLE fee_schedule_new (
    payer_id        VARCHAR(20)  NOT NULL REFERENCES payer (payer_id),
    concept_id      INTEGER      NOT NULL REFERENCES concept (concept_id),
    section         VARCHAR(100) NOT NULL DEFAULT '',  -- schedule section / specialty, e.g. 'Anaesthesia'; '' = whole schedule
    modifier        VARCHAR(255) NOT NULL DEFAULT '',  -- '' = base fee; 'LO=HOME, RO=DETE (RF=REFD)' etc.
    locality        VARCHAR(20)  NOT NULL DEFAULT '',  -- '' = whole jurisdiction
    effective_start DATE         NOT NULL,
    effective_end   DATE,
    category        VARCHAR(20),                       -- payer's service category, e.g. NS 'VIST', 'MASG'
    heading         VARCHAR(255),                      -- group heading the row is printed under
    description     VARCHAR(1000),                     -- this row's description, when it differs by modifier
    units           DECIMAL(10, 2),
    amount          DECIMAL(10, 2),
    anaesthesia_units DECIMAL(10, 2),                  -- NS base anaesthetic units (paid at the AU rate), '+T' time units extra
    fee_note        VARCHAR(255),                      -- fee as printed when not a plain number: 'units 62+MU', 'units Time Only'
    PRIMARY KEY (payer_id, concept_id, section, modifier, locality, effective_start)
);
INSERT INTO fee_schedule_new (payer_id, concept_id, modifier, locality, effective_start, effective_end, units, amount)
    SELECT payer_id, concept_id, modifier, locality, effective_start, effective_end, units, amount FROM fee_schedule;
DROP TABLE fee_schedule;
ALTER TABLE fee_schedule_new RENAME TO fee_schedule;

-- Payers can have more than one unit: Nova Scotia pays MSU for most services and AU for anaesthesia.
CREATE TABLE unit_value_new (
    payer_id        VARCHAR(20)  NOT NULL REFERENCES payer (payer_id),
    unit_name       VARCHAR(20)  NOT NULL DEFAULT '',
    effective_start DATE         NOT NULL,
    effective_end   DATE,
    amount_per_unit DECIMAL(10, 4) NOT NULL,
    PRIMARY KEY (payer_id, unit_name, effective_start)
);
INSERT INTO unit_value_new (payer_id, unit_name, effective_start, effective_end, amount_per_unit)
    SELECT u.payer_id, COALESCE(p.unit_name, ''), u.effective_start, u.effective_end, u.amount_per_unit
    FROM unit_value u JOIN payer p ON p.payer_id = u.payer_id;
DROP TABLE unit_value;
ALTER TABLE unit_value_new RENAME TO unit_value;

-- Nova Scotia claim modifiers (LO=HOME) and explanatory (adjudication) codes, from the Physician's Manual appendices.
INSERT INTO vocabulary (vocabulary_id, name, publisher, source_url, license, redistributable) VALUES
    ('NS_MSI_MOD',  'Nova Scotia MSI modifier values',   'Medavie Blue Cross for NS Department of Health and Wellness',
     'https://msi.medavie.bluecross.ca/physicians-manual/', 'no open licence stated', 0),
    ('NS_MSI_EXPL', 'Nova Scotia MSI explanatory codes', 'Medavie Blue Cross for NS Department of Health and Wellness',
     'https://msi.medavie.bluecross.ca/physicians-manual/', 'no open licence stated', 0);
