"""Currency-explicit trader wallets, historical FX snapshots and local banks."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = '0029_trader_wallets_local_banks'
down_revision = '0028_xe_crossborder_quotes'
branch_labels = None
depends_on = None


VERIFIED_ON = '2026-10-03'
AM_SOURCE = 'https://www.cba.am/en/licensed-banks/'
GE_BANK_SOURCE = 'https://nbg.gov.ge/en/licensed-commercial-banks'
GE_MICRO_SOURCE = 'https://nbg.gov.ge/en/page/licensed-microbanks'

BANKS = [
    ('am-acba', 'AM', 'ACBA Bank', 'bank', AM_SOURCE),
    ('am-ameriabank', 'AM', 'Ameriabank', 'bank', AM_SOURCE),
    ('am-amio', 'AM', 'AMIO Bank', 'bank', AM_SOURCE),
    ('am-armeconombank', 'AM', 'ARMECONOMBANK', 'bank', AM_SOURCE),
    ('am-ardshinbank', 'AM', 'Ardshinbank', 'bank', AM_SOURCE),
    ('am-araratbank', 'AM', 'ARARATBANK', 'bank', AM_SOURCE),
    ('am-armswissbank', 'AM', 'ArmSwissBank', 'bank', AM_SOURCE),
    ('am-artsakhbank', 'AM', 'Artsakhbank', 'bank', AM_SOURCE),
    ('am-byblos', 'AM', 'Byblos Bank Armenia', 'bank', AM_SOURCE),
    ('am-converse', 'AM', 'Converse Bank', 'bank', AM_SOURCE),
    ('am-evocabank', 'AM', 'Evocabank', 'bank', AM_SOURCE),
    ('am-fast', 'AM', 'Fast Bank', 'bank', AM_SOURCE),
    ('am-idbank', 'AM', 'IDBank', 'bank', AM_SOURCE),
    ('am-inecobank', 'AM', 'Inecobank', 'bank', AM_SOURCE),
    ('am-mellat', 'AM', 'Mellat Bank', 'bank', AM_SOURCE),
    ('am-unibank', 'AM', 'Unibank', 'bank', AM_SOURCE),
    ('am-vtb', 'AM', 'VTB Bank (Armenia)', 'bank', AM_SOURCE),
    ('ge-pave', 'GE', 'Pave Bank Georgia', 'bank', GE_BANK_SOURCE),
    ('ge-hash', 'GE', 'Hash Bank', 'bank', GE_BANK_SOURCE),
    ('ge-paysera', 'GE', 'Paysera Bank Georgia', 'bank', GE_BANK_SOURCE),
    ('ge-credo', 'GE', 'Credo Bank', 'bank', GE_BANK_SOURCE),
    ('ge-isbank', 'GE', 'Isbank Georgia', 'bank', GE_BANK_SOURCE),
    ('ge-pasha', 'GE', 'PASHA Bank Georgia', 'bank', GE_BANK_SOURCE),
    ('ge-halyk', 'GE', 'Halyk Bank Georgia', 'bank', GE_BANK_SOURCE),
    ('ge-terabank', 'GE', 'TeraBank', 'bank', GE_BANK_SOURCE),
    ('ge-silk', 'GE', 'Silk Bank', 'bank', GE_BANK_SOURCE),
    ('ge-procredit', 'GE', 'ProCredit Bank', 'bank', GE_BANK_SOURCE),
    ('ge-ziraat', 'GE', 'Ziraat Bank Georgia', 'bank', GE_BANK_SOURCE),
    ('ge-cartu', 'GE', 'Cartu Bank', 'bank', GE_BANK_SOURCE),
    ('ge-vtb', 'GE', 'VTB Bank (Georgia)', 'bank', GE_BANK_SOURCE),
    ('ge-bank-of-georgia', 'GE', 'Bank of Georgia', 'bank', GE_BANK_SOURCE),
    ('ge-basisbank', 'GE', 'Basisbank', 'bank', GE_BANK_SOURCE),
    ('ge-liberty', 'GE', 'Liberty Bank', 'bank', GE_BANK_SOURCE),
    ('ge-tbc', 'GE', 'TBC Bank', 'bank', GE_BANK_SOURCE),
    ('ge-micro-crystal', 'GE', 'Crystal', 'microbank', GE_MICRO_SOURCE),
    ('ge-micro-mbc', 'GE', 'MBC', 'microbank', GE_MICRO_SOURCE),
]


def upgrade():
    op.create_table(
        'bank_directory',
        sa.Column('id', sa.String(64), primary_key=True),
        sa.Column('country', sa.String(2), nullable=False),
        sa.Column('name', sa.String(160), nullable=False),
        sa.Column('institution_type', sa.String(24), nullable=False),
        sa.Column('status', sa.String(16), nullable=False, server_default='active'),
        sa.Column('aliases', postgresql.JSONB(), nullable=False, server_default='[]'),
        sa.Column('source_url', sa.String(500), nullable=False),
        sa.Column('verified_on', sa.Date(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint('country', 'name', name='uq_bank_directory_country_name'),
        sa.CheckConstraint("country IN ('AM','GE')", name='ck_bank_directory_country'),
        sa.CheckConstraint("institution_type IN ('bank','microbank')", name='ck_bank_directory_type'),
        sa.CheckConstraint("status IN ('active','inactive')", name='ck_bank_directory_status'),
    )
    op.create_index('ix_bank_directory_country', 'bank_directory', ['country'])
    op.create_index('ix_bank_directory_status', 'bank_directory', ['status'])
    bind = op.get_bind()
    seed = sa.text("""
        INSERT INTO bank_directory
          (id,country,name,institution_type,status,aliases,source_url,verified_on,created_at,updated_at)
        VALUES (:id,:country,:name,:kind,'active',CAST(:aliases AS jsonb),:source,CAST(:verified AS date),now(),now())
        ON CONFLICT (id) DO UPDATE SET
          country=EXCLUDED.country, name=EXCLUDED.name,
          institution_type=EXCLUDED.institution_type, status=EXCLUDED.status,
          source_url=EXCLUDED.source_url, verified_on=EXCLUDED.verified_on,
          updated_at=now()
    """)
    for bank_id, country, name, kind, source in BANKS:
        bind.execute(seed, dict(id=bank_id, country=country, name=name, kind=kind, source=source, verified=VERIFIED_ON, aliases='[]'))

    op.create_table(
        'trader_wallets',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('trader_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('users.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('currency', sa.String(8), nullable=False, server_default='USDT'),
        sa.Column('balance', sa.Numeric(24, 8), nullable=False, server_default='0'),
        sa.Column('hold', sa.Numeric(24, 8), nullable=False, server_default='0'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint('trader_id', 'currency', name='uq_trader_wallet_currency'),
        sa.CheckConstraint("currency = 'USDT'", name='ck_trader_wallet_usdt'),
        sa.CheckConstraint('balance >= 0 AND hold >= 0 AND hold <= balance', name='ck_trader_wallet_amounts'),
    )
    op.create_index('ix_trader_wallets_trader_id', 'trader_wallets', ['trader_id'])
    op.execute("""INSERT INTO trader_wallets (id,trader_id,currency,balance,hold)
        SELECT gen_random_uuid(),id,'USDT',0,0 FROM users
        WHERE role='trader' OR (role='operator' AND trader_account_id=id)
        ON CONFLICT (trader_id,currency) DO NOTHING""")
    op.create_table(
        'trader_wallet_ledger_entries',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('wallet_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('trader_wallets.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('trader_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('users.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('operation_id', postgresql.UUID(as_uuid=True)),
        sa.Column('entry_type', sa.String(64), nullable=False),
        sa.Column('amount', sa.Numeric(24, 8), nullable=False),
        sa.Column('balance_after', sa.Numeric(24, 8), nullable=False),
        sa.Column('hold_after', sa.Numeric(24, 8), nullable=False),
        sa.Column('currency', sa.String(8), nullable=False, server_default='USDT'),
        sa.Column('description', sa.String(500), nullable=False, server_default=''),
        sa.Column('idempotency_key', sa.String(160), unique=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("currency = 'USDT'", name='ck_trader_wallet_ledger_usdt'),
        sa.CheckConstraint('balance_after >= 0 AND hold_after >= 0 AND hold_after <= balance_after', name='ck_trader_wallet_ledger_amounts'),
    )
    for name, cols in (
        ('ix_trader_wallet_ledger_entries_wallet_id', ['wallet_id']),
        ('ix_trader_wallet_ledger_entries_trader_id', ['trader_id']),
        ('ix_trader_wallet_ledger_entries_operation_id', ['operation_id']),
        ('ix_trader_wallet_ledger_entries_entry_type', ['entry_type']),
    ):
        op.create_index(name, 'trader_wallet_ledger_entries', cols)
    op.execute("""CREATE FUNCTION protect_trader_wallet_ledger() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN RAISE EXCEPTION 'trader wallet ledger entries are immutable'; END $$""")
    op.execute('CREATE TRIGGER trader_wallet_ledger_immutable BEFORE UPDATE OR DELETE ON trader_wallet_ledger_entries FOR EACH ROW EXECUTE FUNCTION protect_trader_wallet_ledger()')

    op.create_table(
        'trader_operation_snapshots',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('deposit_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('deposits.id', ondelete='RESTRICT'), nullable=False, unique=True),
        sa.Column('trader_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('users.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('local_currency', sa.String(8), nullable=False),
        sa.Column('local_amount', sa.Numeric(24, 8), nullable=False),
        sa.Column('settlement_usdt', sa.Numeric(24, 8), nullable=False),
        sa.Column('profit_local', sa.Numeric(24, 8), nullable=False),
        sa.Column('profit_usdt', sa.Numeric(24, 8), nullable=False),
        sa.Column('rate_local_per_usdt', sa.Numeric(24, 12), nullable=False),
        sa.Column('rate_source', sa.String(16), nullable=False, server_default='xe'),
        sa.Column('provider_timestamp', sa.DateTime(timezone=True), nullable=False),
        sa.Column('fetched_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("local_currency IN ('AMD','GEL')", name='ck_trader_operation_local_currency'),
        sa.CheckConstraint("rate_source = 'xe'", name='ck_trader_operation_rate_source'),
        sa.CheckConstraint('local_amount >= 0 AND settlement_usdt >= 0 AND profit_local >= 0 AND profit_usdt >= 0 AND rate_local_per_usdt > 0', name='ck_trader_operation_amounts'),
    )
    op.create_index('ix_trader_operation_snapshots_deposit_id', 'trader_operation_snapshots', ['deposit_id'], unique=True)
    op.create_index('ix_trader_operation_snapshots_trader_id', 'trader_operation_snapshots', ['trader_id'])
    op.create_index('ix_trader_operation_snapshots_local_currency', 'trader_operation_snapshots', ['local_currency'])
    op.execute("""CREATE FUNCTION protect_trader_operation_snapshot() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN RAISE EXCEPTION 'trader operation snapshots are immutable'; END $$""")
    op.execute('CREATE TRIGGER trader_operation_snapshot_immutable BEFORE UPDATE OR DELETE ON trader_operation_snapshots FOR EACH ROW EXECUTE FUNCTION protect_trader_operation_snapshot()')

    op.add_column('requisites', sa.Column('bank_id', sa.String(64), sa.ForeignKey('bank_directory.id', ondelete='RESTRICT')))
    op.add_column('requisites', sa.Column('country', sa.String(2), nullable=False, server_default='RU'))
    op.add_column('requisites', sa.Column('currency', sa.String(8), nullable=False, server_default='RUB'))
    op.create_index('ix_requisites_bank_id', 'requisites', ['bank_id'])
    op.create_index('ix_requisites_country', 'requisites', ['country'])
    op.create_index('ix_requisites_currency', 'requisites', ['currency'])

    op.execute('DROP TRIGGER requisite_jurisdiction_guard ON requisites')
    op.execute('DROP TRIGGER deposit_jurisdiction_guard ON deposits')
    op.execute('DROP FUNCTION guard_payment_jurisdiction()')
    op.execute("""CREATE FUNCTION guard_payment_jurisdiction() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE owner_id uuid; profile_country text; profile_currency text; req_country text; req_currency text; bank_country text; bank_status text;
    BEGIN
      IF TG_TABLE_NAME='requisites' THEN
        owner_id := NEW.trader_id;
        SELECT working_country,working_currency INTO profile_country,profile_currency FROM users
          WHERE id=owner_id AND (role='trader' OR (role='operator' AND trader_account_id=id));
        IF profile_country IS NULL THEN RAISE EXCEPTION 'payment jurisdiction is not assigned'; END IF;
        IF NEW.country IS DISTINCT FROM profile_country OR NEW.currency IS DISTINCT FROM profile_currency THEN
          RAISE EXCEPTION 'requisite jurisdiction mismatch';
        END IF;
        IF profile_country='RU' THEN
          IF NEW.bank_id IS NOT NULL THEN RAISE EXCEPTION 'foreign bank is not valid for RU'; END IF;
        ELSE
          IF NEW.method IS DISTINCT FROM 'c2c' OR NEW.bank_id IS NULL THEN RAISE EXCEPTION 'foreign requisite requires a directory bank and c2c'; END IF;
          SELECT country,status INTO bank_country,bank_status FROM bank_directory WHERE id=NEW.bank_id;
          IF bank_country IS DISTINCT FROM profile_country OR bank_status IS DISTINCT FROM 'active' THEN RAISE EXCEPTION 'bank country mismatch or bank inactive'; END IF;
          IF NEW.enabled THEN RAISE EXCEPTION 'foreign payment directions are not enabled'; END IF;
        END IF;
      ELSE
        IF NEW.requisites_id IS NULL THEN RETURN NEW; END IF;
        SELECT trader_id,country,currency INTO owner_id,req_country,req_currency FROM requisites WHERE id=NEW.requisites_id;
        SELECT working_country,working_currency INTO profile_country,profile_currency FROM users
          WHERE id=owner_id AND (role='trader' OR (role='operator' AND trader_account_id=id));
        IF req_country IS DISTINCT FROM profile_country OR req_currency IS DISTINCT FROM profile_currency OR NEW.currency IS DISTINCT FROM req_currency THEN
          RAISE EXCEPTION 'deposit currency mismatch';
        END IF;
      END IF;
      RETURN NEW;
    END $$""")
    op.execute('CREATE TRIGGER requisite_jurisdiction_guard BEFORE INSERT OR UPDATE ON requisites FOR EACH ROW EXECUTE FUNCTION guard_payment_jurisdiction()')
    op.execute('CREATE TRIGGER deposit_jurisdiction_guard BEFORE INSERT OR UPDATE ON deposits FOR EACH ROW EXECUTE FUNCTION guard_payment_jurisdiction()')


def downgrade():
    bind = op.get_bind()
    if bind.scalar(sa.text("SELECT count(*) FROM trader_wallets WHERE balance<>0 OR hold<>0")):
        raise RuntimeError('Export non-zero USDT trader wallets before downgrading')
    if bind.scalar(sa.text('SELECT count(*) FROM trader_wallet_ledger_entries')) or bind.scalar(sa.text('SELECT count(*) FROM trader_operation_snapshots')):
        raise RuntimeError('Export immutable trader financial history before downgrading')
    if bind.scalar(sa.text('SELECT count(*) FROM requisites WHERE bank_id IS NOT NULL')):
        raise RuntimeError('Foreign bank references require an explicit downgrade migration')
    op.execute('DROP TRIGGER deposit_jurisdiction_guard ON deposits')
    op.execute('DROP TRIGGER requisite_jurisdiction_guard ON requisites')
    op.execute('DROP FUNCTION guard_payment_jurisdiction()')
    op.drop_index('ix_requisites_currency', table_name='requisites')
    op.drop_index('ix_requisites_country', table_name='requisites')
    op.drop_index('ix_requisites_bank_id', table_name='requisites')
    op.drop_column('requisites', 'currency')
    op.drop_column('requisites', 'country')
    op.drop_column('requisites', 'bank_id')
    op.execute('DROP TRIGGER trader_operation_snapshot_immutable ON trader_operation_snapshots')
    op.execute('DROP FUNCTION protect_trader_operation_snapshot()')
    op.drop_table('trader_operation_snapshots')
    op.execute('DROP TRIGGER trader_wallet_ledger_immutable ON trader_wallet_ledger_entries')
    op.execute('DROP FUNCTION protect_trader_wallet_ledger()')
    op.drop_table('trader_wallet_ledger_entries')
    op.drop_table('trader_wallets')
    op.drop_table('bank_directory')
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
      SELECT working_country,working_currency INTO country,currency FROM users WHERE id=owner_id AND (role='trader' OR (role='operator' AND trader_account_id=id));
      IF country IS DISTINCT FROM 'RU' OR currency IS DISTINCT FROM 'RUB' THEN RAISE EXCEPTION 'payment jurisdiction is not active'; END IF;
      IF TG_TABLE_NAME='deposits' AND NEW.currency IS DISTINCT FROM currency THEN RAISE EXCEPTION 'deposit currency mismatch'; END IF;
      RETURN NEW;
    END $$""")
    op.execute('CREATE TRIGGER requisite_jurisdiction_guard BEFORE INSERT OR UPDATE ON requisites FOR EACH ROW EXECUTE FUNCTION guard_payment_jurisdiction()')
    op.execute('CREATE TRIGGER deposit_jurisdiction_guard BEFORE INSERT OR UPDATE ON deposits FOR EACH ROW EXECUTE FUNCTION guard_payment_jurisdiction()')
