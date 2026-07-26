CREATE EXTERNAL TABLE `source_db`.`events` (
  `event_id` BIGINT COMMENT 'Идентификатор',
  `owner` STRING COMMENT 'O\'Brien',
  `amount` DECIMAL(18, 2)
)
COMMENT 'События команды O\'Brien'
PARTITIONED BY (
  `event_day` DATE COMMENT 'День события'
)
STORED AS PARQUET
