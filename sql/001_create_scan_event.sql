-- 001_create_scan_event.sql
--
-- Floor Supervisor: immutable card-scan events.
--
-- Target:   arrise_vm_db, schema floor_supervisor (owner floor_supervisor_app)
-- Status:   PROPOSED - not applied. Apply only after review/approval:
--             psql "host=... dbname=arrise_vm_db user=floor_supervisor_app sslmode=require" \
--                  -v ON_ERROR_STOP=1 -f sql/001_create_scan_event.sql
--
-- Design notes:
--   * One row = one accepted card scan. No attendance semantics (IN/OUT/shift) are inferred.
--   * No badge credential material is stored (no raw card value, facility code or card number).
--   * Employee fields are a snapshot of hibob_etl.employees at scan time; they are NULL when
--     the HiBob lookup returned no row (hibob_lookup_status = 'NOT_FOUND').
--   * Timestamps are timestamptz (stored as UTC); the application displays America/Bogota.
--
-- Rollback (only while the table holds no data that must be kept, and with approval):
--   BEGIN;
--   DROP TABLE floor_supervisor.scan_event;
--   DROP FUNCTION floor_supervisor.reject_scan_event_modification();
--   COMMIT;

BEGIN;

CREATE TABLE floor_supervisor.scan_event (
    id                          bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,

    -- Identity as resolved by Card Resolver
    hibob_id                    text        NOT NULL,
    card_resolver_employee_name text        NOT NULL,
    card_resolver_dataset_id    integer     NOT NULL,
    hibob_lookup_status         text        NOT NULL,

    -- HiBob snapshot at scan time (NULL when hibob_lookup_status = 'NOT_FOUND')
    employee_name               text,
    job_title                   text,
    department                  text,
    site                        text,
    employment_status           text,
    lifecycle_status            text,

    -- Event context
    device_id                   text        NOT NULL,
    scanned_at                  timestamptz NOT NULL,
    created_at                  timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT scan_event_hibob_id_chk
        CHECK (hibob_id ~ '^\S{1,64}$'),
    CONSTRAINT scan_event_resolver_name_chk
        CHECK (btrim(card_resolver_employee_name) <> ''),
    CONSTRAINT scan_event_device_id_chk
        CHECK (device_id ~ '^[A-Za-z0-9_.-]{1,64}$'),
    CONSTRAINT scan_event_lookup_status_chk
        CHECK (hibob_lookup_status IN ('FOUND', 'NOT_FOUND')),
    CONSTRAINT scan_event_found_has_name_chk
        CHECK (hibob_lookup_status <> 'FOUND' OR employee_name IS NOT NULL),
    CONSTRAINT scan_event_not_found_no_snapshot_chk
        CHECK (
            hibob_lookup_status <> 'NOT_FOUND'
            OR (employee_name IS NULL AND job_title IS NULL AND department IS NULL
                AND site IS NULL AND employment_status IS NULL AND lifecycle_status IS NULL)
        )
);

-- Duplicate detection + per-device employee history.
CREATE INDEX scan_event_device_hibob_scanned_at_idx
    ON floor_supervisor.scan_event (device_id, hibob_id, scanned_at DESC);

-- Time-range reporting.
CREATE INDEX scan_event_scanned_at_idx
    ON floor_supervisor.scan_event (scanned_at);

-- Append-only guard. Protects against accidental modification by the application role;
-- it is not a substitute for separating the owner role from the runtime role.
CREATE FUNCTION floor_supervisor.reject_scan_event_modification()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
BEGIN
    RAISE EXCEPTION 'floor_supervisor.scan_event is append-only: % is not allowed', TG_OP
        USING ERRCODE = 'insufficient_privilege';
END;
$$;

CREATE TRIGGER scan_event_no_update_delete
    BEFORE UPDATE OR DELETE ON floor_supervisor.scan_event
    FOR EACH ROW EXECUTE FUNCTION floor_supervisor.reject_scan_event_modification();

CREATE TRIGGER scan_event_no_truncate
    BEFORE TRUNCATE ON floor_supervisor.scan_event
    FOR EACH STATEMENT EXECUTE FUNCTION floor_supervisor.reject_scan_event_modification();

COMMENT ON TABLE floor_supervisor.scan_event IS
    'Immutable card-scan facts recorded by Floor Supervisor. No attendance state is inferred.';
COMMENT ON COLUMN floor_supervisor.scan_event.hibob_id IS
    'Card Resolver employee.hibob_id = hibob_etl.employees.raw_work_employeeidincompany (text).';
COMMENT ON COLUMN floor_supervisor.scan_event.hibob_lookup_status IS
    'FOUND: HiBob snapshot columns populated. NOT_FOUND: no HiBob row at scan time; snapshot columns NULL.';
COMMENT ON COLUMN floor_supervisor.scan_event.scanned_at IS
    'Application clock (UTC-aware) when the card value was received.';
COMMENT ON COLUMN floor_supervisor.scan_event.created_at IS
    'Database time when the row was inserted.';

COMMIT;
