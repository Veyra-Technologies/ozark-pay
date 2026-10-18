"""Preserve registered advance evidence and canonical TRON transfer identity."""
from alembic import op
import sqlalchemy as sa

revision = '0046_rolling_evidence_integrity'
down_revision = '0045_tj_rolling_admission'
branch_labels = None
depends_on = None


def upgrade():
    # Never merge or rewrite historical obligations to make a new index pass.
    if op.get_bind().scalar(sa.text("""
        SELECT EXISTS(SELECT 1 FROM merchant_rolling_transfers
        WHERE network='TRC20' AND tx_hash ~ '^[0-9a-fA-F]{64}$'
        GROUP BY lower(tx_hash) HAVING count(*)>1)
    """)):
        raise RuntimeError('Duplicate canonical TRON Rolling evidence; reconcile history before upgrade')
    op.execute("""CREATE UNIQUE INDEX uq_rolling_tron_txid_canonical
        ON merchant_rolling_transfers(lower(tx_hash))
        WHERE network='TRC20' AND tx_hash ~ '^[0-9a-fA-F]{64}$'""")
    op.execute("""
    CREATE FUNCTION protect_rolling_transfer_evidence() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_OP='DELETE' THEN RAISE EXCEPTION 'Rolling transfer evidence is immutable'; END IF;
      IF TG_OP='UPDATE' THEN
        IF (to_jsonb(NEW)-ARRAY['status','rolling_account_id','recovered_usdt','remaining_usdt',
             'confirmed_at','confirmed_by','disputed_at','disputed_by','dispute_reason',
             'cancelled_at','cancelled_by','cancel_reason','updated_at'])
          IS DISTINCT FROM
           (to_jsonb(OLD)-ARRAY['status','rolling_account_id','recovered_usdt','remaining_usdt',
             'confirmed_at','confirmed_by','disputed_at','disputed_by','dispute_reason',
             'cancelled_at','cancelled_by','cancel_reason','updated_at']) THEN
          RAISE EXCEPTION 'Rolling transfer evidence is immutable';
        END IF;
        IF (OLD.status='confirmed' AND (NEW.status<>'confirmed'
             OR NEW.rolling_account_id IS DISTINCT FROM OLD.rolling_account_id
             OR NEW.confirmed_at IS DISTINCT FROM OLD.confirmed_at
             OR NEW.confirmed_by IS DISTINCT FROM OLD.confirmed_by))
          OR (OLD.status='cancelled' AND NEW.status<>'cancelled')
          OR (OLD.status='disputed' AND NEW.status NOT IN ('disputed','cancelled')) THEN
          RAISE EXCEPTION 'Rolling transfer confirmation is immutable';
        END IF;
        IF OLD.status='disputed' AND ROW(NEW.disputed_at,NEW.disputed_by,NEW.dispute_reason)
           IS DISTINCT FROM ROW(OLD.disputed_at,OLD.disputed_by,OLD.dispute_reason) THEN
          RAISE EXCEPTION 'Rolling dispute evidence is immutable';
        END IF;
        IF OLD.status='cancelled' AND ROW(NEW.cancelled_at,NEW.cancelled_by,NEW.cancel_reason)
           IS DISTINCT FROM ROW(OLD.cancelled_at,OLD.cancelled_by,OLD.cancel_reason) THEN
          RAISE EXCEPTION 'Rolling cancellation evidence is immutable';
        END IF;
      END IF;
      IF NEW.rolling_account_id IS NOT NULL AND NOT EXISTS(
        SELECT 1 FROM merchant_rolling_accounts a WHERE a.id=NEW.rolling_account_id
          AND a.merchant_id=NEW.merchant_id) THEN
        RAISE EXCEPTION 'Rolling transfer account owner mismatch';
      END IF;
      RETURN NEW;
    END $$;
    CREATE TRIGGER rolling_transfer_evidence BEFORE INSERT OR UPDATE OR DELETE
      ON merchant_rolling_transfers FOR EACH ROW EXECUTE FUNCTION protect_rolling_transfer_evidence();
    """)


def downgrade():
    # A schema-only downgrade must not silently remove money/evidence protections.
    if op.get_bind().scalar(sa.text('SELECT EXISTS(SELECT 1 FROM merchant_rolling_transfers)')):
        raise RuntimeError('Rolling evidence exists; retain integrity guards with a compatible application rollback')
    op.execute('DROP TRIGGER rolling_transfer_evidence ON merchant_rolling_transfers')
    op.execute('DROP FUNCTION protect_rolling_transfer_evidence()')
    op.execute('DROP INDEX uq_rolling_tron_txid_canonical')
