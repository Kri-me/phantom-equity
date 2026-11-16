-- Store the CEMA proxy's three evidence tiers as text.
-- Safe for databases created with the earlier BOOLEAN definition.
ALTER TABLE signal_cema_proxy
    ALTER COLUMN cema_proxy TYPE TEXT
    USING cema_proxy::text;