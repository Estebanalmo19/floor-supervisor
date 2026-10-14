-- 003_allow_resolved_card_without_resolver_name.sql
--
-- Floor Supervisor: allow a CARD scan whose Card Resolver answer has no employee name.
--
-- Target:   arrise_vm_db, schema floor_supervisor. Requires 001 and 002.
--
-- Why:
--   A card registered through the fallback (Card Resolver POST /cards/update) is stored as a
--   manual mapping. When its HiBob ID is not in Card Resolver's active import dataset,
--   /cards/resolve answers successfully with a valid hibob_id but "name": null
--   ("source": "manual_mapping"). That is a genuine CARD resolution, so it is recorded as
--   entry_method = 'CARD' with card_resolver_employee_name = NULL. The HiBob snapshot
--   (employee_name, ...) is filled in when the HiBob lookup succeeds, exactly as before.
--   card_resolver_employee_name is never back-filled with the HiBob name: the column keeps
--   meaning "the name Card Resolver returned".
--
-- Change:
--   Only scan_event_entry_method_consistency_chk is replaced. The CARD branch drops the
--   "card_resolver_employee_name IS NOT NULL" condition; everything else is identical
--   (CARD still requires fallback_reason IS NULL and card_resolver_dataset_id IS NOT NULL;
--   MANUAL_HIBOB_FALLBACK is unchanged). The new CHECK is strictly looser than the old one,
--   so every existing row remains valid. scan_event_resolver_name_chk (btrim(...) <> '')
--   from 001 still rejects blank names and already accepts NULL.
--   No data is rewritten; the append-only triggers from 001 are not involved.
--
-- Rollback (only while no CARD row has card_resolver_employee_name IS NULL, and with approval):
--   BEGIN;
--   ALTER TABLE floor_supervisor.scan_event
--       DROP CONSTRAINT scan_event_entry_method_consistency_chk,
--       ADD CONSTRAINT scan_event_entry_method_consistency_chk
--           CHECK (
--               (entry_method = 'CARD'
--                  AND fallback_reason IS NULL
--                  AND card_resolver_employee_name IS NOT NULL
--                  AND card_resolver_dataset_id IS NOT NULL)
--            OR (entry_method = 'MANUAL_HIBOB_FALLBACK'
--                  AND fallback_reason = 'CARD_NOT_RESOLVED'
--                  AND card_resolver_employee_name IS NULL
--                  AND card_resolver_dataset_id IS NULL
--                  AND hibob_lookup_status = 'FOUND')
--           );
--   COMMIT;

BEGIN;
SET LOCAL lock_timeout = '5s';

ALTER TABLE floor_supervisor.scan_event
    DROP CONSTRAINT scan_event_entry_method_consistency_chk,
    ADD CONSTRAINT scan_event_entry_method_consistency_chk
        CHECK (
            (entry_method = 'CARD'
               AND fallback_reason IS NULL
               AND card_resolver_dataset_id IS NOT NULL)
         OR (entry_method = 'MANUAL_HIBOB_FALLBACK'
               AND fallback_reason = 'CARD_NOT_RESOLVED'
               AND card_resolver_employee_name IS NULL
               AND card_resolver_dataset_id IS NULL
               AND hibob_lookup_status = 'FOUND')
        );

COMMENT ON COLUMN floor_supervisor.scan_event.card_resolver_employee_name IS
    'Name returned by Card Resolver. NULL for MANUAL_HIBOB_FALLBACK, and for CARD scans of a '
    'manually mapped card whose HiBob ID is not in the active Card Resolver dataset.';

COMMIT;
