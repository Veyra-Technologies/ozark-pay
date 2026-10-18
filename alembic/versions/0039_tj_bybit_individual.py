"""Replace the TJ provider without rewriting published 0038 or historical quotes.

Existing TJ cabinets retain IDs, wallets, keys, and individual tariffs. Legacy
provider assignments are retained as history. No RU/KZ/GE row is rewritten.
"""
import json
import uuid
from datetime import datetime, timezone

import sqlalchemy as sa
from alembic import op

revision = '0039_tj_bybit_individual'
down_revision = '0038_tj_cabinet_preparation'
branch_labels = None
depends_on = None

OLD = 'rapira_green_tjs'
NEW = 'bybit_p2p_tjs_green_avg3'


def replace_check(bind, table, name, old, new, *, defer_validation=False):
    definition = bind.scalar(sa.text('SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname=:name'), {'name': name})
    if not definition or old not in definition:
        raise RuntimeError('Unexpected TJ constraint: ' + name)
    expression = definition[len('CHECK ('):-1].replace(old, new)
    op.drop_constraint(name, table, type_='check')
    if defer_validation:
        op.execute(f'ALTER TABLE {table} ADD CONSTRAINT {name} CHECK ({expression}) NOT VALID')
    else:
        op.create_check_constraint(name, table, expression)


def upgrade():
    bind = op.get_bind()
    op.execute('LOCK TABLE users, merchants, parser_assignments, parser_configurations IN SHARE ROW EXCLUSIVE MODE')
    if bind.scalar(sa.text("SELECT count(*) FROM parser_configurations WHERE parser_id=:old AND is_active"), {'old': OLD}):
        raise RuntimeError('Unexpected active legacy TJ parser; review before migration')
    op.alter_column('users', 'working_rate_source', type_=sa.String(64), existing_type=sa.String(16))
    replace_check(bind, 'users', 'ck_users_work_rate_source', OLD, NEW, defer_validation=True)
    replace_check(bind, 'merchants', 'ck_merchant_country_parser', OLD, NEW, defer_validation=True)
    config_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    bind.execute(sa.text("""INSERT INTO parser_configurations
        (id,parser_id,version,country,direction,parameters,is_active,created_at,updated_at)
        VALUES (:id,:parser,1,'TJ','tj-tj',CAST(:params AS jsonb),false,:now,:now)"""),
        {'id': config_id, 'parser': NEW, 'now': now, 'params': json.dumps({
            'refresh_seconds': 15, 'ttl_seconds': 60, 'contract_confirmed': False})})
    bind.execute(sa.text("UPDATE users SET working_rate_source=:new WHERE working_country='TJ' AND working_rate_source=:old"), {'old': OLD, 'new': NEW})
    bind.execute(sa.text("UPDATE merchants SET rate_parser_id=:new WHERE working_country='TJ' AND rate_parser_id=:old"), {'old': OLD, 'new': NEW})
    op.execute('ALTER TABLE users VALIDATE CONSTRAINT ck_users_work_rate_source')
    op.execute('ALTER TABLE merchants VALIDATE CONSTRAINT ck_merchant_country_parser')
    # Copy the latest assignment as a new immutable event. Never update history.
    rows = bind.execute(sa.text("""SELECT DISTINCT ON (scope_key) * FROM parser_assignments
        WHERE country='TJ' AND parser_id=:old ORDER BY scope_key,version DESC"""), {'old': OLD}).mappings().all()
    for row in rows:
        bind.execute(sa.text("""INSERT INTO parser_assignments
            (id,scope_key,version,merchant_id,trader_id,country,parser_id,purpose,
             configuration_id,configuration_version,is_active,created_by,reason,created_at,updated_at)
            VALUES (:id,:scope,1,:merchant,:trader,'TJ',:parser,'financial',:config,1,false,:actor,
             'TJ_Wave2_provider_replacement_prepared',:now,:now)"""),
            {'id': uuid.uuid4(), 'scope': row['scope_key'].replace(OLD, NEW),
             'merchant': row['merchant_id'], 'trader': row['trader_id'], 'parser': NEW,
             'config': config_id, 'actor': row['created_by'], 'now': now})


def downgrade():
    # Used provider/version history is append-only; a forward fix is required.
    bind = op.get_bind()
    op.execute('LOCK TABLE users, merchants, country_tariff_plans, parser_assignments, parser_configurations, rate_observations IN ACCESS EXCLUSIVE MODE')
    for sql in (
        "SELECT count(*) FROM users WHERE working_country='TJ'",
        "SELECT count(*) FROM merchants WHERE working_country='TJ'",
        "SELECT count(*) FROM country_tariff_plans WHERE country='TJ'",
        "SELECT count(*) FROM parser_assignments WHERE parser_id=:parser",
        "SELECT count(*) FROM rate_observations WHERE configuration_id IN (SELECT id FROM parser_configurations WHERE parser_id=:parser)",
        "SELECT count(*) FROM parser_configurations WHERE parser_id=:parser AND (version<>1 OR is_active OR created_by IS NOT NULL OR parameters::jsonb<>jsonb_build_object('refresh_seconds',15,'ttl_seconds',60,'contract_confirmed',false))",
    ):
        if bind.scalar(sa.text(sql), {'parser': NEW}):
            raise RuntimeError('TJ cabinets/history exist; retain 0039 and use a forward-compatible rollback')
    op.execute('ALTER TABLE parser_configurations DISABLE TRIGGER parser_configurations_immutable')
    bind.execute(sa.text('DELETE FROM parser_configurations WHERE parser_id=:parser'), {'parser': NEW})
    op.execute('ALTER TABLE parser_configurations ENABLE TRIGGER parser_configurations_immutable')
    replace_check(bind, 'users', 'ck_users_work_rate_source', NEW, OLD)
    replace_check(bind, 'merchants', 'ck_merchant_country_parser', NEW, OLD)
    op.alter_column('users', 'working_rate_source', type_=sa.String(16), existing_type=sa.String(64))
