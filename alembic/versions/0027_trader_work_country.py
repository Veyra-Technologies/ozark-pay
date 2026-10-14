"""Assigned country; existing RUB accounts retain their owners and ledger values."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0027_trader_work_country'
down_revision = '0026_crossborder_drafts'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('users', sa.Column('working_country', sa.String(2)))
    op.add_column('users', sa.Column('working_currency', sa.String(3)))
    op.add_column('users', sa.Column('trader_account_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('users.id')))
    op.create_index('ix_users_trader_account_id', 'users', ['trader_account_id'])
    # Before this revision Operator and Trader were aliases for independent accounts.
    # Self-assignment preserves that ownership; new operators must reference an owner.
    op.execute("UPDATE users SET trader_account_id=id WHERE role='operator'")
    op.execute("""UPDATE users u SET working_country='RU', working_currency='RUB'
        WHERE u.role IN ('trader','operator')
        AND NOT EXISTS (SELECT 1 FROM deposits d JOIN requisites r ON r.id=d.requisites_id
                        WHERE r.trader_id=u.id AND d.currency <> 'RUB')
        AND (u.trader_balance <> 0 OR u.trader_hold <> 0
             OR EXISTS (SELECT 1 FROM requisites r WHERE r.trader_id=u.id)
             OR EXISTS (SELECT 1 FROM trader_ledger_entries l WHERE l.trader_id=u.id))""")
    op.create_check_constraint('ck_users_work_country', 'users', "(working_country IS NULL AND working_currency IS NULL) OR (working_country IS NOT NULL AND working_currency IS NOT NULL AND ((working_country='RU' AND working_currency='RUB') OR (working_country='AM' AND working_currency='AMD') OR (working_country='GE' AND working_currency='GEL')))")
    op.execute("""CREATE FUNCTION guard_trader_work_profile() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF OLD.working_country IS NOT NULL AND (NEW.working_country IS DISTINCT FROM OLD.working_country OR NEW.working_currency IS DISTINCT FROM OLD.working_currency) THEN
        RAISE EXCEPTION 'assigned working country is immutable; explicit migration required';
      END IF;
      IF NEW.trader_account_id IS DISTINCT FROM OLD.trader_account_id THEN
        RAISE EXCEPTION 'operator ownership is immutable; explicit migration required';
      END IF;
      IF OLD.working_country IS NULL AND NEW.working_country IS NOT NULL AND NEW.working_currency <> 'RUB' AND
         (OLD.trader_balance <> 0 OR OLD.trader_hold <> 0 OR EXISTS (SELECT 1 FROM requisites WHERE trader_id=OLD.id) OR EXISTS (SELECT 1 FROM trader_ledger_entries WHERE trader_id=OLD.id)) THEN
        RAISE EXCEPTION 'historical RUB account cannot change currency';
      END IF;
      RETURN NEW;
    END $$""")
    op.execute('CREATE TRIGGER users_work_profile_guard BEFORE UPDATE ON users FOR EACH ROW EXECUTE FUNCTION guard_trader_work_profile()')
    op.execute("""CREATE FUNCTION guard_payment_jurisdiction() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE owner_id uuid; country text; currency text;
    BEGIN
      IF TG_TABLE_NAME='requisites' THEN
        IF TG_OP='UPDATE' AND NOT NEW.enabled THEN RETURN NEW; END IF;
        owner_id := NEW.trader_id;
      ELSE
        IF NEW.requisites_id IS NULL THEN RETURN NEW; END IF;
        IF TG_OP='UPDATE' AND NEW.requisites_id IS NOT DISTINCT FROM OLD.requisites_id AND NEW.currency IS NOT DISTINCT FROM OLD.currency THEN RETURN NEW; END IF;
        SELECT trader_id INTO owner_id FROM requisites WHERE id=NEW.requisites_id;
      END IF;
      SELECT working_country, working_currency INTO country, currency FROM users WHERE id=owner_id AND (role='trader' OR (role='operator' AND trader_account_id=id));
      IF country IS DISTINCT FROM 'RU' OR currency IS DISTINCT FROM 'RUB' THEN RAISE EXCEPTION 'payment jurisdiction is not active'; END IF;
      IF TG_TABLE_NAME='deposits' THEN
        IF NEW.currency IS DISTINCT FROM currency THEN RAISE EXCEPTION 'deposit currency mismatch'; END IF;
      END IF;
      RETURN NEW;
    END $$""")
    op.execute('CREATE TRIGGER requisite_jurisdiction_guard BEFORE INSERT OR UPDATE ON requisites FOR EACH ROW EXECUTE FUNCTION guard_payment_jurisdiction()')
    op.execute('CREATE TRIGGER deposit_jurisdiction_guard BEFORE INSERT OR UPDATE ON deposits FOR EACH ROW EXECUTE FUNCTION guard_payment_jurisdiction()')


def downgrade():
    op.execute('DROP TRIGGER deposit_jurisdiction_guard ON deposits')
    op.execute('DROP TRIGGER requisite_jurisdiction_guard ON requisites')
    op.execute('DROP FUNCTION guard_payment_jurisdiction()')
    op.execute('DROP TRIGGER users_work_profile_guard ON users')
    op.execute('DROP FUNCTION guard_trader_work_profile()')
    op.drop_constraint('ck_users_work_country', 'users')
    op.drop_index('ix_users_trader_account_id', 'users')
    op.drop_column('users', 'trader_account_id')
    op.drop_column('users', 'working_currency')
    op.drop_column('users', 'working_country')
