"""Country cabinets, pinned local tariffs and atomic USDT crossborder operations.

Existing RUB balances are preserved; unknown merchant jurisdictions remain unset.
Legacy appeals remain visible but are never automatically financially approved.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
import uuid

revision = '0032_crossborder_live'
down_revision = '0031_binance_merchant_rate'
branch_labels = None
depends_on = None

DDL = ['\nCREATE TABLE business_accounts (\n\tid UUID NOT NULL, \n\tname VARCHAR(160) NOT NULL, \n\tcreated_by UUID, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tupdated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tPRIMARY KEY (id), \n\tFOREIGN KEY(created_by) REFERENCES users (id) ON DELETE RESTRICT\n)\n\n', '\nCREATE TABLE parser_configurations (\n\tid UUID NOT NULL, \n\tparser_id VARCHAR(64) NOT NULL, \n\tversion INTEGER NOT NULL, \n\tcountry VARCHAR(2) NOT NULL, \n\tdirection VARCHAR(5), \n\tparameters JSONB NOT NULL, \n\tis_active BOOLEAN NOT NULL, \n\tcreated_by UUID, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tupdated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tPRIMARY KEY (id), \n\tCONSTRAINT uq_parser_version UNIQUE (parser_id, version), \n\tFOREIGN KEY(created_by) REFERENCES users (id) ON DELETE RESTRICT\n)\n\n', 'CREATE INDEX ix_parser_configurations_parser_id ON parser_configurations (parser_id)', '\nCREATE TABLE rate_observations (\n\tid UUID NOT NULL, \n\tconfiguration_id UUID NOT NULL, \n\tfingerprint VARCHAR(64) NOT NULL, \n\tsnapshot JSONB NOT NULL, \n\tfetched_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tfixed_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\texpires_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (id), \n\tCONSTRAINT uq_rate_observation UNIQUE (configuration_id, fingerprint), \n\tCONSTRAINT ck_rate_observation_expiry CHECK (expires_at > fetched_at), \n\tFOREIGN KEY(configuration_id) REFERENCES parser_configurations (id) ON DELETE RESTRICT\n)\n\n', 'CREATE INDEX ix_rate_observations_configuration_id ON rate_observations (configuration_id)', "\nCREATE TABLE country_tariff_plans (\n\tid UUID NOT NULL, \n\tscope_key VARCHAR(160) NOT NULL, \n\tversion INTEGER NOT NULL, \n\tparty VARCHAR(16) NOT NULL, \n\tcountry VARCHAR(2) NOT NULL, \n\tcurrency VARCHAR(3) NOT NULL, \n\tdirection VARCHAR(5) NOT NULL, \n\tentity_id UUID, \n\ttiers JSONB NOT NULL, \n\treference JSONB NOT NULL, \n\teffective_from TIMESTAMP WITH TIME ZONE NOT NULL, \n\tis_active BOOLEAN NOT NULL, \n\tcreated_by UUID, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tupdated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tPRIMARY KEY (id), \n\tCONSTRAINT uq_country_tariff_version UNIQUE (scope_key, version), \n\tCONSTRAINT ck_country_tariff_party CHECK (party IN ('merchant','trader')), \n\tCONSTRAINT ck_country_tariff_direction CHECK (direction = 'kz-ge'), \n\tCONSTRAINT ck_country_tariff_dimensions CHECK ((party='merchant' AND country='KZ' AND currency='KZT') OR (party='trader' AND country='GE' AND currency='GEL')), \n\tFOREIGN KEY(created_by) REFERENCES users (id) ON DELETE RESTRICT\n)\n\n", 'CREATE INDEX ix_country_tariff_plans_effective_from ON country_tariff_plans (effective_from)', 'CREATE INDEX ix_country_tariff_plans_entity_id ON country_tariff_plans (entity_id)', 'CREATE INDEX ix_country_tariff_plans_scope_key ON country_tariff_plans (scope_key)', "\nCREATE TABLE crossborder_operations (\n\tid UUID NOT NULL, \n\tmerchant_id UUID NOT NULL, \n\ttrader_id UUID NOT NULL, \n\trequisite_id UUID NOT NULL, \n\tquote_id UUID NOT NULL, \n\tmerchant_plan_id UUID NOT NULL, \n\ttrader_plan_id UUID NOT NULL, \n\texternal_id VARCHAR(128) NOT NULL, \n\tdirection VARCHAR(5) NOT NULL, \n\tstatus VARCHAR(16) NOT NULL, \n\tamount_kzt NUMERIC(18, 2) NOT NULL, \n\texpected_gel NUMERIC(18, 2), \n\tactual_gel NUMERIC(18, 2), \n\tbase_usdt NUMERIC(24, 8) NOT NULL, \n\tmerchant_usdt NUMERIC(24, 8) NOT NULL, \n\treserved_usdt NUMERIC(24, 8) NOT NULL, \n\ttrader_usdt NUMERIC(24, 8), \n\tmargin_usdt NUMERIC(24, 8), \n\texpenses_usdt NUMERIC(24, 8) NOT NULL, \n\tsnapshot JSONB NOT NULL, \n\treceived_at TIMESTAMP WITH TIME ZONE, \n\tconfirmed_at TIMESTAMP WITH TIME ZONE, \n\tconfirmation_source VARCHAR(32), \n\tconfirmation_reference VARCHAR(200), \n\tconfirmed_by UUID, \n\texpires_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tupdated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tPRIMARY KEY (id), \n\tCONSTRAINT uq_crossborder_external UNIQUE (merchant_id, external_id), \n\tCONSTRAINT uq_crossborder_operation_quote UNIQUE (quote_id), \n\tCONSTRAINT ck_crossborder_operation_direction CHECK (direction = 'kz-ge'), \n\tCONSTRAINT ck_crossborder_operation_status CHECK (status IN ('accepted','paid','cancelled','refunded')), \n\tCONSTRAINT ck_crossborder_operation_amounts CHECK (amount_kzt > 0 AND base_usdt > 0 AND reserved_usdt >= 0 AND merchant_usdt >= 0), \n\tCONSTRAINT ck_crossborder_operation_confirmation CHECK (status NOT IN ('paid','refunded') OR (actual_gel > 0 AND confirmed_at IS NOT NULL AND trader_usdt IS NOT NULL AND margin_usdt IS NOT NULL)), \n\tFOREIGN KEY(merchant_id) REFERENCES merchants (id) ON DELETE RESTRICT, \n\tFOREIGN KEY(trader_id) REFERENCES users (id) ON DELETE RESTRICT, \n\tFOREIGN KEY(requisite_id) REFERENCES requisites (id) ON DELETE RESTRICT, \n\tFOREIGN KEY(quote_id) REFERENCES crossborder_quotes (id) ON DELETE RESTRICT, \n\tFOREIGN KEY(merchant_plan_id) REFERENCES country_tariff_plans (id) ON DELETE RESTRICT, \n\tFOREIGN KEY(trader_plan_id) REFERENCES country_tariff_plans (id) ON DELETE RESTRICT, \n\tFOREIGN KEY(confirmed_by) REFERENCES users (id) ON DELETE RESTRICT\n)\n\n", 'CREATE INDEX ix_crossborder_operation_trader_status ON crossborder_operations (trader_id, status)', 'CREATE INDEX ix_crossborder_operations_expires_at ON crossborder_operations (expires_at)', 'CREATE INDEX ix_crossborder_operations_merchant_id ON crossborder_operations (merchant_id)', '\nCREATE TABLE crossborder_events (\n\tid UUID NOT NULL, \n\toperation_id UUID NOT NULL, \n\tevent_key VARCHAR(160) NOT NULL, \n\tpayload_hash VARCHAR(64) NOT NULL, \n\taction VARCHAR(32) NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tupdated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tPRIMARY KEY (id), \n\tFOREIGN KEY(operation_id) REFERENCES crossborder_operations (id) ON DELETE RESTRICT, \n\tUNIQUE (event_key)\n)\n\n', 'CREATE INDEX ix_crossborder_events_operation_id ON crossborder_events (operation_id)']

def upgrade():
    for statement in DDL:
        op.execute(statement)
    op.add_column('merchants', sa.Column('business_id', postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column('merchants', sa.Column('working_country', sa.String(2), nullable=True))
    op.add_column('merchants', sa.Column('rate_parser_id', sa.String(64), nullable=True))
    op.add_column('merchants', sa.Column('country_assigned_at', sa.DateTime(timezone=True), nullable=True))
    op.create_foreign_key('fk_merchant_business', 'merchants', 'business_accounts', ['business_id'], ['id'], ondelete='RESTRICT')
    op.create_unique_constraint('uq_merchant_business_country', 'merchants', ['business_id', 'working_country'])
    op.create_check_constraint('ck_merchant_country_parser', 'merchants', "(working_country IS NULL AND rate_parser_id IS NULL) OR (working_country='RU' AND rate_parser_id='rapira') OR (working_country='KZ' AND rate_parser_id='binance_p2p_kaspi_buy_100000_avg10') OR (working_country='GE' AND rate_parser_id='xe')")
    for table, fields in [('balances', ('available', 'frozen')), ('ledger_entries', ('amount',)), ('platform_ledger_entries', ('amount',))]:
        for field in fields:
            op.alter_column(table, field, type_=sa.Numeric(24, 8), existing_type=sa.Numeric(18, 2))
    op.add_column('appeals', sa.Column('evidence_validated', sa.Boolean(), nullable=False, server_default=sa.false()))
    op.create_index('uq_validated_active_appeal', 'appeals', ['operation_type', 'operation_id'], unique=True,
                    postgresql_where=sa.text("evidence_validated AND status IN ('opened','in_review')"))
    op.create_check_constraint('ck_appeal_validated_evidence', 'appeals', "NOT evidence_validated OR (operation_type='deposit' AND metadata_json::jsonb ?& ARRAY['receipt_file','trader_id','amount_claimed','requisite','recipient_bank','operation_date','validation_version'])")
    op.execute("INSERT INTO business_accounts(id,name,created_at,updated_at) SELECT id,name,now(),now() FROM merchants")
    op.execute("UPDATE merchants SET business_id=id")
    op.execute("""UPDATE merchants m SET working_country='RU', rate_parser_id='rapira', country_assigned_at=now()
        WHERE NOT EXISTS (SELECT 1 FROM merchant_crossborder_rate_settings s WHERE s.merchant_id=m.id)
        AND EXISTS (SELECT 1 FROM deposits d WHERE d.merchant_id=m.id AND d.currency='RUB'
                    UNION ALL SELECT 1 FROM payouts p WHERE p.merchant_id=m.id AND p.currency='RUB'
                    UNION ALL SELECT 1 FROM ledger_entries l WHERE l.merchant_id=m.id AND l.currency='RUB' AND l.amount<>0)
        AND NOT EXISTS (SELECT 1 FROM deposits d WHERE d.merchant_id=m.id AND d.currency<>'RUB'
                        UNION ALL SELECT 1 FROM payouts p WHERE p.merchant_id=m.id AND p.currency<>'RUB'
                        UNION ALL SELECT 1 FROM ledger_entries l WHERE l.merchant_id=m.id AND l.currency<>'RUB' AND l.amount<>0)""")
    op.execute("""UPDATE merchants m SET working_country='KZ', rate_parser_id='binance_p2p_kaspi_buy_100000_avg10', country_assigned_at=now()
        WHERE EXISTS (SELECT 1 FROM merchant_crossborder_rate_settings s WHERE s.merchant_id=m.id AND s.direction='kz-ge')
        AND NOT EXISTS (SELECT 1 FROM deposits d WHERE d.merchant_id=m.id
                        UNION ALL SELECT 1 FROM payouts p WHERE p.merchant_id=m.id
                        UNION ALL SELECT 1 FROM ledger_entries l WHERE l.merchant_id=m.id AND l.amount<>0)""")
    config = sa.table('parser_configurations', sa.column('id', postgresql.UUID(as_uuid=True)), sa.column('parser_id', sa.String()), sa.column('version', sa.Integer()), sa.column('country', sa.String()), sa.column('direction', sa.String()), sa.column('parameters', postgresql.JSONB()), sa.column('is_active', sa.Boolean()))
    op.bulk_insert(config, [dict(id=uuid.uuid5(uuid.NAMESPACE_URL, 'ozark:parser:'+parser), parser_id=parser, version=1, country=country, direction=direction,
        parameters={'refresh_seconds': 60, 'ttl_seconds': 60}, is_active=True)
        for parser,country,direction in [('rapira','RU',None),('binance_p2p_kaspi_buy_100000_avg10','KZ','kz-ge'),('xe','GE',None)]])
    for party,country,currency,percent in [('merchant','KZ','KZT','10.0000'),('trader','GE','GEL','7.0000')]:
        op.execute(sa.text("""INSERT INTO country_tariff_plans(id,scope_key,version,party,country,currency,direction,tiers,reference,effective_from,is_active)
            VALUES (:id,:scope,1,:party,:country,:currency,'kz-ge',CAST(:tiers AS jsonb),'{}'::jsonb,now(),true)""").bindparams(
            id=uuid.uuid5(uuid.NAMESPACE_URL,'ozark:tariff:'+party),scope=f'kz-ge:{party}:{country}:default',party=party,country=country,currency=currency,
            tiers='[{"min":"0.01","max":null,"percent":"'+percent+'"}]'))
    op.execute("""CREATE FUNCTION protect_merchant_country() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
        IF OLD.working_country IS NOT NULL AND (NEW.working_country IS DISTINCT FROM OLD.working_country OR NEW.business_id IS DISTINCT FROM OLD.business_id) THEN
            RAISE EXCEPTION 'merchant country and business are immutable'; END IF; RETURN NEW; END $$""")
    op.execute('CREATE TRIGGER merchant_country_immutable BEFORE UPDATE ON merchants FOR EACH ROW EXECUTE FUNCTION protect_merchant_country()')
    op.execute("""CREATE FUNCTION protect_crossborder_configuration() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
        RAISE EXCEPTION 'crossborder configuration and evidence are append-only'; END $$""")
    for table in ('parser_configurations','country_tariff_plans','rate_observations','crossborder_events'):
        op.execute(f'CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION protect_crossborder_configuration()')
    op.execute("""CREATE FUNCTION protect_crossborder_operation() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
        IF TG_OP='DELETE' THEN RAISE EXCEPTION 'crossborder operations cannot be deleted'; END IF;
        IF (to_jsonb(OLD) - ARRAY['status','actual_gel','received_at','confirmed_at','confirmation_source','confirmation_reference','confirmed_by','trader_usdt','margin_usdt','updated_at'])
           IS DISTINCT FROM (to_jsonb(NEW) - ARRAY['status','actual_gel','received_at','confirmed_at','confirmation_source','confirmation_reference','confirmed_by','trader_usdt','margin_usdt','updated_at']) THEN
            RAISE EXCEPTION 'accepted financial quote is immutable'; END IF;
        IF OLD.status IN ('cancelled','refunded') AND to_jsonb(OLD) IS DISTINCT FROM to_jsonb(NEW) THEN RAISE EXCEPTION 'terminal operation is immutable'; END IF;
        IF OLD.status='paid' AND (NEW.status NOT IN ('paid','refunded') OR (to_jsonb(OLD) - ARRAY['status','updated_at']) IS DISTINCT FROM (to_jsonb(NEW) - ARRAY['status','updated_at'])) THEN RAISE EXCEPTION 'paid financial result is immutable'; END IF;
        IF NEW.status IN ('paid','refunded') AND NEW.trader_usdt <> NEW.merchant_usdt + NEW.margin_usdt THEN RAISE EXCEPTION 'crossborder financial invariant'; END IF;
        RETURN NEW; END $$""")
    op.execute('CREATE TRIGGER crossborder_operation_immutable BEFORE UPDATE OR DELETE ON crossborder_operations FOR EACH ROW EXECUTE FUNCTION protect_crossborder_operation()')


def downgrade():
    bind = op.get_bind()
    for table in ('crossborder_operations','crossborder_events','rate_observations'):
        if bind.scalar(sa.text(f'SELECT count(*) FROM {table}')):
            raise RuntimeError('refusing to discard crossborder financial history; restore a verified backup for rollback')
    if bind.scalar(sa.text('SELECT count(*) FROM country_tariff_plans WHERE version>1')) or bind.scalar(sa.text('SELECT count(*) FROM parser_configurations WHERE version>1')):
        raise RuntimeError('refusing to discard configured tariff/parser history')
    if bind.scalar(sa.text('SELECT count(*) FROM appeals WHERE evidence_validated')):
        raise RuntimeError('refusing to discard validated appeal evidence')
    for table in ('balances','ledger_entries','platform_ledger_entries'):
        if bind.scalar(sa.text(f"SELECT count(*) FROM {table} WHERE currency='USDT'")):
            raise RuntimeError('refusing to discard USDT precision')
    op.execute('DROP TRIGGER crossborder_operation_immutable ON crossborder_operations')
    op.execute('DROP FUNCTION protect_crossborder_operation()')
    for table in ('crossborder_events','crossborder_operations','rate_observations','country_tariff_plans','parser_configurations'):
        op.drop_table(table)
    op.execute('DROP FUNCTION protect_crossborder_configuration()')
    op.execute('DROP TRIGGER merchant_country_immutable ON merchants')
    op.execute('DROP FUNCTION protect_merchant_country()')
    op.drop_constraint('ck_merchant_country_parser','merchants',type_='check')
    op.drop_constraint('uq_merchant_business_country','merchants',type_='unique')
    op.drop_constraint('fk_merchant_business','merchants',type_='foreignkey')
    for column in ('country_assigned_at','rate_parser_id','working_country','business_id'):
        op.drop_column('merchants',column)
    op.drop_table('business_accounts')
    op.drop_constraint('ck_appeal_validated_evidence','appeals',type_='check')
    op.drop_index('uq_validated_active_appeal',table_name='appeals')
    op.drop_column('appeals','evidence_validated')
    for table, fields in [('balances', ('available', 'frozen')), ('ledger_entries', ('amount',)), ('platform_ledger_entries', ('amount',))]:
        for field in fields:
            op.alter_column(table, field, type_=sa.Numeric(18, 2), existing_type=sa.Numeric(24, 8))
