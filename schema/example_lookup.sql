-- Colloquial term -> ranked candidate ICD-10-CM codes.
-- Matches the phrase either as a lay concept's own code or as one of its synonyms.
SELECT lay.code        AS lay_term,
       tgt.code        AS icd10cm,
       tgt.name        AS description,
       r.map_priority,
       r.confidence,
       tgt.is_billable
FROM concept lay
JOIN concept_relationship r ON r.concept_id_1 = lay.concept_id AND r.relationship_id = 'may_be'
JOIN concept tgt            ON tgt.concept_id = r.concept_id_2 AND tgt.vocabulary_id = 'ICD10CM'
WHERE lay.vocabulary_id = 'LAY'
  AND (lay.code = 'nerves'
       OR lay.concept_id IN (SELECT concept_id FROM concept_synonym WHERE term_normalized = 'nerves'))
ORDER BY r.map_priority;
