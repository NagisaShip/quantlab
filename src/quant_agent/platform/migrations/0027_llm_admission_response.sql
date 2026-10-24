-- Billing can remain unresolved after a response has finished using a model slot.
ALTER TABLE llm_admissions ADD COLUMN response_received_at BIGINT;
