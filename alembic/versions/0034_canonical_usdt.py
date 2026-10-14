"""Preserve legacy settlement; add canonical USDT tariffs and cabinet parsers."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg
import uuid

revision = '0034_canonical_usdt'
down_revision = '0033_ge_route_guards'
branch_labels = None
depends_on = None
GEL = 'binance_p2p_tbc_buy_500_avg10'


def upgrade():
    op.add_column('country_tariff_plans', sa.Column('basis', sa.String(16), nullable=False, server_default='local_amount'))
    op.alter_column('country_tariff_plans', 'currency', type_=sa.String(4), existing_type=sa.String(3))
    op.drop_constraint('ck_country_tariff_dimensions', 'country_tariff_plans', type_='check')
    op.create_check_constraint('ck_country_tariff_dimensions', 'country_tariff_plans',
        "(basis='base_usdt' AND currency='USDT' AND ((party='merchant' AND country='KZ') OR (party='trader' AND country='GE'))) OR (basis='local_amount' AND ((party='merchant' AND country='KZ' AND currency='KZT') OR (party='trader' AND country='GE' AND currency='GEL')))")
    # A constant, unbounded percentage is independent of FX. Finite/multi-tier
    # local plans are deliberately not guessed: resolve_plan requires approval.
    op.execute("""INSERT INTO country_tariff_plans(id,scope_key,version,party,country,currency,basis,direction,
        entity_id,tiers,reference,effective_from,is_active,created_by)
        SELECT gen_random_uuid(),scope_key||chr(58)||'base_usdt',version,party,country,'USDT','base_usdt',direction,
        entity_id,jsonb_build_array(jsonb_build_object('min','0.00000001','max',NULL,'percent',tiers->0->>'percent')),
        jsonb_build_object('source','constant_percentage_migration','legacy_plan_id',id,
                          'approval','No FX threshold conversion: one unbounded constant percentage'),
        effective_from,is_active,created_by FROM country_tariff_plans
        WHERE basis='local_amount' AND jsonb_array_length(tiers)=1 AND (tiers->0->>'min')::numeric=0.01
        AND tiers->0->>'max' IS NULL""")
    config = sa.table('parser_configurations', sa.column('id',pg.UUID()), sa.column('parser_id',sa.String()),
        sa.column('version',sa.Integer()),sa.column('country',sa.String()),sa.column('direction',sa.String()),
        sa.column('parameters',pg.JSONB()),sa.column('is_active',sa.Boolean()))
    op.bulk_insert(config,[dict(id=uuid.uuid5(uuid.NAMESPACE_URL,'ozark:parser:'+GEL),parser_id=GEL,
        version=1,country='GE',direction='kz-ge',parameters={'refresh_seconds':60,'ttl_seconds':60},is_active=True)])
    op.create_table('parser_assignments',
        sa.Column('id',pg.UUID(),primary_key=True),sa.Column('scope_key',sa.String(200),nullable=False),
        sa.Column('version',sa.Integer(),nullable=False),
        sa.Column('merchant_id',pg.UUID(),sa.ForeignKey('merchants.id',ondelete='RESTRICT')),
        sa.Column('trader_id',pg.UUID(),sa.ForeignKey('users.id',ondelete='RESTRICT')),
        sa.Column('country',sa.String(2),nullable=False),sa.Column('parser_id',sa.String(64),nullable=False),
        sa.Column('purpose',sa.String(16),nullable=False),
        sa.Column('configuration_id',pg.UUID(),sa.ForeignKey('parser_configurations.id',ondelete='RESTRICT'),nullable=False),
        sa.Column('configuration_version',sa.Integer(),nullable=False),sa.Column('is_active',sa.Boolean(),nullable=False),
        sa.Column('created_by',pg.UUID(),sa.ForeignKey('users.id',ondelete='RESTRICT')),
        sa.Column('reason',sa.String(200),nullable=False),
        sa.Column('created_at',sa.DateTime(timezone=True),server_default=sa.func.now(),nullable=False),
        sa.Column('updated_at',sa.DateTime(timezone=True),server_default=sa.func.now(),nullable=False),
        sa.UniqueConstraint('scope_key','version',name='uq_parser_assignment_version'),
        sa.CheckConstraint('(merchant_id IS NULL) <> (trader_id IS NULL)',name='ck_parser_assignment_owner'),
        sa.CheckConstraint("purpose IN ('financial','informational')",name='ck_parser_assignment_purpose'))
    for col in ('scope_key','merchant_id','trader_id'):
        op.create_index('ix_parser_assignments_'+col,'parser_assignments',[col])
    op.execute("""WITH configs AS (SELECT DISTINCT ON(parser_id) * FROM parser_configurations ORDER BY parser_id,version DESC)
        INSERT INTO parser_assignments(id,scope_key,version,merchant_id,country,parser_id,purpose,
            configuration_id,configuration_version,is_active,reason)
        SELECT gen_random_uuid(),'merchant:'||m.id||':'||CASE WHEN m.working_country='GE' THEN 'informational' ELSE 'financial' END||':'||c.parser_id,
            1,m.id,m.working_country,c.parser_id,CASE WHEN m.working_country='GE' THEN 'informational' ELSE 'financial' END,
            c.id,c.version,c.is_active,'0034_existing_primary_parser' FROM merchants m JOIN configs c ON m.rate_parser_id=c.parser_id""")
    op.execute(sa.text("""INSERT INTO parser_assignments(id,scope_key,version,trader_id,country,parser_id,purpose,
            configuration_id,configuration_version,is_active,reason)
        SELECT gen_random_uuid(),'trader:'||u.id||chr(58)||'informational'||chr(58)||c.parser_id,1,u.id,'GE',c.parser_id,'informational',
            c.id,c.version,true,'0034_GE_informational_default' FROM users u CROSS JOIN parser_configurations c
        WHERE u.role='trader' AND u.working_country='GE' AND c.parser_id=:parser AND c.version=1""").bindparams(parser=GEL))
    op.execute('CREATE TRIGGER parser_assignments_immutable BEFORE UPDATE OR DELETE ON parser_assignments FOR EACH ROW EXECUTE FUNCTION protect_crossborder_configuration()')


def downgrade():
    bind = op.get_bind()
    guards = [
        'SELECT count(*) FROM parser_assignments',
        "SELECT count(*) FROM crossborder_quotes WHERE (snapshot->>'format_version')::int >= 4",
        "SELECT count(*) FROM country_tariff_plans WHERE basis='base_usdt' AND reference->>'source' IS DISTINCT FROM 'constant_percentage_migration'",
        "SELECT count(*) FROM parser_configurations WHERE parser_id='binance_p2p_tbc_buy_500_avg10' AND version>1",
        "SELECT count(*) FROM rate_observations o JOIN parser_configurations c ON c.id=o.configuration_id WHERE c.parser_id='binance_p2p_tbc_buy_500_avg10'",
    ]
    if any(bind.scalar(sa.text(query)) for query in guards):
        raise RuntimeError('refusing to discard financial/configuration history; restore a verified pre-0034 backup for rollback')
    # Empty installations can roll back; only unused migration seeds are removed.
    op.drop_table('parser_assignments')
    for table, predicate in [('country_tariff_plans', "basis='base_usdt'"),
                             ('parser_configurations', "parser_id='binance_p2p_tbc_buy_500_avg10'")]:
        op.execute(f'DROP TRIGGER {table}_immutable ON {table}')
        op.execute(f'DELETE FROM {table} WHERE {predicate}')
        op.execute(f'CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION protect_crossborder_configuration()')
    op.drop_constraint('ck_country_tariff_dimensions', 'country_tariff_plans', type_='check')
    op.create_check_constraint('ck_country_tariff_dimensions', 'country_tariff_plans',
        "(party='merchant' AND country='KZ' AND currency='KZT') OR (party='trader' AND country='GE' AND currency='GEL')")
    op.drop_column('country_tariff_plans', 'basis')
    op.alter_column('country_tariff_plans', 'currency', type_=sa.String(3), existing_type=sa.String(4))
