-- BUG #1 fix: vc_balance must never go negative. SQLite doesn't support
-- ALTER TABLE ... ADD CONSTRAINT, so we enforce this with a trigger instead
-- of a full table rebuild (cheaper, lower risk on an existing prod DB).
-- First, clamp any balance that already went negative (e.g. from the old
-- unguarded edit_result revert path) so the trigger doesn't immediately
-- reject legitimate future updates on those rows.
UPDATE users SET vc_balance = 0 WHERE vc_balance < 0;

CREATE TRIGGER IF NOT EXISTS trg_users_vc_balance_nonneg
BEFORE UPDATE OF vc_balance ON users
WHEN NEW.vc_balance < 0
BEGIN
    SELECT RAISE(ABORT, 'vc_balance cannot be negative');
END;

CREATE TRIGGER IF NOT EXISTS trg_users_vc_balance_nonneg_insert
BEFORE INSERT ON users
WHEN NEW.vc_balance < 0
BEGIN
    SELECT RAISE(ABORT, 'vc_balance cannot be negative');
END;
