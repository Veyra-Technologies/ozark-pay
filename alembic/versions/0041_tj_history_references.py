"""Preserve TJ history even when a journal reference is changed or removed."""
from alembic import op
import sqlalchemy as sa

revision = '0041_tj_history_references'
down_revision = '0040_tj_local_finance'
branch_labels = None
depends_on = None

ANCHOR = '      SELECT * INTO dep FROM deposits WHERE id=CASE'
OLD_SCOPE = """      IF TG_OP='UPDATE' AND EXISTS(SELECT 1 FROM deposits WHERE id=OLD.operation_id AND currency='TJS') THEN
        RAISE EXCEPTION 'TJ financial journal is append-only'; END IF;
"""


def replace_scope(*, remove=False):
    definition = op.get_bind().scalar(sa.text("SELECT pg_get_functiondef('guard_tj_journal_scope()'::regprocedure)"))
    if remove:
        if definition.count(OLD_SCOPE) != 1:
            raise RuntimeError('Unexpected TJ journal history guard')
        definition = definition.replace(OLD_SCOPE, '', 1)
    else:
        if definition.count(ANCHOR) != 1 or OLD_SCOPE in definition:
            raise RuntimeError('Unexpected TJ journal reference guard')
        definition = definition.replace(ANCHOR, OLD_SCOPE + ANCHOR, 1)
    op.execute(definition)


def upgrade():
    replace_scope()
    op.execute("""CREATE FUNCTION protect_tj_deposit_history() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF OLD.currency='TJS' THEN
        IF TG_OP='DELETE' THEN RAISE EXCEPTION 'TJ financial operation history cannot be deleted'; END IF;
        IF ROW(NEW.id,NEW.merchant_id,NEW.amount,NEW.currency,NEW.method,NEW.requisites_id,
          NEW.external_id,NEW.idempotency_key,NEW.request_fingerprint,NEW.created_at,NEW.expires_at)
          IS DISTINCT FROM ROW(OLD.id,OLD.merchant_id,OLD.amount,OLD.currency,OLD.method,OLD.requisites_id,
          OLD.external_id,OLD.idempotency_key,OLD.request_fingerprint,OLD.created_at,OLD.expires_at) THEN
          RAISE EXCEPTION 'TJ accepted payment identity and nominal are immutable'; END IF;
      END IF;
      IF TG_OP='DELETE' THEN RETURN OLD; ELSE RETURN NEW; END IF;
    END $$""")
    op.execute('CREATE TRIGGER tj_deposit_history BEFORE UPDATE OR DELETE ON deposits FOR EACH ROW EXECUTE FUNCTION protect_tj_deposit_history()')
    op.execute("""CREATE FUNCTION protect_tj_fee_snapshot_history() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF OLD.currency='TJS' THEN RAISE EXCEPTION 'TJ financial snapshot history cannot be deleted'; END IF;
      RETURN OLD;
    END $$""")
    op.execute('CREATE TRIGGER tj_fee_snapshot_history BEFORE DELETE ON operation_fee_snapshots FOR EACH ROW EXECUTE FUNCTION protect_tj_fee_snapshot_history()')
    op.execute("""CREATE FUNCTION check_tj_fee_snapshot_accounting() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN PERFORM check_tj_operation_accounting(NEW.deposit_id); RETURN NEW; END $$""")
    op.execute('CREATE CONSTRAINT TRIGGER tj_fee_snapshot_accounting AFTER INSERT OR UPDATE ON operation_fee_snapshots DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION check_tj_fee_snapshot_accounting()')


def downgrade():
    bind = op.get_bind()
    op.execute('LOCK TABLE deposits,operation_fee_snapshots IN ACCESS EXCLUSIVE MODE')
    if bind.scalar(sa.text("SELECT EXISTS(SELECT 1 FROM deposits WHERE currency='TJS') OR EXISTS(SELECT 1 FROM operation_fee_snapshots WHERE currency='TJS')")):
        raise RuntimeError('TJ financial history exists; use a forward-compatible rollback')
    op.execute('DROP TRIGGER tj_fee_snapshot_accounting ON operation_fee_snapshots')
    op.execute('DROP FUNCTION check_tj_fee_snapshot_accounting()')
    op.execute('DROP TRIGGER tj_fee_snapshot_history ON operation_fee_snapshots')
    op.execute('DROP FUNCTION protect_tj_fee_snapshot_history()')
    op.execute('DROP TRIGGER tj_deposit_history ON deposits')
    op.execute('DROP FUNCTION protect_tj_deposit_history()')
    replace_scope(remove=True)
