-- Safe example input kept outside work/input so it remains version-controlled.
CREATE TABLE IF NOT EXISTS analytics.customer_orders (
  order_id BIGINT,
  customer_name STRING,
  ordered_at TIMESTAMP,
  amount DECIMAL(18, 2),
  tags ARRAY<STRING>
)
STORED AS PARQUET;
