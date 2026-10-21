"""Схема из ТЗ: docs/schema.sql целиком (70 таблиц).

Сгенерирована из forecast/db/schema.py в его исходном виде (один в один со
schema.sql, проверено сравнением на PostgreSQL 16). Умолчания записаны
переносимыми конструкциями (sa.func.now(), sa.false()), чтобы миграция шла и на
PostgreSQL, и на SQLite. Отличия Фазы 1 — в следующей миграции, 0002.

Revision ID: 0001
Revises:
Create Date: 2026-10-21

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0001'
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('buyer_profile_reference',
    sa.Column('segment_type', sa.Text(), nullable=False),
    sa.Column('segment_value', sa.Text(), nullable=False),
    sa.Column('share_of_buyers_pct', sa.Numeric(precision=6, scale=3), nullable=False),
    sa.Column('computed_at', sa.DateTime(), server_default=sa.func.now(), nullable=True),
    sa.PrimaryKeyConstraint('segment_type', 'segment_value', name='buyer_profile_reference_pkey')
    )
    op.create_table('category_market_trends',
    sa.Column('category', sa.Text(), nullable=False),
    sa.Column('trend_date', sa.Date(), nullable=False),
    sa.Column('market_growth_index', sa.Numeric(precision=6, scale=3), nullable=True),
    sa.Column('competitor_count', sa.Integer(), nullable=True),
    sa.Column('avg_category_price', sa.Numeric(precision=10, scale=2), nullable=True),
    sa.Column('source', sa.Text(), nullable=True),
    sa.PrimaryKeyConstraint('category', 'trend_date', name='category_market_trends_pkey')
    )
    op.create_table('channel_commission_rates',
    sa.Column('channel', sa.Text(), nullable=False),
    sa.Column('category', sa.Text(), nullable=False),
    sa.Column('commission_pct', sa.Numeric(precision=6, scale=3), nullable=True),
    sa.Column('logistics_fee', sa.Numeric(precision=10, scale=2), nullable=True),
    sa.Column('effective_from', sa.Date(), nullable=False),
    sa.Column('effective_to', sa.Date(), nullable=True),
    sa.PrimaryKeyConstraint('channel', 'category', 'effective_from', name='channel_commission_rates_pkey')
    )
    op.create_table('channel_ratio_calibration',
    sa.Column('channel', sa.Text(), nullable=False),
    sa.Column('category', sa.Text(), nullable=False),
    sa.Column('ratio', sa.Numeric(precision=6, scale=4), nullable=False),
    sa.Column('sample_size', sa.Integer(), nullable=True),
    sa.Column('computed_at', sa.DateTime(), server_default=sa.func.now(), nullable=True),
    sa.PrimaryKeyConstraint('channel', 'category', name='channel_ratio_calibration_pkey')
    )
    op.create_table('color_profile',
    sa.Column('category', sa.Text(), nullable=False),
    sa.Column('color_group', sa.Text(), nullable=False),
    sa.Column('share_of_sales_pct', sa.Numeric(precision=6, scale=3), nullable=False),
    sa.Column('report_date', sa.Date(), nullable=False),
    sa.PrimaryKeyConstraint('category', 'color_group', 'report_date', name='color_profile_pkey')
    )
    op.create_table('external_channel_sales_history',
    sa.Column('channel', sa.Text(), nullable=False),
    sa.Column('barcode', sa.Text(), nullable=False),
    sa.Column('month', sa.Date(), nullable=False),
    sa.Column('qty_sold', sa.Numeric(precision=10, scale=2), nullable=True),
    sa.Column('revenue', sa.Numeric(precision=14, scale=2), nullable=True),
    sa.Column('loaded_at', sa.DateTime(), server_default=sa.func.now(), nullable=True),
    sa.PrimaryKeyConstraint('channel', 'barcode', 'month', name='external_channel_sales_history_pkey')
    )
    op.create_table('forecast_correction_settings',
    sa.Column('category', sa.Text(), nullable=False),
    sa.Column('recovery_tail_days', sa.Integer(), server_default=sa.text('7'), nullable=False),
    sa.Column('recovery_tail_coefficient', sa.Numeric(precision=3, scale=2), server_default=sa.text('0.5'), nullable=False),
    sa.Column('outlier_sigma_threshold', sa.Numeric(precision=3, scale=1), server_default=sa.text('3.0'), nullable=False),
    sa.PrimaryKeyConstraint('category', name='forecast_correction_settings_pkey')
    )
    op.create_table('forecast_deviation_settings',
    sa.Column('category', sa.Text(), nullable=False),
    sa.Column('mape_warning_threshold', sa.Numeric(precision=5, scale=2), server_default=sa.text('20'), nullable=False),
    sa.Column('mape_critical_threshold', sa.Numeric(precision=5, scale=2), server_default=sa.text('40'), nullable=False),
    sa.Column('consecutive_periods_trigger', sa.Integer(), server_default=sa.text('3'), nullable=False),
    sa.PrimaryKeyConstraint('category', name='forecast_deviation_settings_pkey')
    )
    op.create_table('google_trends_data',
    sa.Column('keyword', sa.Text(), nullable=False),
    sa.Column('trend_date', sa.Date(), nullable=False),
    sa.Column('region', sa.Text(), nullable=False),
    sa.Column('popularity_index', sa.Numeric(precision=5, scale=2), nullable=True),
    sa.PrimaryKeyConstraint('keyword', 'trend_date', 'region', name='google_trends_data_pkey')
    )
    op.create_table('lamoda_official_reconciliation',
    sa.Column('period_month', sa.Date(), nullable=False),
    sa.Column('official_price_realized_incl_vat', sa.Numeric(precision=14, scale=2), nullable=True),
    sa.Column('our_computed_sum', sa.Numeric(precision=14, scale=2), nullable=True),
    sa.Column('discrepancy_pct', sa.Numeric(precision=6, scale=2), nullable=True),
    sa.Column('reconciled_at', sa.DateTime(), server_default=sa.func.now(), nullable=True),
    sa.PrimaryKeyConstraint('period_month', name='lamoda_official_reconciliation_pkey')
    )
    op.create_table('lead_time_stats',
    sa.Column('scope_type', sa.Text(), nullable=False),
    sa.Column('scope_id', sa.Text(), nullable=False),
    sa.Column('lead_type', sa.Text(), nullable=False),
    sa.Column('median_days', sa.Numeric(precision=6, scale=2), nullable=False),
    sa.Column('std_days', sa.Numeric(precision=6, scale=2), nullable=True),
    sa.Column('computed_at', sa.DateTime(), server_default=sa.func.now(), nullable=True),
    sa.PrimaryKeyConstraint('scope_type', 'scope_id', 'lead_type', name='lead_time_stats_pkey')
    )
    op.create_table('margin_settings',
    sa.Column('setting_id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('effective_from', sa.Date(), nullable=False),
    sa.Column('lamoda_commission_rate', sa.Numeric(precision=5, scale=4), server_default=sa.text('0.42'), nullable=False),
    sa.Column('tax_regime', sa.Text(), server_default=sa.text("'УСН доходы'"), nullable=False),
    sa.Column('vat_rate', sa.Numeric(precision=5, scale=4), server_default=sa.text('0.05'), nullable=False),
    sa.Column('usn_rate', sa.Numeric(precision=5, scale=4), server_default=sa.text('0.06'), nullable=False),
    sa.Column('margin_min_pct', sa.Numeric(precision=5, scale=2), server_default=sa.text('30'), nullable=False),
    sa.Column('margin_target_pct', sa.Numeric(precision=5, scale=2), server_default=sa.text('50'), nullable=False),
    sa.Column('margin_max_pct', sa.Numeric(precision=5, scale=2), server_default=sa.text('80'), nullable=False),
    sa.Column('usd_rate_adjustment', sa.Numeric(precision=6, scale=2), server_default=sa.text('4'), nullable=False),
    sa.Column('commission_vat_multiplier', sa.Numeric(precision=5, scale=4), server_default=sa.text('1'), nullable=False),
    sa.Column('liquidation_recovery_rate', sa.Numeric(precision=4, scale=3), server_default=sa.text('0.5'), nullable=False),
    sa.PrimaryKeyConstraint('setting_id', name='margin_settings_pkey')
    )
    op.create_table('price_elasticity',
    sa.Column('scope_type', sa.Text(), nullable=False),
    sa.Column('scope_id', sa.Text(), nullable=False),
    sa.Column('method', sa.Text(), server_default=sa.text("'log_log_regression'"), nullable=True),
    sa.Column('elasticity_coefficient', sa.Numeric(precision=6, scale=4), nullable=False),
    sa.Column('r_squared', sa.Numeric(precision=4, scale=3), nullable=True),
    sa.Column('sample_size', sa.Integer(), nullable=True),
    sa.Column('computed_at', sa.DateTime(), server_default=sa.func.now(), nullable=True),
    sa.PrimaryKeyConstraint('scope_type', 'scope_id', name='price_elasticity_pkey')
    )
    op.create_table('price_optimization_run',
    sa.Column('run_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
    sa.Column('run_date', sa.Date(), nullable=False),
    sa.Column('scope', sa.Text(), nullable=True),
    sa.Column('objective_value_before', sa.Numeric(precision=14, scale=2), nullable=True),
    sa.Column('objective_value_after', sa.Numeric(precision=14, scale=2), nullable=True),
    sa.Column('status', sa.Text(), server_default=sa.text("'completed'"), nullable=True),
    sa.Column('created_at', sa.DateTime(), server_default=sa.func.now(), nullable=True),
    sa.PrimaryKeyConstraint('run_id', name='price_optimization_run_pkey')
    )
    op.create_table('promo_calendar',
    sa.Column('promo_id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('name', sa.Text(), nullable=True),
    sa.Column('date_from', sa.Date(), nullable=False),
    sa.Column('date_to', sa.Date(), nullable=False),
    sa.PrimaryKeyConstraint('promo_id', name='promo_calendar_pkey')
    )
    op.create_table('promo_report_daily',
    sa.Column('report_date', sa.Date(), nullable=False),
    sa.Column('orders_sum_promo', sa.Numeric(precision=12, scale=2), nullable=True),
    sa.Column('orders_sum_all', sa.Numeric(precision=12, scale=2), nullable=True),
    sa.Column('orders_share_promo_pct', sa.Numeric(precision=6, scale=2), nullable=True),
    sa.Column('redemption_sum_promo', sa.Numeric(precision=12, scale=2), nullable=True),
    sa.Column('redemption_sum_all', sa.Numeric(precision=12, scale=2), nullable=True),
    sa.Column('redemption_share_promo_pct', sa.Numeric(precision=6, scale=2), nullable=True),
    sa.PrimaryKeyConstraint('report_date', name='promo_report_daily_pkey')
    )
    op.create_table('seasonal_coefficients',
    sa.Column('seasonal_group', sa.Text(), nullable=False),
    sa.Column('month', sa.Integer(), nullable=False),
    sa.Column('coefficient', sa.Numeric(precision=6, scale=3), nullable=False),
    sa.Column('computed_at', sa.DateTime(), server_default=sa.func.now(), nullable=True),
    sa.CheckConstraint('month >= 1 AND month <= 12', name='seasonal_coefficients_month_check'),
    sa.PrimaryKeyConstraint('seasonal_group', 'month', name='seasonal_coefficients_pkey')
    )
    op.create_table('seasonal_index_reference',
    sa.Column('seasonal_group', sa.Text(), nullable=False),
    sa.Column('month_number', sa.Integer(), nullable=False),
    sa.Column('index_value', sa.Numeric(precision=6, scale=4), nullable=False),
    sa.Column('source_channels', sa.Text(), nullable=True),
    sa.Column('computed_at', sa.DateTime(), server_default=sa.func.now(), nullable=True),
    sa.PrimaryKeyConstraint('seasonal_group', 'month_number', name='seasonal_index_reference_pkey')
    )
    op.create_table('size_profile',
    sa.Column('category', sa.Text(), nullable=False),
    sa.Column('gender', sa.Text(), nullable=False),
    sa.Column('size_label', sa.Text(), nullable=False),
    sa.Column('share_of_sales_pct', sa.Numeric(precision=6, scale=3), nullable=False),
    sa.Column('report_date', sa.Date(), nullable=False),
    sa.PrimaryKeyConstraint('category', 'gender', 'size_label', 'report_date', name='size_profile_pkey')
    )
    op.create_table('suppliers',
    sa.Column('supplier_id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('name', sa.Text(), nullable=False),
    sa.Column('production_days', sa.Integer(), nullable=False),
    sa.Column('delivery_days', sa.Integer(), nullable=False),
    sa.Column('moq', sa.Integer(), nullable=False),
    sa.Column('on_time_delivery_rate_pct', sa.Numeric(precision=5, scale=2), nullable=True),
    sa.PrimaryKeyConstraint('supplier_id', name='suppliers_pkey')
    )
    op.create_table('usd_rate',
    sa.Column('rate_date', sa.Date(), nullable=False),
    sa.Column('cbr_rate', sa.Numeric(precision=8, scale=4), nullable=False),
    sa.Column('adjusted_rate', sa.Numeric(precision=8, scale=4), nullable=False),
    sa.PrimaryKeyConstraint('rate_date', name='usd_rate_pkey')
    )
    op.create_table('weather_data',
    sa.Column('region', sa.Text(), nullable=False),
    sa.Column('weather_date', sa.Date(), nullable=False),
    sa.Column('avg_temp_c', sa.Numeric(precision=4, scale=1), nullable=True),
    sa.PrimaryKeyConstraint('region', 'weather_date', name='weather_data_pkey')
    )
    op.create_table('webhook_events_log',
    sa.Column('event_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
    sa.Column('notification_type', sa.Text(), nullable=False),
    sa.Column('tracking_id', sa.Text(), nullable=True),
    sa.Column('sequence_number', sa.BigInteger(), nullable=True),
    sa.Column('payload', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('confirmed_via_get', sa.Boolean(), server_default=sa.false(), nullable=True),
    sa.Column('gap_resend_requested', sa.Boolean(), server_default=sa.false(), nullable=True),
    sa.Column('processed', sa.Boolean(), server_default=sa.false(), nullable=True),
    sa.Column('received_at', sa.DateTime(), server_default=sa.func.now(), nullable=True),
    sa.PrimaryKeyConstraint('event_id', name='webhook_events_log_pkey')
    )
    op.create_table('wordstat_data',
    sa.Column('keyword', sa.Text(), nullable=False),
    sa.Column('stat_date', sa.Date(), nullable=False),
    sa.Column('region', sa.Text(), nullable=False),
    sa.Column('frequency', sa.Integer(), nullable=True),
    sa.PrimaryKeyConstraint('keyword', 'stat_date', 'region', name='wordstat_data_pkey')
    )
    op.create_table('product_models',
    sa.Column('product_model_id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('article', sa.Text(), nullable=False),
    sa.Column('color', sa.Text(), nullable=True),
    sa.Column('category', sa.Text(), nullable=False),
    sa.Column('subcategory', sa.Text(), nullable=True),
    sa.Column('season', sa.Text(), nullable=True),
    sa.Column('supplier_id', sa.Integer(), nullable=True),
    sa.Column('collection_type', sa.Text(), server_default=sa.text("'base'"), nullable=True),
    sa.Column('first_sale_date', sa.Date(), nullable=True),
    sa.Column('status', sa.Text(), server_default=sa.text("'active'"), nullable=True),
    sa.Column('available_to_reorder', sa.Boolean(), server_default=sa.true(), nullable=True),
    sa.Column('discontinued_date', sa.Date(), nullable=True),
    sa.Column('vat_rate', sa.Numeric(precision=5, scale=2), nullable=True),
    sa.Column('lamoda_sku', sa.Text(), nullable=True),
    sa.Column('wb_sku', sa.Text(), nullable=True),
    sa.Column('ozon_sku', sa.Text(), nullable=True),
    sa.Column('planned_launch_price', sa.Numeric(precision=10, scale=2), nullable=True),
    sa.Column('seasonal_group', sa.Text(), nullable=True),
    sa.Column('color_group', sa.Text(), nullable=True),
    sa.ForeignKeyConstraint(['supplier_id'], ['suppliers.supplier_id'], name='product_models_supplier_id_fkey'),
    sa.PrimaryKeyConstraint('product_model_id', name='product_models_pkey'),
    sa.UniqueConstraint('article', 'color', name='product_models_article_color_key')
    )
    op.create_table('supplier_deliveries',
    sa.Column('delivery_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
    sa.Column('supplier_id', sa.Integer(), nullable=False),
    sa.Column('ordered_date', sa.Date(), nullable=False),
    sa.Column('expected_ready_date', sa.Date(), nullable=False),
    sa.Column('actual_ready_date', sa.Date(), nullable=True),
    sa.Column('on_time', sa.Boolean(), nullable=True),
    sa.ForeignKeyConstraint(['supplier_id'], ['suppliers.supplier_id'], name='supplier_deliveries_supplier_id_fkey'),
    sa.PrimaryKeyConstraint('delivery_id', name='supplier_deliveries_pkey')
    )
    op.create_table('abc_xyz_classification',
    sa.Column('product_model_id', sa.Integer(), nullable=False),
    sa.Column('period_start', sa.Date(), nullable=False),
    sa.Column('combined_score', sa.Numeric(precision=10, scale=4), nullable=True),
    sa.Column('abc_class', sa.Text(), nullable=False),
    sa.Column('coefficient_variation', sa.Numeric(precision=6, scale=3), nullable=True),
    sa.Column('xyz_class', sa.Text(), nullable=False),
    sa.Column('combined_segment', sa.Text(), nullable=False),
    sa.Column('service_level_z', sa.Numeric(precision=4, scale=3), nullable=False),
    sa.Column('review_frequency', sa.Text(), nullable=True),
    sa.Column('review_interval_days', sa.Integer(), nullable=True),
    sa.ForeignKeyConstraint(['product_model_id'], ['product_models.product_model_id'], name='abc_xyz_classification_product_model_id_fkey'),
    sa.PrimaryKeyConstraint('product_model_id', 'period_start', name='abc_xyz_classification_pkey')
    )
    op.create_table('audience_quality_score',
    sa.Column('product_model_id', sa.Integer(), nullable=False),
    sa.Column('score_date', sa.Date(), nullable=False),
    sa.Column('quality_score', sa.Numeric(precision=4, scale=3), nullable=False),
    sa.Column('computed_at', sa.DateTime(), server_default=sa.func.now(), nullable=True),
    sa.ForeignKeyConstraint(['product_model_id'], ['product_models.product_model_id'], name='audience_quality_score_product_model_id_fkey'),
    sa.PrimaryKeyConstraint('product_model_id', 'score_date', name='audience_quality_score_pkey')
    )
    op.create_table('funnel_model',
    sa.Column('product_model_id', sa.Integer(), nullable=False),
    sa.Column('period_start', sa.Date(), nullable=False),
    sa.Column('period_end', sa.Date(), nullable=False),
    sa.Column('views', sa.Integer(), nullable=True),
    sa.Column('cart_conversion_pct', sa.Numeric(precision=6, scale=3), nullable=True),
    sa.Column('cart_adds', sa.Integer(), nullable=True),
    sa.Column('order_share_pct', sa.Numeric(precision=6, scale=3), nullable=True),
    sa.Column('orders_qty', sa.Integer(), nullable=True),
    sa.Column('purchased_qty', sa.Integer(), nullable=True),
    sa.Column('purchase_rate_pct', sa.Numeric(precision=6, scale=3), nullable=True),
    sa.ForeignKeyConstraint(['product_model_id'], ['product_models.product_model_id'], name='funnel_model_product_model_id_fkey'),
    sa.PrimaryKeyConstraint('product_model_id', 'period_start', 'period_end', name='funnel_model_pkey')
    )
    op.create_table('metrika_audience_segments',
    sa.Column('product_model_id', sa.Integer(), nullable=False),
    sa.Column('segment_date', sa.Date(), nullable=False),
    sa.Column('segment_type', sa.Text(), nullable=False),
    sa.Column('segment_value', sa.Text(), nullable=False),
    sa.Column('share_of_clicks_pct', sa.Numeric(precision=6, scale=3), nullable=False),
    sa.ForeignKeyConstraint(['product_model_id'], ['product_models.product_model_id'], name='metrika_audience_segments_product_model_id_fkey'),
    sa.PrimaryKeyConstraint('product_model_id', 'segment_date', 'segment_type', 'segment_value', name='metrika_audience_segments_pkey')
    )
    op.create_table('new_item_forecast',
    sa.Column('product_model_id', sa.Integer(), nullable=False),
    sa.Column('computed_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
    sa.Column('analog_based_forecast_qty', sa.Numeric(precision=10, scale=2), nullable=True),
    sa.Column('category_correction_median', sa.Numeric(precision=6, scale=4), nullable=True),
    sa.Column('category_correction_std', sa.Numeric(precision=6, scale=4), nullable=True),
    sa.Column('wordstat_coef', sa.Numeric(precision=6, scale=4), nullable=True),
    sa.Column('final_forecast_qty', sa.Numeric(precision=10, scale=2), nullable=True),
    sa.Column('deepar_forecast_qty', sa.Numeric(precision=10, scale=2), nullable=True),
    sa.Column('coverage_period_days', sa.Integer(), nullable=True),
    sa.Column('underage_cost_cu', sa.Numeric(precision=10, scale=2), nullable=True),
    sa.Column('overage_cost_co', sa.Numeric(precision=10, scale=2), nullable=True),
    sa.Column('critical_fractile', sa.Numeric(precision=5, scale=4), nullable=True),
    sa.Column('z_newsvendor', sa.Numeric(precision=5, scale=3), nullable=True),
    sa.Column('recommended_first_shipment_qty', sa.Numeric(precision=10, scale=2), nullable=True),
    sa.Column('moq_adjusted_qty', sa.Numeric(precision=10, scale=2), nullable=True),
    sa.Column('risk_flag', sa.Boolean(), server_default=sa.false(), nullable=True),
    sa.ForeignKeyConstraint(['product_model_id'], ['product_models.product_model_id'], name='new_item_forecast_product_model_id_fkey'),
    sa.PrimaryKeyConstraint('product_model_id', 'computed_at', name='new_item_forecast_pkey')
    )
    op.create_table('product_analogs',
    sa.Column('product_model_id_new', sa.Integer(), nullable=False),
    sa.Column('product_model_id_analog', sa.Integer(), nullable=False),
    sa.Column('similarity_weight', sa.Numeric(precision=4, scale=3), nullable=False),
    sa.ForeignKeyConstraint(['product_model_id_analog'], ['product_models.product_model_id'], name='product_analogs_product_model_id_analog_fkey'),
    sa.ForeignKeyConstraint(['product_model_id_new'], ['product_models.product_model_id'], name='product_analogs_product_model_id_new_fkey'),
    sa.PrimaryKeyConstraint('product_model_id_new', 'product_model_id_analog', name='product_analogs_pkey')
    )
    op.create_table('product_sizes',
    sa.Column('product_size_id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('product_model_id', sa.Integer(), nullable=False),
    sa.Column('size_label', sa.Text(), nullable=False),
    sa.Column('lamoda_sku', sa.Text(), nullable=True),
    sa.ForeignKeyConstraint(['product_model_id'], ['product_models.product_model_id'], name='product_sizes_product_model_id_fkey'),
    sa.PrimaryKeyConstraint('product_size_id', name='product_sizes_pkey'),
    sa.UniqueConstraint('product_model_id', 'size_label', name='product_sizes_product_model_id_size_label_key')
    )
    op.create_table('promo_cannibalization_pairs',
    sa.Column('product_model_id_a', sa.Integer(), nullable=False),
    sa.Column('product_model_id_b', sa.Integer(), nullable=False),
    sa.Column('mean_sales_a_no_promo_b', sa.Numeric(precision=10, scale=2), nullable=True),
    sa.Column('mean_sales_a_promo_b', sa.Numeric(precision=10, scale=2), nullable=True),
    sa.Column('demand_shift_d', sa.Numeric(precision=10, scale=2), nullable=True),
    sa.Column('p_value', sa.Numeric(precision=6, scale=5), nullable=True),
    sa.Column('significant', sa.Boolean(), nullable=True),
    sa.Column('computed_at', sa.DateTime(), server_default=sa.func.now(), nullable=True),
    sa.ForeignKeyConstraint(['product_model_id_a'], ['product_models.product_model_id'], name='promo_cannibalization_pairs_product_model_id_a_fkey'),
    sa.ForeignKeyConstraint(['product_model_id_b'], ['product_models.product_model_id'], name='promo_cannibalization_pairs_product_model_id_b_fkey'),
    sa.PrimaryKeyConstraint('product_model_id_a', 'product_model_id_b', name='promo_cannibalization_pairs_pkey')
    )
    op.create_table('style_tags',
    sa.Column('style_tag_id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('product_model_id', sa.Integer(), nullable=True),
    sa.Column('tag', sa.Text(), nullable=False),
    sa.Column('confidence', sa.Numeric(precision=4, scale=3), nullable=True),
    sa.Column('source', sa.Text(), server_default=sa.text("'agent'"), nullable=True),
    sa.Column('reviewed', sa.Boolean(), server_default=sa.false(), nullable=True),
    sa.Column('created_at', sa.DateTime(), server_default=sa.func.now(), nullable=True),
    sa.ForeignKeyConstraint(['product_model_id'], ['product_models.product_model_id'], name='style_tags_product_model_id_fkey'),
    sa.PrimaryKeyConstraint('style_tag_id', name='style_tags_pkey')
    )
    op.create_table('underperformance_flags',
    sa.Column('product_model_id', sa.Integer(), nullable=False),
    sa.Column('period_start', sa.Date(), nullable=False),
    sa.Column('period_end', sa.Date(), nullable=False),
    sa.Column('expected_sales_qty', sa.Numeric(precision=10, scale=2), nullable=True),
    sa.Column('actual_sales_qty', sa.Numeric(precision=10, scale=2), nullable=True),
    sa.Column('shortfall_pct', sa.Numeric(precision=6, scale=2), nullable=True),
    sa.Column('in_stock_full_period', sa.Boolean(), nullable=True),
    sa.Column('attributes_incomplete', sa.Boolean(), nullable=True),
    sa.Column('popular_sizes_out_of_stock', sa.Boolean(), nullable=True),
    sa.Column('price_gap_vs_category_pct', sa.Numeric(precision=6, scale=2), nullable=True),
    sa.Column('price_gap_vs_source_channel_pct', sa.Numeric(precision=6, scale=2), nullable=True),
    sa.Column('card_status_issue', sa.Boolean(), nullable=True),
    sa.Column('competitors_in_promo_we_are_not', sa.Boolean(), nullable=True),
    sa.Column('flagged', sa.Boolean(), server_default=sa.false(), nullable=True),
    sa.Column('reviewed_at', sa.DateTime(), nullable=True),
    sa.Column('review_outcome', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(), server_default=sa.func.now(), nullable=True),
    sa.ForeignKeyConstraint(['product_model_id'], ['product_models.product_model_id'], name='underperformance_flags_product_model_id_fkey'),
    sa.PrimaryKeyConstraint('product_model_id', 'period_start', 'period_end', name='underperformance_flags_pkey')
    )
    op.create_table('yandex_direct_ad_mapping',
    sa.Column('ad_id', sa.Text(), nullable=False),
    sa.Column('campaign_id', sa.Text(), nullable=True),
    sa.Column('product_model_id', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(), server_default=sa.func.now(), nullable=True),
    sa.ForeignKeyConstraint(['product_model_id'], ['product_models.product_model_id'], name='yandex_direct_ad_mapping_product_model_id_fkey'),
    sa.PrimaryKeyConstraint('ad_id', name='yandex_direct_ad_mapping_pkey')
    )
    op.create_table('anomaly_triage_log',
    sa.Column('triage_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
    sa.Column('product_size_id', sa.Integer(), nullable=True),
    sa.Column('period_start', sa.Date(), nullable=True),
    sa.Column('period_end', sa.Date(), nullable=True),
    sa.Column('signals', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=True),
    sa.Column('probable_cause', sa.Text(), nullable=True),
    sa.Column('recommended_action', sa.Text(), nullable=True),
    sa.Column('confidence', sa.Numeric(precision=4, scale=3), nullable=True),
    sa.Column('manager_decision', sa.Text(), nullable=True),
    sa.Column('decided_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['product_size_id'], ['product_sizes.product_size_id'], name='anomaly_triage_log_product_size_id_fkey'),
    sa.PrimaryKeyConstraint('triage_id', name='anomaly_triage_log_pkey')
    )
    op.create_table('barcode_pool',
    sa.Column('barcode_id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('product_size_id', sa.Integer(), nullable=False),
    sa.Column('barcode', sa.Text(), nullable=False),
    sa.Column('seller_sku', sa.Text(), nullable=True),
    sa.Column('valid_from', sa.Date(), server_default=sa.text('CURRENT_DATE'), nullable=True),
    sa.Column('valid_to', sa.Date(), nullable=True),
    sa.ForeignKeyConstraint(['product_size_id'], ['product_sizes.product_size_id'], name='barcode_pool_product_size_id_fkey'),
    sa.PrimaryKeyConstraint('barcode_id', name='barcode_pool_pkey'),
    sa.UniqueConstraint('barcode', name='barcode_pool_barcode_key')
    )
    op.create_table('cannibalization_events',
    sa.Column('event_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
    sa.Column('product_size_id_out_of_stock', sa.Integer(), nullable=False),
    sa.Column('product_size_id_gained_demand', sa.Integer(), nullable=False),
    sa.Column('event_date', sa.Date(), nullable=False),
    sa.Column('demand_shift_qty', sa.Numeric(precision=10, scale=2), nullable=True),
    sa.Column('detected_at', sa.DateTime(), server_default=sa.func.now(), nullable=True),
    sa.ForeignKeyConstraint(['product_size_id_gained_demand'], ['product_sizes.product_size_id'], name='cannibalization_events_product_size_id_gained_demand_fkey'),
    sa.ForeignKeyConstraint(['product_size_id_out_of_stock'], ['product_sizes.product_size_id'], name='cannibalization_events_product_size_id_out_of_stock_fkey'),
    sa.PrimaryKeyConstraint('event_id', name='cannibalization_events_pkey')
    )
    op.create_table('central_stock_aggregate_demand',
    sa.Column('product_size_id', sa.Integer(), nullable=False),
    sa.Column('period_start', sa.Date(), nullable=False),
    sa.Column('period_end', sa.Date(), nullable=False),
    sa.Column('total_forecast_qty', sa.Numeric(precision=10, scale=2), nullable=False),
    sa.Column('channel_breakdown', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=True),
    sa.Column('computed_at', sa.DateTime(), server_default=sa.func.now(), nullable=True),
    sa.ForeignKeyConstraint(['product_size_id'], ['product_sizes.product_size_id'], name='central_stock_aggregate_demand_product_size_id_fkey'),
    sa.PrimaryKeyConstraint('product_size_id', 'period_start', 'period_end', name='central_stock_aggregate_demand_pkey')
    )
    op.create_table('cross_elasticity',
    sa.Column('product_size_id_a', sa.Integer(), nullable=False),
    sa.Column('product_size_id_b', sa.Integer(), nullable=False),
    sa.Column('cross_elasticity_coefficient', sa.Numeric(precision=6, scale=4), nullable=False),
    sa.Column('sample_size', sa.Integer(), nullable=True),
    sa.Column('computed_at', sa.DateTime(), server_default=sa.func.now(), nullable=True),
    sa.ForeignKeyConstraint(['product_size_id_a'], ['product_sizes.product_size_id'], name='cross_elasticity_product_size_id_a_fkey'),
    sa.ForeignKeyConstraint(['product_size_id_b'], ['product_sizes.product_size_id'], name='cross_elasticity_product_size_id_b_fkey'),
    sa.PrimaryKeyConstraint('product_size_id_a', 'product_size_id_b', name='cross_elasticity_pkey')
    )
    op.create_table('demand_forecast',
    sa.Column('product_size_id', sa.Integer(), nullable=False),
    sa.Column('channel', sa.Text(), server_default=sa.text("'lamoda'"), nullable=False),
    sa.Column('period_start', sa.Date(), nullable=False),
    sa.Column('period_end', sa.Date(), nullable=False),
    sa.Column('forecast_qty', sa.Numeric(precision=10, scale=2), nullable=False),
    sa.Column('lower_bound', sa.Numeric(precision=10, scale=2), nullable=True),
    sa.Column('upper_bound', sa.Numeric(precision=10, scale=2), nullable=True),
    sa.Column('model_type', sa.Text(), nullable=True),
    sa.Column('adi_days', sa.Numeric(precision=6, scale=2), nullable=True),
    sa.Column('cv_squared', sa.Numeric(precision=6, scale=3), nullable=True),
    sa.Column('demand_pattern', sa.Text(), nullable=True),
    sa.Column('computed_at', sa.DateTime(), server_default=sa.func.now(), nullable=True),
    sa.ForeignKeyConstraint(['product_size_id'], ['product_sizes.product_size_id'], name='demand_forecast_product_size_id_fkey'),
    sa.PrimaryKeyConstraint('product_size_id', 'channel', 'period_start', 'period_end', name='demand_forecast_pkey')
    )
    with op.batch_alter_table('demand_forecast', schema=None) as batch_op:
        batch_op.create_index('idx_demand_forecast_period', ['period_start', 'period_end'], unique=False)

    op.create_table('demand_timeseries',
    sa.Column('product_size_id', sa.Integer(), nullable=False),
    sa.Column('demand_date', sa.Date(), nullable=False),
    sa.Column('net_demand', sa.Numeric(precision=10, scale=2), nullable=False),
    sa.Column('base_demand', sa.Numeric(precision=10, scale=2), nullable=True),
    sa.Column('promo_uplift', sa.Numeric(precision=10, scale=2), nullable=True),
    sa.Column('is_stockout_corrected', sa.Boolean(), server_default=sa.false(), nullable=True),
    sa.Column('is_return_censored', sa.Boolean(), server_default=sa.false(), nullable=True),
    sa.Column('is_recovery_tail_corrected', sa.Boolean(), server_default=sa.false(), nullable=True),
    sa.ForeignKeyConstraint(['product_size_id'], ['product_sizes.product_size_id'], name='demand_timeseries_product_size_id_fkey'),
    sa.PrimaryKeyConstraint('product_size_id', 'demand_date', name='demand_timeseries_pkey')
    )
    op.create_table('fbo_shipments',
    sa.Column('shipment_id', sa.Text(), nullable=False),
    sa.Column('product_size_id', sa.Integer(), nullable=False),
    sa.Column('qty', sa.Integer(), nullable=False),
    sa.Column('created_date', sa.Date(), nullable=False),
    sa.Column('expected_arrival_date', sa.Date(), nullable=True),
    sa.Column('actual_arrival_date', sa.Date(), nullable=True),
    sa.Column('validation_status', sa.Text(), nullable=True),
    sa.Column('status', sa.Text(), server_default=sa.text("'in_transit'"), nullable=True),
    sa.ForeignKeyConstraint(['product_size_id'], ['product_sizes.product_size_id'], name='fbo_shipments_product_size_id_fkey'),
    sa.PrimaryKeyConstraint('shipment_id', 'product_size_id', name='fbo_shipments_pkey')
    )
    op.create_table('fill_rate_log',
    sa.Column('product_size_id', sa.Integer(), nullable=False),
    sa.Column('period_start', sa.Date(), nullable=False),
    sa.Column('period_end', sa.Date(), nullable=False),
    sa.Column('demand_qty_total', sa.Numeric(precision=10, scale=2), nullable=True),
    sa.Column('demand_qty_fulfilled', sa.Numeric(precision=10, scale=2), nullable=True),
    sa.Column('fill_rate_pct', sa.Numeric(precision=6, scale=3), nullable=True),
    sa.Column('computed_at', sa.DateTime(), server_default=sa.func.now(), nullable=True),
    sa.ForeignKeyConstraint(['product_size_id'], ['product_sizes.product_size_id'], name='fill_rate_log_product_size_id_fkey'),
    sa.PrimaryKeyConstraint('product_size_id', 'period_start', 'period_end', name='fill_rate_log_pkey')
    )
    op.create_table('forecast_accuracy_log',
    sa.Column('product_size_id', sa.Integer(), nullable=False),
    sa.Column('period_start', sa.Date(), nullable=False),
    sa.Column('period_end', sa.Date(), nullable=False),
    sa.Column('forecast_qty', sa.Numeric(precision=10, scale=2), nullable=True),
    sa.Column('actual_qty', sa.Numeric(precision=10, scale=2), nullable=True),
    sa.Column('mape', sa.Numeric(precision=6, scale=3), nullable=True),
    sa.Column('wape', sa.Numeric(precision=6, scale=3), nullable=True),
    sa.Column('mae', sa.Numeric(precision=10, scale=2), nullable=True),
    sa.Column('rmse', sa.Numeric(precision=10, scale=2), nullable=True),
    sa.Column('avg_deficit', sa.Numeric(precision=10, scale=2), nullable=True),
    sa.Column('avg_surplus', sa.Numeric(precision=10, scale=2), nullable=True),
    sa.Column('deviation_status', sa.Text(), server_default=sa.text("'normal'"), nullable=True),
    sa.Column('consecutive_warning_count', sa.Integer(), server_default=sa.text('0'), nullable=True),
    sa.Column('computed_at', sa.DateTime(), server_default=sa.func.now(), nullable=True),
    sa.ForeignKeyConstraint(['product_size_id'], ['product_sizes.product_size_id'], name='forecast_accuracy_log_product_size_id_fkey'),
    sa.PrimaryKeyConstraint('product_size_id', 'period_start', 'period_end', name='forecast_accuracy_log_pkey')
    )
    op.create_table('forecast_overrides',
    sa.Column('override_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
    sa.Column('product_size_id', sa.Integer(), nullable=False),
    sa.Column('period_start', sa.Date(), nullable=False),
    sa.Column('period_end', sa.Date(), nullable=False),
    sa.Column('original_forecast_qty', sa.Numeric(precision=10, scale=2), nullable=True),
    sa.Column('override_qty', sa.Numeric(precision=10, scale=2), nullable=False),
    sa.Column('reason', sa.Text(), nullable=False),
    sa.Column('overridden_by', sa.Text(), nullable=False),
    sa.Column('created_at', sa.DateTime(), server_default=sa.func.now(), nullable=True),
    sa.ForeignKeyConstraint(['product_size_id'], ['product_sizes.product_size_id'], name='forecast_overrides_product_size_id_fkey'),
    sa.PrimaryKeyConstraint('override_id', name='forecast_overrides_pkey')
    )
    op.create_table('funnel_size',
    sa.Column('product_size_id', sa.Integer(), nullable=False),
    sa.Column('period_start', sa.Date(), nullable=False),
    sa.Column('period_end', sa.Date(), nullable=False),
    sa.Column('orders_qty', sa.Integer(), nullable=True),
    sa.Column('purchased_qty', sa.Integer(), nullable=True),
    sa.Column('purchase_rate_pct', sa.Numeric(precision=6, scale=3), nullable=True),
    sa.Column('returns_qty', sa.Integer(), nullable=True),
    sa.Column('return_rate_pct', sa.Numeric(precision=6, scale=3), nullable=True),
    sa.ForeignKeyConstraint(['product_size_id'], ['product_sizes.product_size_id'], name='funnel_size_product_size_id_fkey'),
    sa.PrimaryKeyConstraint('product_size_id', 'period_start', 'period_end', name='funnel_size_pkey')
    )
    op.create_table('illiquid_flags',
    sa.Column('product_size_id', sa.Integer(), nullable=False),
    sa.Column('snapshot_date', sa.Date(), nullable=False),
    sa.Column('is_illiquid', sa.Boolean(), nullable=False),
    sa.ForeignKeyConstraint(['product_size_id'], ['product_sizes.product_size_id'], name='illiquid_flags_product_size_id_fkey'),
    sa.PrimaryKeyConstraint('product_size_id', 'snapshot_date', name='illiquid_flags_pkey')
    )
    op.create_table('margin_calc',
    sa.Column('product_size_id', sa.Integer(), nullable=False),
    sa.Column('calc_date', sa.Date(), nullable=False),
    sa.Column('channel', sa.Text(), server_default=sa.text("'lamoda'"), nullable=False),
    sa.Column('price_realized', sa.Numeric(precision=10, scale=2), nullable=False),
    sa.Column('cost_unit', sa.Numeric(precision=10, scale=2), nullable=False),
    sa.Column('commission_amount', sa.Numeric(precision=10, scale=2), nullable=True),
    sa.Column('logistics_fbo_amount', sa.Numeric(precision=10, scale=2), nullable=True),
    sa.Column('vat_amount', sa.Numeric(precision=10, scale=2), nullable=True),
    sa.Column('usn_tax_amount', sa.Numeric(precision=10, scale=2), nullable=True),
    sa.Column('margin_abs', sa.Numeric(precision=10, scale=2), nullable=True),
    sa.Column('margin_pct', sa.Numeric(precision=6, scale=2), nullable=True),
    sa.Column('deviation_reason', sa.Text(), nullable=True),
    sa.ForeignKeyConstraint(['product_size_id'], ['product_sizes.product_size_id'], name='margin_calc_product_size_id_fkey'),
    sa.PrimaryKeyConstraint('product_size_id', 'calc_date', 'channel', name='margin_calc_pkey')
    )
    with op.batch_alter_table('margin_calc', schema=None) as batch_op:
        batch_op.create_index('idx_margin_calc_date', ['calc_date'], unique=False)

    op.create_table('model_backtest_results',
    sa.Column('backtest_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
    sa.Column('product_size_id', sa.Integer(), nullable=False),
    sa.Column('backtest_date', sa.Date(), nullable=False),
    sa.Column('model_type', sa.Text(), nullable=False),
    sa.Column('wape_holdout', sa.Numeric(precision=6, scale=3), nullable=False),
    sa.Column('selected', sa.Boolean(), server_default=sa.false(), nullable=True),
    sa.ForeignKeyConstraint(['product_size_id'], ['product_sizes.product_size_id'], name='model_backtest_results_product_size_id_fkey'),
    sa.PrimaryKeyConstraint('backtest_id', name='model_backtest_results_pkey')
    )
    op.create_table('model_recalibration_log',
    sa.Column('recalibration_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
    sa.Column('product_size_id', sa.Integer(), nullable=False),
    sa.Column('triggered_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
    sa.Column('reason', sa.Text(), nullable=True),
    sa.Column('old_model_type', sa.Text(), nullable=True),
    sa.Column('new_model_type', sa.Text(), nullable=True),
    sa.Column('triggered_by', sa.Text(), server_default=sa.text("'auto'"), nullable=True),
    sa.ForeignKeyConstraint(['product_size_id'], ['product_sizes.product_size_id'], name='model_recalibration_log_product_size_id_fkey'),
    sa.PrimaryKeyConstraint('recalibration_id', name='model_recalibration_log_pkey')
    )
    op.create_table('new_item_size_allocation',
    sa.Column('product_model_id', sa.Integer(), nullable=False),
    sa.Column('computed_at', sa.DateTime(), nullable=False),
    sa.Column('product_size_id', sa.Integer(), nullable=False),
    sa.Column('allocated_qty', sa.Numeric(precision=10, scale=2), nullable=False),
    sa.ForeignKeyConstraint(['product_model_id', 'computed_at'], ['new_item_forecast.product_model_id', 'new_item_forecast.computed_at'], name='new_item_size_allocation_product_model_id_computed_at_fkey'),
    sa.ForeignKeyConstraint(['product_size_id'], ['product_sizes.product_size_id'], name='new_item_size_allocation_product_size_id_fkey'),
    sa.PrimaryKeyConstraint('product_model_id', 'computed_at', 'product_size_id', name='new_item_size_allocation_pkey')
    )
    op.create_table('order_items',
    sa.Column('order_item_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
    sa.Column('order_id', sa.Text(), nullable=False),
    sa.Column('tracking_id', sa.Text(), nullable=True),
    sa.Column('lamoda_item_id', sa.BigInteger(), nullable=True),
    sa.Column('product_size_id', sa.Integer(), nullable=True),
    sa.Column('status', sa.Text(), nullable=False),
    sa.Column('price_actual', sa.Numeric(precision=10, scale=2), nullable=True),
    sa.Column('price_base', sa.Numeric(precision=10, scale=2), nullable=True),
    sa.Column('total_discount', sa.Numeric(precision=10, scale=2), nullable=True),
    sa.Column('event_date', sa.Date(), nullable=False),
    sa.Column('ingested_at', sa.DateTime(), server_default=sa.func.now(), nullable=True),
    sa.ForeignKeyConstraint(['product_size_id'], ['product_sizes.product_size_id'], name='order_items_product_size_id_fkey'),
    sa.PrimaryKeyConstraint('order_item_id', name='order_items_pkey'),
    sa.UniqueConstraint('order_id', 'lamoda_item_id', 'status', name='order_items_order_id_lamoda_item_id_status_key')
    )
    with op.batch_alter_table('order_items', schema=None) as batch_op:
        batch_op.create_index('idx_order_items_size_date', ['product_size_id', 'event_date'], unique=False)

    op.create_table('price_optimization_result',
    sa.Column('run_id', sa.BigInteger(), nullable=False),
    sa.Column('product_size_id', sa.Integer(), nullable=False),
    sa.Column('current_price', sa.Numeric(precision=10, scale=2), nullable=True),
    sa.Column('recommended_price', sa.Numeric(precision=10, scale=2), nullable=True),
    sa.Column('price_change_pct', sa.Numeric(precision=6, scale=2), nullable=True),
    sa.Column('predicted_margin_impact', sa.Numeric(precision=10, scale=2), nullable=True),
    sa.Column('predicted_demand_impact_pct', sa.Numeric(precision=6, scale=2), nullable=True),
    sa.Column('stock_check_passed', sa.Boolean(), server_default=sa.true(), nullable=True),
    sa.Column('included_in_recommendations', sa.Boolean(), server_default=sa.false(), nullable=True),
    sa.ForeignKeyConstraint(['product_size_id'], ['product_sizes.product_size_id'], name='price_optimization_result_product_size_id_fkey'),
    sa.ForeignKeyConstraint(['run_id'], ['price_optimization_run.run_id'], name='price_optimization_result_run_id_fkey'),
    sa.PrimaryKeyConstraint('run_id', 'product_size_id', name='price_optimization_result_pkey')
    )
    op.create_table('price_scenario_log',
    sa.Column('scenario_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
    sa.Column('product_size_id', sa.Integer(), nullable=False),
    sa.Column('price_change_pct', sa.Numeric(precision=6, scale=2), nullable=False),
    sa.Column('predicted_demand_change_pct', sa.Numeric(precision=6, scale=2), nullable=True),
    sa.Column('predicted_margin_impact', sa.Numeric(precision=10, scale=2), nullable=True),
    sa.Column('applied', sa.Boolean(), server_default=sa.false(), nullable=True),
    sa.Column('created_at', sa.DateTime(), server_default=sa.func.now(), nullable=True),
    sa.ForeignKeyConstraint(['product_size_id'], ['product_sizes.product_size_id'], name='price_scenario_log_product_size_id_fkey'),
    sa.PrimaryKeyConstraint('scenario_id', name='price_scenario_log_pkey')
    )
    op.create_table('promo_report_sku',
    sa.Column('product_size_id', sa.Integer(), nullable=False),
    sa.Column('period_start', sa.Date(), nullable=False),
    sa.Column('period_end', sa.Date(), nullable=False),
    sa.Column('orders_sum_promo', sa.Numeric(precision=12, scale=2), nullable=True),
    sa.Column('orders_qty_promo', sa.Integer(), nullable=True),
    sa.Column('redemption_sum_promo', sa.Numeric(precision=12, scale=2), nullable=True),
    sa.Column('redemption_qty_promo', sa.Integer(), nullable=True),
    sa.Column('redemption_rate_pct', sa.Numeric(precision=6, scale=3), nullable=True),
    sa.Column('avg_price_seller_and_lamoda', sa.Numeric(precision=10, scale=2), nullable=True),
    sa.Column('avg_discount_seller_and_lamoda_pct', sa.Numeric(precision=6, scale=3), nullable=True),
    sa.Column('avg_price_seller', sa.Numeric(precision=10, scale=2), nullable=True),
    sa.Column('avg_discount_seller_pct', sa.Numeric(precision=6, scale=3), nullable=True),
    sa.ForeignKeyConstraint(['product_size_id'], ['product_sizes.product_size_id'], name='promo_report_sku_product_size_id_fkey'),
    sa.PrimaryKeyConstraint('product_size_id', 'period_start', 'period_end', name='promo_report_sku_pkey')
    )
    op.create_table('purchase_recommendations',
    sa.Column('recommendation_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
    sa.Column('product_size_id', sa.Integer(), nullable=False),
    sa.Column('supplier_id', sa.Integer(), nullable=True),
    sa.Column('qty_to_purchase', sa.Numeric(precision=10, scale=2), nullable=False),
    sa.Column('ready_by_date', sa.Date(), nullable=True),
    sa.Column('status', sa.Text(), server_default=sa.text("'pending'"), nullable=True),
    sa.Column('created_at', sa.DateTime(), server_default=sa.func.now(), nullable=True),
    sa.ForeignKeyConstraint(['product_size_id'], ['product_sizes.product_size_id'], name='purchase_recommendations_product_size_id_fkey'),
    sa.ForeignKeyConstraint(['supplier_id'], ['suppliers.supplier_id'], name='purchase_recommendations_supplier_id_fkey'),
    sa.PrimaryKeyConstraint('recommendation_id', name='purchase_recommendations_pkey')
    )
    op.create_table('recommendation_confidence',
    sa.Column('product_size_id', sa.Integer(), nullable=False),
    sa.Column('computed_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
    sa.Column('wape_penalty', sa.Numeric(precision=5, scale=2), nullable=True),
    sa.Column('demand_pattern_penalty', sa.Numeric(precision=5, scale=2), nullable=True),
    sa.Column('short_history_penalty', sa.Numeric(precision=5, scale=2), nullable=True),
    sa.Column('model_disagreement_penalty', sa.Numeric(precision=5, scale=2), nullable=True),
    sa.Column('recent_deviation_penalty', sa.Numeric(precision=5, scale=2), nullable=True),
    sa.Column('stockout_share_penalty', sa.Numeric(precision=5, scale=2), nullable=True),
    sa.Column('confidence_pct', sa.Numeric(precision=5, scale=2), nullable=False),
    sa.Column('explanation_text', sa.Text(), nullable=True),
    sa.ForeignKeyConstraint(['product_size_id'], ['product_sizes.product_size_id'], name='recommendation_confidence_product_size_id_fkey'),
    sa.PrimaryKeyConstraint('product_size_id', 'computed_at', name='recommendation_confidence_pkey')
    )
    op.create_table('reorder_points',
    sa.Column('product_size_id', sa.Integer(), nullable=False),
    sa.Column('computed_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
    sa.Column('lead_time_used_days', sa.Numeric(precision=6, scale=2), nullable=True),
    sa.Column('sigma_used', sa.Numeric(precision=10, scale=3), nullable=True),
    sa.Column('formula_variant', sa.Text(), server_default=sa.text("'basic'"), nullable=True),
    sa.Column('safety_stock', sa.Numeric(precision=10, scale=2), nullable=True),
    sa.Column('reorder_point_qty', sa.Numeric(precision=10, scale=2), nullable=False),
    sa.ForeignKeyConstraint(['product_size_id'], ['product_sizes.product_size_id'], name='reorder_points_product_size_id_fkey'),
    sa.PrimaryKeyConstraint('product_size_id', 'computed_at', name='reorder_points_pkey')
    )
    op.create_table('sales_daily',
    sa.Column('product_size_id', sa.Integer(), nullable=False),
    sa.Column('sale_date', sa.Date(), nullable=False),
    sa.Column('qty_delivered', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('qty_returned', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('revenue', sa.Numeric(precision=12, scale=2), nullable=True),
    sa.ForeignKeyConstraint(['product_size_id'], ['product_sizes.product_size_id'], name='sales_daily_product_size_id_fkey'),
    sa.PrimaryKeyConstraint('product_size_id', 'sale_date', name='sales_daily_pkey')
    )
    with op.batch_alter_table('sales_daily', schema=None) as batch_op:
        batch_op.create_index('idx_sales_daily_date', ['sale_date'], unique=False)

    op.create_table('shipment_recommendations',
    sa.Column('recommendation_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
    sa.Column('product_size_id', sa.Integer(), nullable=False),
    sa.Column('stock_1c_qty_snapshot', sa.Numeric(precision=10, scale=2), nullable=True),
    sa.Column('stock_fbo_qty_snapshot', sa.Numeric(precision=10, scale=2), nullable=True),
    sa.Column('qty_to_ship', sa.Numeric(precision=10, scale=2), nullable=False),
    sa.Column('confidence_pct', sa.Numeric(precision=5, scale=2), nullable=True),
    sa.Column('confidence_explanation', sa.Text(), nullable=True),
    sa.Column('confirmed_qty', sa.Numeric(precision=10, scale=2), nullable=True),
    sa.Column('related_fbo_shipment_id', sa.Text(), nullable=True),
    sa.Column('correction_pct_vs_original', sa.Numeric(precision=6, scale=2), nullable=True),
    sa.Column('penalty_risk', sa.Boolean(), server_default=sa.false(), nullable=True),
    sa.Column('target_ship_date', sa.Date(), nullable=True),
    sa.Column('status', sa.Text(), server_default=sa.text("'pending'"), nullable=True),
    sa.Column('created_at', sa.DateTime(), server_default=sa.func.now(), nullable=True),
    sa.ForeignKeyConstraint(['product_size_id'], ['product_sizes.product_size_id'], name='shipment_recommendations_product_size_id_fkey'),
    sa.PrimaryKeyConstraint('recommendation_id', name='shipment_recommendations_pkey')
    )
    op.create_table('stock_1c',
    sa.Column('product_size_id', sa.Integer(), nullable=False),
    sa.Column('snapshot_date', sa.Date(), nullable=False),
    sa.Column('qty', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['product_size_id'], ['product_sizes.product_size_id'], name='stock_1c_product_size_id_fkey'),
    sa.PrimaryKeyConstraint('product_size_id', 'snapshot_date', name='stock_1c_pkey')
    )
    op.create_table('stock_fbo',
    sa.Column('product_size_id', sa.Integer(), nullable=False),
    sa.Column('warehouse_code', sa.Text(), nullable=False),
    sa.Column('snapshot_date', sa.Date(), nullable=False),
    sa.Column('qty', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['product_size_id'], ['product_sizes.product_size_id'], name='stock_fbo_product_size_id_fkey'),
    sa.PrimaryKeyConstraint('product_size_id', 'warehouse_code', 'snapshot_date', name='stock_fbo_pkey')
    )
    with op.batch_alter_table('stock_fbo', schema=None) as batch_op:
        batch_op.create_index('idx_stock_fbo_date', ['snapshot_date'], unique=False)

    op.create_table('stock_reconciliation_log',
    sa.Column('reconciliation_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
    sa.Column('product_size_id', sa.Integer(), nullable=False),
    sa.Column('check_date', sa.Date(), nullable=False),
    sa.Column('expected_qty_computed', sa.Numeric(precision=10, scale=2), nullable=True),
    sa.Column('actual_qty_from_snapshot', sa.Numeric(precision=10, scale=2), nullable=True),
    sa.Column('discrepancy', sa.Numeric(precision=10, scale=2), nullable=True),
    sa.Column('resolved', sa.Boolean(), server_default=sa.false(), nullable=True),
    sa.ForeignKeyConstraint(['product_size_id'], ['product_sizes.product_size_id'], name='stock_reconciliation_log_product_size_id_fkey'),
    sa.PrimaryKeyConstraint('reconciliation_id', name='stock_reconciliation_log_pkey')
    )
    op.create_table('yandex_direct_clicks',
    sa.Column('ad_id', sa.Text(), nullable=False),
    sa.Column('click_date', sa.Date(), nullable=False),
    sa.Column('clicks', sa.Integer(), nullable=True),
    sa.Column('impressions', sa.Integer(), nullable=True),
    sa.Column('ctr_pct', sa.Numeric(precision=6, scale=3), nullable=True),
    sa.Column('spend', sa.Numeric(precision=10, scale=2), nullable=True),
    sa.ForeignKeyConstraint(['ad_id'], ['yandex_direct_ad_mapping.ad_id'], name='yandex_direct_clicks_ad_id_fkey'),
    sa.PrimaryKeyConstraint('ad_id', 'click_date', name='yandex_direct_clicks_pkey')
    )
    op.create_table('price_ab_test',
    sa.Column('test_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
    sa.Column('treatment_product_size_id', sa.Integer(), nullable=False),
    sa.Column('control_product_size_ids', sa.JSON().with_variant(postgresql.ARRAY(sa.Integer()), 'postgresql'), nullable=False),
    sa.Column('scenario_id', sa.BigInteger(), nullable=True),
    sa.Column('test_start', sa.Date(), nullable=False),
    sa.Column('test_end', sa.Date(), nullable=False),
    sa.Column('demand_change_pct_treatment', sa.Numeric(precision=6, scale=2), nullable=True),
    sa.Column('demand_change_pct_control', sa.Numeric(precision=6, scale=2), nullable=True),
    sa.Column('diff_in_diff_effect', sa.Numeric(precision=6, scale=2), nullable=True),
    sa.Column('decision', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(), server_default=sa.func.now(), nullable=True),
    sa.ForeignKeyConstraint(['scenario_id'], ['price_scenario_log.scenario_id'], name='price_ab_test_scenario_id_fkey'),
    sa.ForeignKeyConstraint(['treatment_product_size_id'], ['product_sizes.product_size_id'], name='price_ab_test_treatment_product_size_id_fkey'),
    sa.PrimaryKeyConstraint('test_id', name='price_ab_test_pkey')
    )
    op.create_table('pricing_recommendations',
    sa.Column('recommendation_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
    sa.Column('product_size_id', sa.Integer(), nullable=False),
    sa.Column('recommendation_type', sa.Text(), nullable=False),
    sa.Column('rationale', sa.Text(), nullable=True),
    sa.Column('margin_pct_at_calc', sa.Numeric(precision=6, scale=2), nullable=True),
    sa.Column('source', sa.Text(), server_default=sa.text("'rule'"), nullable=True),
    sa.Column('optimization_run_id', sa.BigInteger(), nullable=True),
    sa.Column('status', sa.Text(), server_default=sa.text("'pending'"), nullable=True),
    sa.Column('suppressed_by_triage_id', sa.BigInteger(), nullable=True),
    sa.Column('approved_by', sa.Text(), nullable=True),
    sa.Column('approved_at', sa.DateTime(), nullable=True),
    sa.Column('created_at', sa.DateTime(), server_default=sa.func.now(), nullable=True),
    sa.ForeignKeyConstraint(['optimization_run_id'], ['price_optimization_run.run_id'], name='pricing_recommendations_optimization_run_id_fkey'),
    sa.ForeignKeyConstraint(['product_size_id'], ['product_sizes.product_size_id'], name='pricing_recommendations_product_size_id_fkey'),
    sa.ForeignKeyConstraint(['suppressed_by_triage_id'], ['anomaly_triage_log.triage_id'], name='pricing_recommendations_suppressed_by_triage_id_fkey'),
    sa.PrimaryKeyConstraint('recommendation_id', name='pricing_recommendations_pkey')
    )
    op.create_table('price_change_log',
    sa.Column('price_change_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
    sa.Column('recommendation_id', sa.BigInteger(), nullable=False),
    sa.Column('product_size_id', sa.Integer(), nullable=False),
    sa.Column('old_price', sa.Numeric(precision=10, scale=2), nullable=True),
    sa.Column('new_price', sa.Numeric(precision=10, scale=2), nullable=False),
    sa.Column('api_method', sa.Text(), server_default=sa.text("'POST /v2/nomenclature-price'"), nullable=True),
    sa.Column('api_response_status', sa.Text(), nullable=True),
    sa.Column('fraud_validation_result', sa.Text(), nullable=True),
    sa.Column('api_error_message', sa.Text(), nullable=True),
    sa.Column('submitted_at', sa.DateTime(), server_default=sa.func.now(), nullable=True),
    sa.ForeignKeyConstraint(['product_size_id'], ['product_sizes.product_size_id'], name='price_change_log_product_size_id_fkey'),
    sa.ForeignKeyConstraint(['recommendation_id'], ['pricing_recommendations.recommendation_id'], name='price_change_log_recommendation_id_fkey'),
    sa.PrimaryKeyConstraint('price_change_id', name='price_change_log_pkey')
    )


def downgrade():
    op.drop_table('price_change_log')
    op.drop_table('pricing_recommendations')
    op.drop_table('price_ab_test')
    op.drop_table('yandex_direct_clicks')
    op.drop_table('stock_reconciliation_log')
    with op.batch_alter_table('stock_fbo', schema=None) as batch_op:
        batch_op.drop_index('idx_stock_fbo_date')

    op.drop_table('stock_fbo')
    op.drop_table('stock_1c')
    op.drop_table('shipment_recommendations')
    with op.batch_alter_table('sales_daily', schema=None) as batch_op:
        batch_op.drop_index('idx_sales_daily_date')

    op.drop_table('sales_daily')
    op.drop_table('reorder_points')
    op.drop_table('recommendation_confidence')
    op.drop_table('purchase_recommendations')
    op.drop_table('promo_report_sku')
    op.drop_table('price_scenario_log')
    op.drop_table('price_optimization_result')
    with op.batch_alter_table('order_items', schema=None) as batch_op:
        batch_op.drop_index('idx_order_items_size_date')

    op.drop_table('order_items')
    op.drop_table('new_item_size_allocation')
    op.drop_table('model_recalibration_log')
    op.drop_table('model_backtest_results')
    with op.batch_alter_table('margin_calc', schema=None) as batch_op:
        batch_op.drop_index('idx_margin_calc_date')

    op.drop_table('margin_calc')
    op.drop_table('illiquid_flags')
    op.drop_table('funnel_size')
    op.drop_table('forecast_overrides')
    op.drop_table('forecast_accuracy_log')
    op.drop_table('fill_rate_log')
    op.drop_table('fbo_shipments')
    op.drop_table('demand_timeseries')
    with op.batch_alter_table('demand_forecast', schema=None) as batch_op:
        batch_op.drop_index('idx_demand_forecast_period')

    op.drop_table('demand_forecast')
    op.drop_table('cross_elasticity')
    op.drop_table('central_stock_aggregate_demand')
    op.drop_table('cannibalization_events')
    op.drop_table('barcode_pool')
    op.drop_table('anomaly_triage_log')
    op.drop_table('yandex_direct_ad_mapping')
    op.drop_table('underperformance_flags')
    op.drop_table('style_tags')
    op.drop_table('promo_cannibalization_pairs')
    op.drop_table('product_sizes')
    op.drop_table('product_analogs')
    op.drop_table('new_item_forecast')
    op.drop_table('metrika_audience_segments')
    op.drop_table('funnel_model')
    op.drop_table('audience_quality_score')
    op.drop_table('abc_xyz_classification')
    op.drop_table('supplier_deliveries')
    op.drop_table('product_models')
    op.drop_table('wordstat_data')
    op.drop_table('webhook_events_log')
    op.drop_table('weather_data')
    op.drop_table('usd_rate')
    op.drop_table('suppliers')
    op.drop_table('size_profile')
    op.drop_table('seasonal_index_reference')
    op.drop_table('seasonal_coefficients')
    op.drop_table('promo_report_daily')
    op.drop_table('promo_calendar')
    op.drop_table('price_optimization_run')
    op.drop_table('price_elasticity')
    op.drop_table('margin_settings')
    op.drop_table('lead_time_stats')
    op.drop_table('lamoda_official_reconciliation')
    op.drop_table('google_trends_data')
    op.drop_table('forecast_deviation_settings')
    op.drop_table('forecast_correction_settings')
    op.drop_table('external_channel_sales_history')
    op.drop_table('color_profile')
    op.drop_table('channel_ratio_calibration')
    op.drop_table('channel_commission_rates')
    op.drop_table('category_market_trends')
    op.drop_table('buyer_profile_reference')
