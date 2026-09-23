-- Provincial and territorial physician fee-for-service payers.
--
-- Each payer's fee codes go in a vocabulary of the same id, loaded with `medterms load fees`.
-- diagnosis_vocabulary_id is set only where the claim diagnosis code set is clear; provinces
-- with their own ICD-9/ICD-8-based lists get their own vocabulary, loaded with `medterms load codes`.
-- None of these publications carries an open licence compatible with CC0, so redistributable = 0.
-- Northwest Territories and Nunavut are left out: their physicians are mostly salaried.

INSERT INTO vocabulary (vocabulary_id, name, publisher, source_url, license, redistributable) VALUES
    ('BC_MSP',      'BC MSC Payment Schedule fee items',                'BC Medical Services Commission',
     'https://www2.gov.bc.ca/gov/content/health/practitioner-professional-resources/msp/physicians/payment-schedules/msc-payment-schedule', 'Crown copyright', 0),
    ('BC_MSP_DX',   'BC MSP diagnostic codes (ICD-9 based)',            'BC Ministry of Health',
     'https://www2.gov.bc.ca/gov/content/health/practitioner-professional-resources/msp/physicians/diagnostic-code-descriptions-icd-9', 'Crown copyright', 0),
    ('AB_AHCIP',    'Alberta Schedule of Medical Benefits health service codes', 'Alberta Health',
     'https://open.alberta.ca/publications', 'Open Government Licence - Alberta (attribution required)', 0),
    ('AB_DX',       'Alberta Health diagnostic codes (ICD-9 based)',    'Alberta Health',
     'https://open.alberta.ca/publications/alberta-health-diagnostic-codes', 'Open Government Licence - Alberta (attribution required)', 0),
    ('SK_MSB',      'Saskatchewan Payment Schedule for physician services', 'Saskatchewan Medical Services Branch',
     'https://www.ehealthsask.ca/', 'Crown copyright', 0),
    ('MB_HEALTH',   'Manitoba Physician''s Manual tariffs',             'Manitoba Health',
     'https://www.gov.mb.ca/health/documents/physmanual.pdf', 'OpenMB licence (attribution required)', 0),
    ('ON_OHIP',     'OHIP Schedule of Benefits fee codes',              'Ontario Ministry of Health',
     'https://www.ontario.ca/page/ohip-schedule-benefits-and-fees', 'King''s Printer for Ontario', 0),
    ('ON_OHIP_DX',  'OHIP diagnostic codes (3-digit, ICD-8 based)',     'Ontario Ministry of Health',
     'https://www.ontario.ca/page/ohip-schedule-benefits-and-fees', 'King''s Printer for Ontario', 0),
    ('QC_RAMQ',     'RAMQ physician billing codes',                     'Régie de l''assurance maladie du Québec',
     'https://www.ramq.gouv.qc.ca/', 'Gouvernement du Québec (authorization required)', 0),
    ('QC_RAMQ_DX',  'RAMQ diagnostic codes (CIM-9 based)',              'Régie de l''assurance maladie du Québec',
     'https://www.ramq.gouv.qc.ca/', 'Gouvernement du Québec (authorization required)', 0),
    ('NB_MEDICARE', 'New Brunswick Physicians'' Manual service codes',  'NB Department of Health',
     'https://www2.gnb.ca/', 'Crown copyright', 0),
    ('PE_HPEI',     'PEI Tariff of Fees',                               'Health PEI',
     'https://www.princeedwardisland.ca/', 'Crown copyright', 0),
    ('NL_MCP',      'NL MCP Medical Payment Schedule fee codes',        'NL Department of Health and Community Services',
     'https://www.gov.nl.ca/hcs/', 'Crown copyright', 0),
    ('YT_YHCIP',    'Yukon Health Care Insurance Plan payment schedule', 'Yukon Insured Health Services',
     'https://yukon.ca/', 'Crown copyright', 0);

INSERT INTO payer (payer_id, name, jurisdiction, fee_vocabulary_id, diagnosis_vocabulary_id, unit_name, currency) VALUES
    ('BC_MSP',      'British Columbia Medical Services Plan',           'CA-BC', 'BC_MSP',      'BC_MSP_DX',  NULL, 'CAD'),
    ('AB_AHCIP',    'Alberta Health Care Insurance Plan',               'CA-AB', 'AB_AHCIP',    'AB_DX',      NULL, 'CAD'),
    ('SK_MSB',      'Saskatchewan Medical Services Branch',             'CA-SK', 'SK_MSB',      NULL,         NULL, 'CAD'),
    ('MB_HEALTH',   'Manitoba Health',                                  'CA-MB', 'MB_HEALTH',   'ICD9CM',     NULL, 'CAD'),
    ('ON_OHIP',     'Ontario Health Insurance Plan',                    'CA-ON', 'ON_OHIP',     'ON_OHIP_DX', NULL, 'CAD'),
    ('QC_RAMQ',     'Régie de l''assurance maladie du Québec',          'CA-QC', 'QC_RAMQ',     'QC_RAMQ_DX', NULL, 'CAD'),
    ('NB_MEDICARE', 'New Brunswick Medicare',                           'CA-NB', 'NB_MEDICARE', NULL,         NULL, 'CAD'),
    ('PE_HPEI',     'Health PEI',                                       'CA-PE', 'PE_HPEI',     NULL,         NULL, 'CAD'),
    ('NL_MCP',      'Newfoundland and Labrador Medical Care Plan',      'CA-NL', 'NL_MCP',      NULL,         NULL, 'CAD'),
    ('YT_YHCIP',    'Yukon Health Care Insurance Plan',                 'CA-YT', 'YT_YHCIP',    NULL,         NULL, 'CAD');
