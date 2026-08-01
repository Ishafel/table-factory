# table-factory

`table-factory` — офлайн CLI-утилита, которая преобразует DDL существующей
Hive-таблицы в настраиваемый набор SQL-файлов шести стабильных ролей. По
умолчанию включены все шесть ролей, включая Liquibase-файл создания Greenplum
external table.

Утилита работает только с локальными файлами: разбирает DDL, проверяет
конфигурацию и генерирует SQL. Она не подключается к Hive, Hadoop, Greenplum
или PXF, не выполняет SQL, не проверяет сеть и не хранит credentials. В
`compose.yaml` есть только одноразовый сервис `table-factory`; целевые системы
и другие внешние сервисы проект не запускает.

## Рабочий процесс

Практический сценарий начинается в Hue:

1. Откройте существующую Hive-таблицу в Hue и получите результат
   `SHOW CREATE TABLE`.
2. Сохраните результат как UTF-8 SQL-файл в `work/input/`, например
   `work/input/customer_orders.sql`.
3. Запустите `table-factory generate`.
4. Получите включённые в config скрипты в `work/output/` — по умолчанию все
   шесть.
5. Передайте скрипты исполнителю целевой среды и выберите один маршрут:
   `01 → 02 → 03 → 04 → 05` для прямого PXF либо
   `03_greenplum_create_external_liquibase → 04 → 05` для Liquibase.

```text
Hue: SHOW CREATE TABLE
          │
          ▼
work/input/*.sql
          │
          ▼
table-factory generate  (локальный parse/validate/render, без подключений)
          │
          ▼
work/output/*.sql
```

Сгенерированная цепочка данных:

```text
существующая source Hive table
          │  Hive INSERT
          ▼
новая физическая Hive table (TEXTFILE, без partitioning)
          │  читается через PXF LOCATION строгого Hive-профиля
          ▼
Greenplum external table
          │  Greenplum INSERT
          ▼
новая физическая Greenplum table
```

Source-таблица считается существующим read-only источником. Ни один артефакт
не изменяет и не удаляет её.

## Быстрый старт

Для Docker-first запуска нужны Docker и Docker Compose v2:

```bash
export LOCAL_UID="$(id -u)"
export LOCAL_GID="$(id -g)"

cp examples/example.sql work/input/example.sql

docker compose build table-factory

docker compose run --rm table-factory generate \
  --input ./work/input \
  --output ./work/output \
  --config ./config/table-factory.yaml
```

Корень проекта примонтирован в контейнер как `/workspace`, поэтому результат
остаётся на хосте после завершения одноразового контейнера. Экспорт
`LOCAL_UID`/`LOCAL_GID` нужен на Unix-хостах, чтобы build и последующие
`docker compose run` использовали владельца bind-mounted файлов. Если
переменные не заданы, Compose использует `1000:1000`. Нулевые UID/GID
отклоняются при сборке: root-driven CI должен явно выбрать ненулевой remap,
поскольку development image никогда не запускает CLI от root.

Первичная сборка может обращаться к registry базового image и Python package
index, если нужных слоёв и пакетов ещё нет в cache. Уже собранный CLI выполняет
`generate`, `validate` и `inspect` без сетевых обращений.

Для source-таблицы `analytics.customer_orders`, стандартного разделителя `__`
и включённых по умолчанию шести ролей будут созданы:

```text
analytics_customer_orders__01_hive_create_physical.sql
analytics_customer_orders__02_hive_insert.sql
analytics_customer_orders__03_greenplum_create_external.sql
analytics_customer_orders__03_greenplum_create_external_liquibase.sql
analytics_customer_orders__04_greenplum_create_physical.sql
analytics_customer_orders__05_greenplum_insert.sql
```

Значения `hive.replica` и `greenplum.replica` влияют на target table names,
а `greenplum.subscription` и `greenplum.original_hive_database` — на database
alias в PXF `LOCATION`. На имена файлов эти поля не влияют: filename stem
по-прежнему строится только из source qualified name.

Существующая output-директория автоматически не очищается. Все новые файлы
сначала полностью записываются во временные файлы, после чего совпадающие
destinations заменяются последовательно. При обычной ошибке записи утилита
пытается откатить уже выполненные замены. Это не единая файловая транзакция:
аварийное завершение процесса и отказ самой операции rollback не гарантируют
восстановление всего набора. Посторонние и устаревшие файлы остаются на месте.
Используйте новую пустую директорию, если нужно проверить, что для одной
таблицы результат содержит ровно выбранные файлы. В частности, отключение роли
в config не удаляет её файл, оставшийся от предыдущего запуска.

## Что принимается на вход

Поддерживаются обычные и external Hive-таблицы:

```sql
CREATE TABLE `analytics`.`customer_orders` (
  `order_id` BIGINT COMMENT 'Stable order identifier',
  `customer_name` STRING COMMENT 'Customer''s display name',
  `ordered_at` TIMESTAMP,
  `amount` DECIMAL(18,2)
)
COMMENT 'Заказы из Hue'
PARTITIONED BY (
  `business_date` DATE COMMENT 'Дата партиции источника'
)
STORED AS PARQUET
LOCATION 'hdfs://example.invalid/warehouse/analytics/customer_orders'
TBLPROPERTIES (
  'source'='SHOW CREATE TABLE example'
)
```

Завершающая точка с запятой необязательна. Ключевое слово `EXTERNAL` также
необязательно: и `CREATE TABLE`, и `CREATE EXTERNAL TABLE` описывают уже
существующий источник.

Парсер поддерживает:

- многострочный DDL и несколько таблиц в одном SQL-файле;
- обычные и квалифицированные имена `database.table`;
- квалифицированные имена как в отдельных backticks
  `` `database`.`table` ``, так и в совместимой с некоторыми вариантами
  `SHOW CREATE TABLE` форме `` `database.table` ``;
- backtick identifiers, включая экранированные обратные кавычки;
- `TEMPORARY`, `EXTERNAL` и `IF NOT EXISTS`;
- column и table `COMMENT`;
- `PARTITIONED BY`;
- распространённые column/table constraints;
- `CLUSTERED BY` с необязательным `SORTED BY`, `SKEWED BY` с необязательным
  `STORED AS DIRECTORIES` и другие перечисленные ниже table clauses в
  стандартном Hive-порядке;
- source `ROW FORMAT`, `STORED AS`, `STORED AS INPUTFORMAT ... OUTPUTFORMAT`,
  `STORED BY`, `LOCATION`, `SERDEPROPERTIES` и `TBLPROPERTIES`;
- source storage `TEXTFILE`, `SEQUENCEFILE`, `RCFILE`, `ORC`, `PARQUET`,
  `AVRO` и `JSONFILE`;
- разорванные на несколько значений Spark schema properties: они остаются
  properties и не становятся колонками;
- `-- ...` и `/* ... */` SQL-комментарии при разборе.

Source storage и структурные clauses нужны только для корректного разбора. Они
не определяют новую Hive-таблицу: source `TEMPORARY`, `IF NOT EXISTS`,
`EXTERNAL`, constraints, `LOCATION`, SerDe, InputFormat, OutputFormat, storage
format, `TBLPROPERTIES`, clustering, sorting, bucketing, skewing и
`STORED AS DIRECTORIES` не переносятся в target. Признак `EXTERNAL` остаётся
только в JSON команды `inspect`.

В позиции полного имени source-таблицы ровно одна точка внутри единой пары
backticks интерпретируется как разделитель database и table. Например,
`` `sales.daily` `` разбирается так же, как `` `sales`.`daily` ``, и в Hive
`INSERT` записывается в последней, однозначной форме. Поэтому literal-имя
неквалифицированной таблицы с точкой этой записью задать нельзя. Пустые части и
несколько точек внутри единой пары backticks отклоняются. Backtick-имена
колонок не делятся по точке; точка сохраняется как часть identifier, после чего
общая semantic validation отклоняет такую колонку как ненадёжно адресуемую.

Неквалифицированный source принимается; в Hive `INSERT` после `FROM`
указывается только quoted name `` `table` ``, без команды `USE`. При
исполнении такого файла необходимо заранее выбрать правильную текущую Hive
database. Для однозначной цепочки предпочтительно передавать
квалифицированный source `database.table`.

Отклоняются:

- `CREATE TABLE AS SELECT`;
- `CREATE TABLE LIKE`;
- SQL-команды и запросы, отличные от поддержанного `CREATE TABLE`;
- malformed DDL, повторные или расположенные в неверном порядке table clauses;
- пустой список колонок;
- double-quoted identifiers и unquoted identifiers вне ASCII-грамматики
  `[A-Za-z_][A-Za-z0-9_]*`; произвольные имена нужно заключать в backticks;
- Unicode control (`Cc`), format (`Cf`), line separator (`Zl`) и paragraph
  separator (`Zp`) символы в source database/table/column identifiers и NUL в
  table/column comments;
- повторные column names среди общего списка обычных и partition columns после
  Unicode NFKC normalization и сравнения без учёта регистра;
- Hive column names с точкой (`.`) или двоеточием (`:`): такой DDL может
  разбираться, но Hive не может надёжно адресовать эти колонки в сгенерированном
  `SELECT`;
- типы без безопасного Hive → Greenplum mapping.

Input может быть одним `.sql`-файлом или директорией. Директория обходится
рекурсивно и детерминированно, расширение `.sql` сравнивается без учёта
регистра. Все документы читаются как UTF-8 и проверяются до начала записи.

## Выходные файлы

`output.artifacts` глобально выбирает роли, генерируемые для каждой source
table. Секция необязательна: отсутствующая секция, пустая mapping и пропущенные
в ней роли означают `true`. Effective config в `inspect` всегда показывает все
шесть boolean-значений. Флаги управляют только созданием файлов: утилита не
добавляет зависимости автоматически, не выполняет SQL и не удаляет ранее
созданные файлы отключённых ролей.

Два файла с номером шага `03` — взаимоисключающие способы создать одну и ту же
external table. Liquibase-вариант находится рядом с прямым вариантом и до
шагов `04`–`05` при лексикографической сортировке. Каждая строка таблицы
создаётся только при включённой соответствующей роли:

| № | Роль и имя | Что делает |
| --- | --- | --- |
| 1 | `<stem>__01_hive_create_physical.sql` | Создаёт обычную непартиционированную Hive-таблицу в настроенной target database |
| 2 | `<stem>__02_hive_insert.sql` | Явно переносит колонки из source Hive table в новую Hive table |
| 3a | `<stem>__03_greenplum_create_external.sql` | Создаёт Greenplum external table, которая через PXF читает новую Hive table |
| 3b | `<stem>__03_greenplum_create_external_liquibase.sql` | Liquibase changeset: пересоздаёт external table через `f_create_external_table(JSON)` поверх исходной Hive table |
| 4 | `<stem>__04_greenplum_create_physical.sql` | Создаёт обычную непартиционированную Greenplum table |
| 5 | `<stem>__05_greenplum_insert.sql` | Явно переносит колонки из external table в physical table |

Прямой маршрут `01 → 02 → 03_greenplum_create_external → 04 → 05` сначала
копирует данные в новую Hive physical table. Короткий маршрут
`03_greenplum_create_external_liquibase → 04 → 05` передаёт функции исходную
`<greenplum.original_hive_database>.<source_table>` и поэтому не использует
файлы `01`–`02`. Эти два маршрута нельзя смешивать. Hive-файлы выполняются в
Hive, Greenplum-файлы — после подключения к `greenplum.database`, а
Liquibase-файл — через Liquibase. Утилита execution context не переключает.
Для полного маршрута нужно самостоятельно включить все перечисленные роли.
Частичный набор допустим, если пропущенные объекты или data steps уже созданы
либо управляются отдельно; локально проверить это состояние невозможно.

Если `output.filename_separator` изменён, вместо `__` используется заданный
разделитель; названия включённых ролей и их порядок не меняются.

Legacy-артефакты `create`, `drop`, `describe`, `show-create` и `analyze` больше
не генерируются. Если Liquibase-роль включена, единственный `DROP` находится
внутри управляемого Liquibase-варианта шага 03 и всегда имеет форму
`DROP EXTERNAL TABLE IF EXISTS ... CASCADE`. Исходный `CREATE TABLE` не
копируется.

Скрипты рассчитаны на новые, ещё не существующие targets и не являются
идемпотентными: включённые роли `CREATE` генерируются без `IF NOT EXISTS`. Если
существующие targets сохранить и вручную повторить только DML, Hive
`INSERT INTO` и Greenplum `INSERT INTO` допишут строки. Hive
`INSERT OVERWRITE` заменит данные только в Hive target; Greenplum INSERT всё
равно останется append-операцией.

### 1. Hive physical table

Первый файл содержит обычный `CREATE TABLE`, без `TEMPORARY`, `IF NOT EXISTS`,
`EXTERNAL`, `LOCATION`, source storage, Spark properties, constraints,
clustering, sorting, bucketing, skewing и partitioning. Target database и имя
таблицы вычисляются из version 3 config. Namespace считается существующим и не
создаётся.

Колонки сохраняют source-порядок и Hive-типы, включая параметры
`DECIMAL(p,s)`, `CHAR(n)` и `VARCHAR(n)`. Storage новой таблицы задаётся только
`hive.storage`.

### 2. Hive INSERT

Второй файл использует:

- `INSERT INTO TABLE` при `hive.insert_mode: into`;
- `INSERT OVERWRITE TABLE` при `hive.insert_mode: overwrite`.

При `into` target и source columns перечисляются явно и в одинаковом порядке.
При `overwrite` грамматика Hive не допускает target column list, поэтому
целевая колонка определяется позицией в детерминированном порядке колонок
сгенерированного `CREATE TABLE`; `SELECT` по-прежнему перечисляет все source
columns явно и в том же порядке. `SELECT *` не используется ни в одном режиме.

### 3. Greenplum external table

Третий файл создаёт `CREATE EXTERNAL TABLE` с явным mapped column list.
`LOCATION` строится из `greenplum.external.location_template` и всегда
подставляет имя новой физической Hive-таблицы, а не source-таблицы. Шаблон
имеет фиксированную форму ресурса
`pxf://prx_{subscription}_{original_hive_database}.{hive_table}` и ровно два
query-параметра:
`PROFILE={profile}` и `SERVER={server}` (в любом порядке).

Для стандартной конфигурации и `customer_orders` получится:

```text
pxf://prx_subscription_original_hive_database.replica_customer_orders_physical?PROFILE=hive&SERVER=default
```

Location template хранится в config, но его структура фиксирована validator:
менять можно значения subscription, original Hive database, PXF server и
порядок двух query-параметров. `{subscription}` получает значение
`greenplum.subscription`, `{original_hive_database}` —
`greenplum.original_hive_database`, а `{hive_table}` — вычисленное имя Hive
target с подставленным `hive.replica`. Другие resource paths, query parameters
и profiles текущая schema не принимает. End-to-end-контракт разрешает только
Hive profile и:

```sql
LOCATION (
  E'pxf://prx_subscription_original_hive_database.replica_customer_orders_physical?PROFILE=hive&SERVER=default'
) ON ALL
FORMAT E'CUSTOM' (FORMATTER=E'pxfwritable_import')
ENCODING 'UTF8';
```

Ресурс PXF использует database alias
`prx_<greenplum.subscription>_<greenplum.original_hive_database>`. Это имя не
выводится ни из source `CREATE TABLE`, ни из `hive.target_database`: оба его
фрагмента явно задаются в config. Контракт предполагает, что целевая
PXF/Hive-среда уже публикует такой alias и направляет его в
`hive.target_database`, где создаётся `{hive_table}`. Утилита alias не создаёт
и не проверяет.

Это согласовано с чтением настроенной Hive `TEXTFILE`-таблицы через PXF Hive
profile. `ON ALL` и UTF-8 encoding являются фиксированной частью
сгенерированного Greenplum external DDL. Другие сочетания
storage/profile/external format отклоняются, чтобы не создавать
правдоподобный, но неподдержанный SQL.

### 4. Greenplum physical table

Четвёртый файл создаёт обычную `CREATE TABLE` в target schema. Текущая
детерминированная distribution policy — `DISTRIBUTED RANDOMLY`. Database,
schema и дополнительные структуры не создаются.

### 5. Greenplum INSERT

Пятый файл использует `INSERT INTO ... SELECT ... FROM ...` и явно перечисляет
одинаковый ordered column list с обеих сторон. `SELECT *`, `DROP`, `TRUNCATE`
и `ANALYZE` не добавляются.

### 3b. Liquibase external table

Liquibase-альтернатива начинается строго с `--liquibase formatted sql` и
содержит один changeset с `runOnChange:true splitStatements:false`. Она удаляет
вычисленную external table через `DROP EXTERNAL TABLE IF EXISTS ... CASCADE`,
затем вызывает `<greenplum.external_schema>.<function_name>(JSON)`.

JSON содержит `schema_name`, вычисленное `table_name`, logical source
`<greenplum.original_hive_database>.<source_table>` и все ordinary/partition
columns в исходном порядке. Для каждой колонки `description` получает текст её
source `COMMENT`; если clause отсутствует, используется `"-"`. Явно заданный
пустой `COMMENT ''` остаётся пустой строкой. Типы функции нормализуются
отдельно: string/char/varchar → `text`, decimal/numeric → `numeric`,
integer-типы → `int2`/`int4`/`int8`, float/double → `float4`/`float8`, boolean →
`bool`, date/timestamp сохраняются.

Оба варианта шага 03 создают один Greenplum target, но читают разные Hive
relations: прямой PXF-вариант — новую physical table из шагов 01–02,
Liquibase-вариант — исходную таблицу. Выполнять их вместе нельзя. Для
Liquibase-варианта функция должна быть заранее установлена в
`greenplum.external_schema`; утилита её наличие не проверяет.

## Partition columns и comments

Source-модель разделяет:

1. обычные колонки из основного column list;
2. колонки из `PARTITIONED BY`.

В обоих физических target эти списки flatten-ятся в один:

```text
ordinary columns в исходном порядке
→ бывшие partition columns в исходном порядке
```

Бывшие partition columns становятся обычными физическими колонками. При
включённых соответствующих ролях они присутствуют в обоих `CREATE`, Hive
`INSERT`, Greenplum external table, Liquibase JSON и Greenplum `INSERT`;
`PARTITIONED BY` в target отсутствует. Все имена в общем списке обычных и
partition columns должны быть уникальны после Unicode NFKC normalization и
сравнения без учёта регистра; коллизия отклоняется до записи.

Семантические comments переносятся так:

- column comments, включая comments бывших partition columns, остаются inline
  в Hive column definitions;
- table comment остаётся Hive `COMMENT` clause;
- Liquibase JSON записывает column comment в `columns[].description`, а при
  отсутствии source column `COMMENT` — `"-"`; table comment в payload не входит;
- Greenplum external и physical artifacts добавляют `COMMENT ON TABLE` и
  `COMMENT ON COLUMN` после соответствующего `CREATE`.

Апострофы, Unicode и identifiers экранируются отдельно для каждого диалекта:
Hive использует backticks, Greenplum — double quotes. Liquibase descriptions
сначала JSON-encode-ятся, затем весь payload экранируется как Greenplum string
literal. Динамические Greenplum strings выводятся как explicit `E'...'`: каждый
backslash и apostrophe экранируется независимо от `standard_conforming_strings`;
Unicode сохраняется. NUL в comments отклоняется до генерации.
Произвольные исходные SQL-комментарии `-- ...` и `/* ... */` не переносятся.

## Hive → Greenplum type mapping

Mapping явный, регистронезависимый и fail-closed:

| Hive type | Greenplum type |
| --- | --- |
| `STRING` | `TEXT` |
| `VARCHAR(n)` | `VARCHAR(n)` |
| `CHAR(n)` | `CHAR(n)` |
| `TINYINT` | `SMALLINT` |
| `SMALLINT` | `SMALLINT` |
| `INT` | `INTEGER` |
| `INTEGER` | `INTEGER` |
| `BIGINT` | `BIGINT` |
| `FLOAT` | `REAL` |
| `REAL` | `REAL` |
| `DOUBLE` | `DOUBLE PRECISION` |
| `DOUBLE PRECISION` | `DOUBLE PRECISION` |
| `DECIMAL` / `NUMERIC` | `NUMERIC(10,0)` (Hive default precision/scale) |
| `DECIMAL(p)` / `NUMERIC(p)` | `NUMERIC(p,0)` |
| `DECIMAL(p,s)` / `NUMERIC(p,s)` | `NUMERIC(p,s)` |
| `BOOLEAN` | `BOOLEAN` |
| `DATE` | `DATE` |
| `TIMESTAMP` | `TIMESTAMP` |

Hive `DECIMAL`/`NUMERIC` precision ограничен диапазоном `1..38`, scale не
может превышать precision. `CHAR` допускает длину `1..255`, `VARCHAR` —
`1..65535`. Numeric type parameter ограничен 32 ASCII-цифрами до преобразования
в число, а вложенность complex types — 100 уровнями; превышение обоих лимитов
считается malformed DDL.

Не поддерживаются и не преобразуются молча в `TEXT`:

- `ARRAY`, `MAP`, `STRUCT`, `UNIONTYPE`;
- `VOID`;
- `BINARY`;
- `TIMESTAMPLOCALTZ`, `TIMESTAMP_LTZ`,
  `TIMESTAMP WITH LOCAL TIME ZONE`;
- `INTERVAL_DAY_TIME`, `INTERVAL_YEAR_MONTH`;
- неизвестные custom types;
- любой другой тип, отсутствующий в таблице mapping.

Ошибка содержит source table, column name, исходный Hive type и причину.
Mapping всех таблиц проверяется до записи, поэтому такая ошибка не оставляет
частичный набор SQL-файлов.

## TEXTFILE — не RFC CSV

Единственный поддержанный end-to-end target storage сейчас:

```sql
ROW FORMAT DELIMITED
  FIELDS TERMINATED BY ...
  ESCAPED BY ...
  NULL DEFINED AS ...
STORED AS TEXTFILE
```

Это delimiter-based Hive TEXTFILE, а не полноценный RFC CSV. Он не гарантирует
RFC-совместимую обработку quoted delimiters, quoted переносов строк и всех
остальных CSV edge cases. Delimiter и escape должны быть разными печатными
ASCII-символами, но не цифрами. Null marker должен быть печатной ASCII-строкой
длиной `1..32`, содержать escape-символ, не заканчиваться им и не содержать
delimiter. Это консервативный контракт validator, предназначенный для
различения SQL `NULL` и совпадающего non-null текста. Утилита не выполняет
сериализацию и не проверяет это допущение на конкретных версиях Hive/PXF,
поэтому выбирайте значения с учётом данных и проверяйте их в целевой среде.
ORC, Parquet и другие target formats отклоняются, пока они не поддержаны
согласованно всей цепочкой до Greenplum.

Source при этом может быть Parquet или использовать SerDe/InputFormat:
source storage разбирается, но не копируется.

## Version 3 config

Путь к YAML-конфигурации обязателен для `generate`, `validate` и `inspect`.
Автоматического поиска config нет. Все показанные ниже поля обязательны, кроме
необязательной partial mapping `output.artifacts`; неизвестные, отсутствующие
обязательные и повторяющиеся mapping keys на любом уровне отклоняются.

Загрузка YAML ограничена размером `1 MiB`, глубиной `64` уровня и `128`
символами в representation одного integer. Неявное распознавание YAML
date/timestamp отключено: такие plain scalars обрабатываются как строки и затем
проходят обычную проверку schema. Ошибки YAML constructors нормализуются в
безопасную CLI-диагностику без traceback и абсолютного host path.

[`config/table-factory.yaml`](config/table-factory.yaml) — пользовательский
runtime-конфиг и пример для локального запуска. Его можно менять под конкретное
окружение, включая database, schema, replica и PXF alias; тесты от его значений
не зависят.

Пример runtime-конфига:

```yaml
version: 3

output:
  include_source_comment: true
  filename_separator: "__"
  artifacts:
    "01_hive_create_physical": true
    "02_hive_insert": true
    "03_greenplum_create_external": true
    "03_greenplum_create_external_liquibase": true
    "04_greenplum_create_physical": true
    "05_greenplum_insert": true

hive:
  target_database: target_hive_db
  replica: replica
  physical_table_name_template: "{replica}_{source_table}_physical"
  insert_mode: into
  storage:
    format: textfile
    field_delimiter: ","
    escape_character: "\\"
    null_value: "\\N"

greenplum:
  database: target_gp_database
  external_schema: ext
  physical_schema: dwh
  replica: replica
  subscription: subscription
  original_hive_database: original_hive_database
  external_table_name_template: "{replica}_{source_table}_ext"
  physical_table_name_template: "{replica}_{source_table}"
  distribution:
    mode: random
  external:
    location_template: "pxf://prx_{subscription}_{original_hive_database}.{hive_table}?PROFILE={profile}&SERVER={server}"
    profile: hive
    server: default
    format:
      kind: custom
      formatter: pxfwritable_import
    liquibase:
      author: "22643610"
      changeset_id_template: "stg-{source_table}_ext"
      function_name: f_create_external_table
```

Конфиги v1 и v2 больше не поддерживаются и не получают неявных defaults:
их загрузка завершается сообщением о необходимости миграции на v3.
Существующие v3-конфиги тоже нужно дополнить обязательными полями
`greenplum.subscription`, `greenplum.original_hive_database` и секцией
`greenplum.external.liquibase`, а старый PXF-шаблон заменить показанным выше
новым контрактом.

### Поля config

| Поле | Тип и допустимые значения | Назначение |
| --- | --- | --- |
| `version` | integer, только `3` | Версия несовместимой v3 schema |
| `output.include_source_comment` | boolean | Добавлять безопасный header с basename source-файла |
| `output.filename_separator` | string из `.`/`_`/`-`, длина `1..8` | Разделять portable stem и стабильную роль файла |
| `output.artifacts.<role>` | boolean; необязательный override, effective default `true` | Генерировать файл соответствующей стабильной роли для каждой source table |
| `hive.target_database` | непустой unqualified identifier | Database новой физической Hive table |
| `hive.replica` | ASCII fragment `[A-Za-z0-9_][A-Za-z0-9_-]*` | Значение `{replica}` в Hive name template |
| `hive.physical_table_name_template` | безопасный name template | Имя новой Hive table |
| `hive.insert_mode` | `into` или `overwrite` | Режим Hive INSERT |
| `hive.storage.format` | только `textfile` | End-to-end storage новой Hive table |
| `hive.storage.field_delimiter` | один printable non-digit ASCII character | `FIELDS TERMINATED BY` |
| `hive.storage.escape_character` | один printable non-digit ASCII character, отличный от delimiter | `ESCAPED BY` |
| `hive.storage.null_value` | printable ASCII string длиной `1..32`, содержащая escape, но не в конце, и без delimiter | `NULL DEFINED AS` |
| `greenplum.database` | непустой unqualified identifier, не более 63 UTF-8 bytes | Database execution/connection context |
| `greenplum.external_schema` | непустой unqualified identifier, не более 63 UTF-8 bytes | Schema external table |
| `greenplum.physical_schema` | непустой unqualified identifier, не более 63 UTF-8 bytes | Schema physical table |
| `greenplum.replica` | ASCII fragment `[A-Za-z0-9_][A-Za-z0-9_-]*` | Значение `{replica}` в Greenplum name templates |
| `greenplum.subscription` | ASCII fragment `[A-Za-z0-9_][A-Za-z0-9_-]*` | Значение `{subscription}` в PXF LOCATION |
| `greenplum.original_hive_database` | непустой unqualified identifier | `{original_hive_database}` в PXF LOCATION и database для Liquibase `source_table` |
| `greenplum.external_table_name_template` | безопасный name template | Имя external table |
| `greenplum.physical_table_name_template` | безопасный name template | Имя physical table |
| `greenplum.distribution.mode` | только `random` | `DISTRIBUTED RANDOMLY` |
| `greenplum.external.location_template` | безопасный `pxf://...` template | PXF LOCATION новой Hive table |
| `greenplum.external.profile` | только `hive`; регистр input не учитывается, effective value нормализуется в `hive` | PXF profile поддержанного workflow |
| `greenplum.external.server` | ASCII token `[A-Za-z0-9_][A-Za-z0-9_.-]*` | Имя PXF server целевой среды |
| `greenplum.external.format.kind` | только `custom` | Greenplum external format |
| `greenplum.external.format.formatter` | только `pxfwritable_import` | PXF custom formatter |
| `greenplum.external.liquibase.author` | безопасный ASCII token | Author в `--changeset author:id` |
| `greenplum.external.liquibase.changeset_id_template` | безопасный changeset template | ID Liquibase changeset для каждой source table |
| `greenplum.external.liquibase.function_name` | непустой unqualified identifier, не более 63 UTF-8 bytes | Функция создания external table в `greenplum.external_schema` |

Допустимые ключи `output.artifacts` — точные role suffixes из таблицы выходных
файлов: `01_hive_create_physical`, `02_hive_insert`,
`03_greenplum_create_external`, `03_greenplum_create_external_liquibase`,
`04_greenplum_create_physical` и `05_greenplum_insert`. Неуказанные роли
остаются включёнными; неизвестные ключи и значения не типа boolean
отклоняются. Все шесть `false` допустимы: `generate` выполняет подготовку,
создаёт output-директорию и сообщает `Generated 0 SQL files`.

Database и schema, включая `greenplum.original_hive_database`, должны быть
одиночными identifiers из ASCII letters, digits и underscore, начинаться с
letter или underscore и не содержать точки. Ограничение 63 bytes в UTF-8
применяется к `greenplum.database`, `greenplum.external_schema`,
`greenplum.physical_schema`, вычисленным Greenplum table names и column names.

Оба поля `replica` и поле `greenplum.subscription` обязательны и должны быть
непустыми ASCII-фрагментами: первый символ — letter, digit или underscore,
остальные — letters, digits, underscores или hyphens. Точки, URI separators и
braces запрещены; subscription не может изменить границы `database.table` в
PXF resource.

Значения `hive.storage.format`, `hive.insert_mode`,
`greenplum.distribution.mode`, `greenplum.external.format.kind` и
`greenplum.external.format.formatter` принимаются без учёта регистра и
нормализуются в lowercase. Profile также нормализуется в каноническое
значение `hive`.

Name templates разрешают literal-фрагменты только из ASCII
`[A-Za-z0-9_]*` и placeholders:

- `{replica}`;
- `{source_database}`;
- `{source_table}`.

В `hive.physical_table_name_template` `{replica}` означает `hive.replica`.
В `greenplum.external_table_name_template` и
`greenplum.physical_table_name_template` он означает `greenplum.replica`.
Неизвестные placeholders, format specs, conversions и другие literal
characters отклоняются. Например, literal hyphen в
`{source_table}-physical` недопустим. Unicode может попасть в результат только
из source placeholder, а hyphen — из source или replica placeholder.
Результат шаблона может содержать только Unicode letters/digits, underscore и
hyphen, в том числе начинаться с цифры: target identifier всегда корректно
quoted. Backticks расширяют синтаксис source identifier, но не допустимый
алфавит target: если подставленное source-значение содержит другие символы,
semantic preflight отклонит результат. Поддерживаются, например, source
identifiers `` `orders-2026` `` и `` `2026_orders` ``. Если source не
квалифицирован database, шаблон с `{source_database}` использовать нельзя.
Форма `` `database.table` `` считается квалифицированной, и
`{source_database}` получает её левую часть.
Вычисленные Greenplum table names дополнительно ограничены 63 UTF-8 bytes.

`greenplum.external.liquibase.changeset_id_template` допускает literal symbols
из `[A-Za-z0-9_.-]` и placeholders `{replica}`, `{source_database}`,
`{source_table}`, `{external_table}`. Результат обязан состоять только из
Unicode letters/digits, `_`, `.` и `-`; whitespace, `:`, SQL/Liquibase options
и управляющие символы отклоняются. Если используется `{source_database}`, все
source tables должны быть квалифицированы database.

`greenplum.external.location_template`:

- должен иметь ресурс ровно
  `pxf://prx_{subscription}_{original_hive_database}.{hive_table}`;
- обязан содержать только два query-параметра:
  `PROFILE={profile}` и `SERVER={server}`; допускается любой их порядок;
- не допускает credentials, дополнительных query-параметров, других
  placeholders, format specs или произвольных SQL fragments.

После подстановки database component имеет вид
`prx_<greenplum.subscription>_<greenplum.original_hive_database>`. Это
PXF-visible alias, который deployment должен заранее сопоставить с Hive
database `hive.target_database`. Поле `original_hive_database` не извлекается
из source DDL и не меняется между таблицами одного запуска; совпадение и
существование alias локально не проверяются.

В config нет полей credentials. Не помещайте secrets в URI или name
templates.

## Правила target naming

При стандартном config source `analytics.customer_orders` превращается в:

| Объект | Вычисленное имя |
| --- | --- |
| Hive physical | `` `target_hive_db`.`replica_customer_orders_physical` `` |
| Greenplum external | `"ext"."replica_customer_orders_ext"` |
| Greenplum physical | `"dwh"."replica_customer_orders"` |
| Greenplum execution database | `target_gp_database` |
| PXF resource | `pxf://prx_subscription_original_hive_database.replica_customer_orders_physical` |

Каждый Hive target сравнивается со всеми source tables текущей партии, а не
только со своим source. Поэтому target одной таблицы не может совпасть с
source другой таблицы, включая совпадения после Unicode normalization и без
учёта регистра. Для source без database совпадение по table name отклоняется
консервативно, поскольку execution database неизвестна.
Greenplum external и physical table не могут иметь одинаковые table name в
одной schema. Коллизии targets и output filenames между несколькими source
tables сравниваются с Unicode normalization и без учёта регистра до записи.

Filename stem строится из source qualified name, а не target name. Точка и
небезопасные символы заменяются `_`, Unicode нормализуется, повторные `_`
схлопываются, а stem обрезается до 160 UTF-8 bytes. Поэтому
`analytics.customer_orders` получает stem `analytics_customer_orders`.

## Greenplum database и schema

`greenplum.database` — database, к которой исполнитель должен подключиться
перед запуском Greenplum-скриптов. Это execution context, а не часть имени
таблицы.

Сгенерированный SQL использует корректные двухчастные имена:

```sql
"ext"."replica_customer_orders_ext"
"dwh"."replica_customer_orders"
```

Он не генерирует неподдержанное
`"database"."schema"."table"`. Schema является частью qualified table name,
database — нет. Утилита не создаёт database или schema: все target namespaces
должны существовать заранее.

## CLI

Общая справка:

```bash
docker compose run --rm table-factory --help
```

Все относительные CLI paths вычисляются от текущей рабочей директории.

### `generate`

```bash
docker compose run --rm table-factory generate \
  --input ./work/input \
  --output ./work/output \
  --config ./config/table-factory.yaml
```

Команда выполняет полный parse, semantic validation, type mapping, render и
collision validation, а затем пишет артефакты. При успехе сообщает число
созданных SQL-файлов: число включённых ролей × число source tables. При
стандартном config это шесть файлов на таблицу.

### `validate`

```bash
docker compose run --rm table-factory validate \
  --input ./work/input \
  --config ./config/table-factory.yaml
```

Команда выполняет ту же подготовку без записи. Проверяются все input DDL,
comments, partition columns, target names, config compatibility, Hive storage,
PXF settings, type mapping и collisions.

### `inspect`

```bash
docker compose run --rm table-factory inspect \
  ./work/input/example.sql \
  --config ./config/table-factory.yaml
```

`inspect` принимает один SQL-файл и выводит UTF-8 JSON, содержащий:

- безопасный source path label без абсолютного host path;
- полную effective version 3 config, включая все шесть artifact-флагов;
- source database, table, qualified name и `external` flag;
- отдельные ordinary и partition columns, Hive types и comments;
- table comment;
- вычисленные Hive, Greenplum external и Greenplum physical targets;
- Greenplum execution database;
- mapped Greenplum types и признак бывшей partition column.

Все три команды возвращают `0` при успехе. Ожидаемые ошибки конфигурации, DDL,
mapping и файловых операций возвращают `2`, печатаются в `stderr` без
traceback и не раскрывают абсолютные host paths.

## Безопасность файлов и детерминизм

- Пользовательские symlink input, вложенные symlink directories/files и
  symlink-компоненты input path отклоняются; то же правило действует для
  output directory и её родительских компонентов. Стабильные root-owned
  системные aliases вроде macOS `/var` и `/tmp` разрешены.
- Input tree закрепляется directory descriptor-ами; каждый SQL-файл открывается
  относительно проверенного parent descriptor с `O_NOFOLLOW`, сверяется через
  `fstat` и читается из того же file descriptor. Поэтому замена уже принятого
  pathname не перенаправляет чтение, а filesystem identity фактически
  прочитанного файла сохраняется для последующих проверок.
- Сгенерированное имя является только filename и не может выйти из output
  через path traversal.
- Ни один destination не может быть тем же файлом, что и прочитанный input
  DDL. Проверяется как совпадение пути, так и filesystem identity через
  `samefile`, поэтому hard-link alias входного файла также не перезаписывается.
- Защита `samefile` относится к прочитанным DDL, но не к YAML config или
  посторонним файлам. Любой существующий non-directory destination с именем
  генерируемого артефакта считается заменяемым. Не храните config и другие
  важные файлы в output под такими именами.
- `--output` не исключается из рекурсивного поиска directory `--input`.
  Output не должен совпадать с input directory или находиться внутри неё:
  следующий запуск воспримет сгенерированные `.sql` как новые входные
  документы. Для одиночного input-файла это ограничение не требуется.
- Все input-файлы разбираются, target/type mapping и collisions проверяются до
  записи.
- Output directory открывается component-by-component и закрепляется directory
  descriptor-ом. Временные файлы создаются с `O_EXCL | O_NOFOLLOW`, а
  `replace`, rollback и cleanup выполняются только относительно закреплённого
  descriptor. Переименование принятого output и установка symlink на его
  прежнем pathname не могут перенаправить запись.
- Полный включённый набор артефактов сначала записывается во временные файлы,
  затем destinations заменяются последовательными атомарными `os.replace`.
  Набор в целом не атомарен для параллельного читателя и не является
  crash-safe. Rollback предполагает единственного writer-а: конкурентное
  изменение имён внутри уже закреплённого output directory не сериализуется.
- При ошибке записи выполняется best-effort rollback, включая попытку
  восстановить существовавшие файлы. Если filesystem отказывает и при
  rollback или cleanup, CLI возвращает контролируемую ошибку без traceback, а
  временный или recovery backup может остаться для ручного восстановления.
- SQL headers содержат только безопасный basename источника.
- Повторная генерация при неизменном наборе реально прочитанных input-файлов и
  том же config побайтово детерминирована.
- Отключённые renderer-ы не вызываются, но общие parse, type/target mapping и
  collision checks выполняются даже при всех шести `false`.
- Файлы, не входящие в текущий включённый набор артефактов, не удаляются.

Число файлов равно числу включённых ролей, умноженному на число source tables.
По умолчанию при двух таблицах создаётся двенадцать артефактов, при трёх —
восемнадцать. Несколько таблиц могут находиться в одном файле или в разных
файлах рекурсивного input tree.

## Проверки проекта

Все основные проверки запускаются из корня репозитория без target Hive,
Greenplum или PXF. На Unix используйте `LOCAL_UID`/`LOCAL_GID`, экспортированные
в разделе быстрого старта:

Функциональные и golden-тесты используют отдельный
[`tests/fixtures/table-factory.yaml`](tests/fixtures/table-factory.yaml).
Пользовательский `config/table-factory.yaml` читает только contract-проверка
валидности v3, не фиксирующая конкретные database, schema или replica. Поэтому
изменение этих runtime-значений не должно ломать тесты.

```bash
docker compose config --quiet
```

```bash
docker compose run --rm \
  --entrypoint pytest \
  table-factory -q
```

```bash
docker compose run --rm \
  --entrypoint ruff \
  table-factory check .
```

```bash
docker compose run --rm \
  --entrypoint ruff \
  table-factory format --check .
```

```bash
docker compose run --rm \
  --entrypoint mypy \
  table-factory src
```

Сборка wheel и sdist:

```bash
docker compose run --rm \
  --entrypoint python \
  table-factory -m build
```

Release sdist содержит runtime-пакет и документацию, но намеренно не включает
набор тестов из репозитория: contract-тесты зависят от fixtures, Docker/Compose
и других файлов полного checkout. Для запуска тестов используйте исходный
репозиторий, а не распакованный sdist.

После изменения `pyproject.toml`, зависимостей или `Dockerfile` сначала
пересоберите development image:

```bash
docker compose build table-factory
```

Docker acceptance tests, которые сами управляют Docker, запускаются с хоста
после установки dev dependencies:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
TABLE_FACTORY_RUN_DOCKER_TESTS=1 pytest -q tests/test_docker_runtime.py
```

## Установка и запуск без Docker

Нативный CLI поддерживается только на POSIX-системах с безопасными
descriptor-relative файловыми операциями (в частности, Linux и macOS) и
требует Python 3.12 или новее. На Windows используйте Docker-first запуск из
раздела «Быстрый старт»: нативная установка wheel там не поддерживается.
Метка wheel `py3-none-any` означает отсутствие бинарных расширений, но не
расширяет этот явно объявленный runtime-контракт на Windows.

Установка wheel на поддерживаемой POSIX-системе:

```bash
VERSION=0.1.0
python -m pip install "dist/table_factory-${VERSION}-py3-none-any.whl"
```

Доступна та же реализация CLI:

```bash
table-factory validate \
  --input ./input \
  --config ./table-factory.yaml

table-factory generate \
  --input ./input \
  --output ./output \
  --config ./table-factory.yaml
```

Runtime-зависимость `PyYAML` устанавливается из metadata wheel. Ни Docker-, ни
wheel-вариант CLI не подключается к target systems.

## Ограничения

- SQL генерируется, но не выполняется и не проверяется на реальных кластерах.
- Утилита не обращается к catalog и не знает, существуют ли target tables вне
  текущей input-партии. До исполнения убедитесь, что вычисленные targets
  свободны.
- Target Hive storage ограничен `TEXTFILE`; external contract ограничен PXF
  Hive profile с `CUSTOM/pxfwritable_import`.
- Greenplum distribution policy ограничена `DISTRIBUTED RANDOMLY`.
- Target Hive/Greenplum database и schemas должны существовать заранее.
- `output.artifacts` не проверяет dependency closure выбранного маршрута:
  частичный набор безопасен только при заранее подготовленных пропущенных
  объектах и data steps.
- PXF/Hive deployment должен заранее публиковать database alias
  `prx_<greenplum.subscription>_<greenplum.original_hive_database>` и
  сопоставлять его с `hive.target_database`; утилита не создаёт и не проверяет
  это сопоставление.
- Если Liquibase-роль включена и её SQL исполняется, функция
  `greenplum.external.liquibase.function_name` должна существовать в
  `greenplum.external_schema` и принимать документированный JSON-контракт.
  Утилита не подключается к Greenplum и не проверяет функцию или permissions.
- Включённый Liquibase-артефакт содержит
  `DROP EXTERNAL TABLE IF EXISTS ... CASCADE` и может удалить зависящие от
  external table объекты. Его следует применять только в контролируемом
  Liquibase deployment вместо прямого PXF-файла 3.
- Hive `INSERT INTO` использует target column list; `INSERT OVERWRITE`
  использует детерминированный порядок target DDL и явный source `SELECT`,
  поскольку target column list в этой форме грамматикой Hive не поддерживается.
- В config настраиваются replicas, subscription, original Hive database, PXF
  server и порядок двух query-параметров, но не структура URI, profile,
  formatter или дополнительные параметры. Если deployment требует другой
  контракт, текущую schema и renderer нужно расширить; сгенерированный SQL
  необходимо проверить в целевой среде.
- Clause `LOCATION (...) ON ALL` зафиксирован для целевого Greenplum/ADB
  dialect. Он не универсален для всех версий и дистрибутивов Greenplum.
- Утилита не проверяет содержимое строк на совместимость с выбранными
  delimiter, escape и null marker.
- Source modifiers, constraints, partitioning, storage, `LOCATION`, properties,
  clustering, sorting, bucketing и skewing в target не переносятся; переносятся
  только колонки, Hive-типы и семантические table/column comments.
- Сгенерированные включёнными ролями `CREATE` не имеют `IF NOT EXISTS`; Hive
  `INSERT INTO` и Greenplum INSERT имеют append-семантику. Набор предназначен
  для контролируемого однократного запуска на свободные target names.
- При directory input output не должен совпадать с ним или быть его
  поддиректорией; это ограничение пока не проверяется автоматически.
- Непустая output-директория не очищается; отключение artifact-роли не удаляет
  созданный ею ранее файл.

## Структура реализации

Основной поток:

```text
CLI
→ strict config validation
→ recursive discovery/read all inputs
→ Hive parse
→ target naming и type mapping
→ source/target collision validation
→ Hive/Greenplum/Liquibase render
→ output-name и destination/input collision validation
→ staged replacement с best-effort rollback
```

Ответственность разделена между `config.py`, `parser.py`, `models.py`,
`naming.py`, `type_mapper.py`, `hive_renderer.py`,
`greenplum_renderer.py`, `liquibase_renderer.py`, `sql.py`, `path_safety.py`, `generator.py` и
`workflow.py`.

## Лицензия

Проект распространяется по лицензии
[`Apache License 2.0`](LICENSE).
