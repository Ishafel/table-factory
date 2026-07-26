-- Paste a SHOW CREATE TABLE result here; the source remains read-only.
CREATE EXTERNAL TABLE analytics.customer_orders (
  order_id BIGINT COMMENT 'Stable order identifier',
  customer_name STRING COMMENT 'Customer''s display name',
  ordered_at TIMESTAMP,
  amount DECIMAL(18, 2)
)
COMMENT 'Orders ready for warehouse transfer'
PARTITIONED BY (
  business_date DATE COMMENT 'Source partition date'
)
STORED AS PARQUET
LOCATION 'hdfs://example.invalid/warehouse/analytics/customer_orders'
TBLPROPERTIES (
  'source'='SHOW CREATE TABLE example'
)
