-- 002_add_entry_method.sql
--
-- Floor Supervisor: audit how each scan was entered (card vs. manual HiBob fallback).
--
-- Target:   arrise_vm_db, schema floor_supervisor. Requires 001_create_scan_event.sql.
--
-- Design notes:
--   * Existing rows become entry_method = 'CARD', fallback_reason = NULL (constant default:
--     no row rewrite and no UPDATE, so the append-only triggers are not involved).
--   * Manual fallback rows have no Card Resolver result, so those two columns become nullable;
--     the consistency CHECK keeps them mandatory for CARD rows.
--   * Manual fallback is only recorded for an existing HiBob employee (hibob_lookup_status = FOUND).
--   * scan_event stays append-only (001 triggers unchanged).
--
-- Rollback (only while no MANUAL_HIBOB_FALLBACK rows exist, and with approval):
--   BEGIN;
--   ALTER TABLE floor_supervisor.scan_event
--       DROP CONSTRAINT scan_event_entry_method_consistency_chk,
--       DROP CONSTRAINT scan_event_entry_method_chk;
--   ALTER TABLE floor_supervisor.scan_event
--       ALTER COLUMN card_resolver_employee_name SET NOT NULL,
--       ALTER COLUMN card_resolver_dataset_id    SET NOT NULL;
--   ALTER TABLE floor_supervisor.scan_event DROP COLUMN fallback_reason, DROP COLUMN entry_method;
--   COMMIT;

BEGIN;
SET LOCAL lock_timeout = '5s';

ALTER TABLE floor_supervisor.scan_event
    ADD COLUMN entry_method    text NOT NULL DEFAULT 'CARD',
    ADD COLUMN fallback_reason text;

ALTER TABLE floor_supervisor.scan_event
    ALTER COLUMN card_resolver_employee_name DROP NOT NULL,
    ALTER COLUMN card_resolver_dataset_id    DROP NOT NULL;

ALTER TABLE floor_supervisor.scan_event
    ADD CONSTRAINT scan_event_entry_method_chk
        CHECK (entry_method IN ('CARD', 'MANUAL_HIBOB_FALLBACK')),
    ADD CONSTRAINT scan_event_entry_method_consistency_chk
        CHECK (
            (entry_method = 'CARD'
               AND fallback_reason IS NULL
               AND card_resolver_employee_name IS NOT NULL
               AND card_resolver_dataset_id IS NOT NULL)
         OR (entry_method = 'MANUAL_HIBOB_FALLBACK'
               AND fallback_reason = 'CARD_NOT_RESOLVED'
               AND card_resolver_employee_name IS NULL
               AND card_resolver_dataset_id IS NULL
               AND hibob_lookup_status = 'FOUND')
        );

COMMENT ON COLUMN floor_supervisor.scan_event.entry_method IS
    'CARD: resolved by Card Resolver. MANUAL_HIBOB_FALLBACK: HiBob ID entered after CARD_NOT_RESOLVED.';
COMMENT ON COLUMN floor_supervisor.scan_event.fallback_reason IS
    'NULL for CARD; CARD_NOT_RESOLVED for MANUAL_HIBOB_FALLBACK.';

COMMIT;
