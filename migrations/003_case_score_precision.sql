-- The case score is an unbounded additive triage-ranking heuristic.
-- Widen storage without changing or capping its value.
ALTER TABLE cases
    ALTER COLUMN score TYPE NUMERIC(12, 2)
    USING score::numeric(12, 2);