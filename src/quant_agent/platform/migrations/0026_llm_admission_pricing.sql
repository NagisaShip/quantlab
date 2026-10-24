-- Freeze the admitted request's billing rates across provider configuration changes.
ALTER TABLE llm_admissions ADD COLUMN pricing_json TEXT;
