# table-factory

`table-factory` — офлайн CLI для подготовки SQL-цепочки переноса данных из
существующей Hive-таблицы в Greenplum через PXF.

На вход подаётся DDL из `SHOW CREATE TABLE`, на выходе получается
детерминированный набор SQL-файлов. Утилита работает только с локальными
файлами: она не подключается к Hive, Hadoop, Greenplum или PXF, не выполняет
SQL и не изменяет source-таблицу.

Direct PXF-маршрут выглядит так:

```text
source Hive table (read-only)
        │
        ▼
physical Hive table (TEXTFILE)
        │
        ▼
Greenplum external table (PXF)
        │
        ▼
physical Greenplum table
```

## Быстрый старт

Рекомендуемый способ запуска — Docker Compose v2. На Unix передайте UID/GID,
чтобы сгенерированные файлы принадлежали текущему пользователю:

```bash
export LOCAL_UID="$(id -u)"
export LOCAL_GID="$(id -g)"

cp examples/example.sql work/input/example.sql
docker compose build table-factory

docker compose run --rm table-factory validate \
  --input work/input \
  --config config/table-factory.yaml

docker compose run --rm table-factory generate \
  --input work/input \
  --output work/output \
  --config config/table-factory.yaml
```

Результат останется в `work/output/` на хосте. Перед рабочим запуском проверьте
target database, schemas, naming templates и PXF-параметры в
[`config/table-factory.yaml`](config/table-factory.yaml).

Compose использует `1000:1000`, если `LOCAL_UID` и `LOCAL_GID` не заданы.
Нулевые значения намеренно не поддерживаются: контейнер не запускает CLI от
root.

Первая сборка image может скачать базовый image и Python-пакеты. После сборки
сами команды CLI не требуют сети.

## Что генерируется

По умолчанию для каждой source-таблицы создаются шесть файлов:

| Роль | Назначение |
| --- | --- |
| `01_hive_create_physical` | Создать непартиционированную Hive TEXTFILE-таблицу |
| `02_hive_insert` | Перенести строки из source Hive в новую Hive-таблицу |
| `03_greenplum_create_external` | Создать Greenplum external table напрямую через PXF |
| `03_greenplum_create_external_liquibase` | Альтернативно создать ту же external table через Liquibase-функцию |
| `04_greenplum_create_physical` | Создать физическую Greenplum-таблицу |
| `05_greenplum_insert` | Перенести строки из external table в физическую Greenplum-таблицу |

Имя файла строится из безопасного представления qualified source name,
разделителя из config и роли. Например:

```text
analytics_customer_orders__01_hive_create_physical.sql
analytics_customer_orders__02_hive_insert.sql
analytics_customer_orders__03_greenplum_create_external.sql
analytics_customer_orders__03_greenplum_create_external_liquibase.sql
analytics_customer_orders__04_greenplum_create_physical.sql
analytics_customer_orders__05_greenplum_insert.sql
```

Два файла шага `03` — взаимоисключающие варианты одного действия. При
исполнении выберите один маршрут:

```text
direct PXF:  01 → 02 → 03_greenplum_create_external           → 04 → 05
Liquibase:             03_greenplum_create_external_liquibase → 04 → 05
```

Liquibase payload берёт database из
`greenplum.original_hive_database`, а имя таблицы — из входного DDL; source
database из DDL в этом маршруте не используется. Поэтому файлы `01`–`02`
Liquibase-маршруту не нужны. Его артефакт также содержит
`DROP EXTERNAL TABLE IF EXISTS ... CASCADE` и должен применяться только в
контролируемом deployment. Ненужные роли можно отключить через
`output.artifacts` в config. CLI не включает зависимые шаги автоматически:
частичный набор корректен только при заранее подготовленных пропущенных
объектах или data steps.

## Входной DDL

Input может быть одним SQL-файлом или директорией. Директория обходится
рекурсивно; читаются UTF-8 файлы с расширением `.sql` без учёта регистра.
Один файл может содержать несколько `CREATE TABLE`.

Основные лимиты партии: 64 уровня вложенности, 1024 SQL-файла, 8 MiB на файл,
64 MiB суммарно, 4096 таблиц, 65 536 колонок и 16 384 включённых артефакта.
Размер config ограничен 1 MiB.

Поддерживаются обычные и external Hive-таблицы, qualified и backtick-имена,
comments, partition columns и распространённые clauses из `SHOW CREATE TABLE`.
Source storage, partitioning, constraints, location и properties нужны для
разбора, но не копируются в target. Partition columns становятся обычными
колонками target-таблиц после основных колонок.

Реализованы явные Hive → Greenplum mappings для строковых, целочисленных,
floating-point, boolean, date/timestamp и decimal/numeric типов. Complex types,
`BINARY`, CTAS, `CREATE TABLE LIKE` и неподдерживаемая грамматика отклоняются
до записи output. Парсер работает fail-closed: неоднозначный DDL лучше
исправить или упростить, чем генерировать из него SQL молча.

Минимальный безопасный пример находится в
[`examples/example.sql`](examples/example.sql).

Используйте qualified source names (`database.table`). Неквалифицированное имя
поддерживается, но перед выполнением Hive INSERT тогда нужно выбрать правильную
текущую Hive database.

## Конфигурация

CLI принимает строгий YAML config версии `3`. Канонический рабочий пример —
[`config/table-factory.yaml`](config/table-factory.yaml); README намеренно не
дублирует его целиком, чтобы две копии не расходились.

Основные группы настроек:

- `output` — комментарий с source filename, разделитель имён и включённые
  артефакты;
- `hive` — target database, replica, шаблон имени, `INSERT INTO` или
  `INSERT OVERWRITE` и параметры TEXTFILE;
- `greenplum` — target database и schemas, шаблоны имён, PXF location,
  subscription/server, random distribution и Liquibase-вызов.

Шаблоны target table names поддерживают `{replica}`, `{source_database}` и
`{source_table}`; Liquibase changeset template дополнительно поддерживает
`{external_table}`. Если используется `{source_database}`, входные таблицы
должны иметь qualified names. Greenplum SQL содержит двухчастные имена
`schema.table`; запускайте его в database, указанной в config.

Неизвестные поля, пропущенные обязательные поля, дубли YAML-ключей и старые
версии config отклоняются. Команда `inspect` показывает effective config,
распознанные таблицы, target names и mappings без записи файлов.

## CLI

Ниже показан установленный CLI. При Docker-запуске добавляйте перед командой
`docker compose run --rm table-factory`, как в быстром старте.

```bash
# Проверка input, config и renderers включённых ролей без записи output
table-factory validate \
  --input INPUT_FILE_OR_DIRECTORY \
  --config CONFIG_FILE

# Проверка и генерация включённых SQL-артефактов
table-factory generate \
  --input INPUT_FILE_OR_DIRECTORY \
  --output OUTPUT_DIRECTORY \
  --config CONFIG_FILE

# JSON-представление одного SQL-файла и effective config
table-factory inspect INPUT_FILE \
  --config CONFIG_FILE
```

Относительные пути разрешаются от текущей директории. `validate` не запускает
renderers отключённых ролей, не принимает output path и не проверяет
безопасность будущей записи — это делает `generate`. Успех возвращает code
`0`, контролируемая ошибка CLI, input или config — code `2` без traceback.

## Важные ограничения

- Утилита генерирует SQL, но не проверяет его на реальных Hive, PXF или
  Greenplum-кластерах.
- Target databases и schemas выбранного маршрута должны существовать заранее;
  Greenplum physical table всегда создаётся с `DISTRIBUTED RANDOMLY`.
- Direct route ограничен Hive TEXTFILE и PXF Hive profile с
  `CUSTOM/pxfwritable_import`. PXF alias
  `prx_<subscription>_<original_hive_database>` должен быть заранее сопоставлен
  с `hive.target_database`; `LOCATION (...) ON ALL` поддерживается не всеми
  версиями и дистрибутивами Greenplum.
- Liquibase route требует настроенную функцию в external schema. CLI только
  формирует JSON-вызов и не контролирует реализацию этой функции.
- Сгенерированные `CREATE` не используют `IF NOT EXISTS`. `INSERT INTO`
  добавляет строки; повторный запуск всей цепочки не является идемпотентным.
- Output-директория не очищается. Отключение роли не удаляет файл от прошлого
  запуска; для точной проверки набора используйте новую пустую директорию.
- Набор файлов записывается через временные файлы с best-effort rollback, но не
  является общей crash-atomic транзакцией. Не запускайте несколько writers в
  одну output-директорию.
- Не размещайте output внутри input tree. Всегда просматривайте сгенерированный
  SQL перед передачей в целевую среду.

## Установка без Docker

Нативный CLI поддерживается на POSIX-системах с Python 3.12+. Он отклоняет
явно известные non-UTF-8 runtime encodings. На Windows используйте Docker.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .

table-factory --help
```

Для разработки установите extra `dev`:

```bash
python -m pip install -e ".[dev]"
```

## Проверки проекта

Основные проверки не требуют доступных Hive, Greenplum или PXF:

```bash
docker compose config --quiet
docker compose run --rm --entrypoint pytest table-factory -q
docker compose run --rm --entrypoint ruff table-factory check .
docker compose run --rm --entrypoint ruff table-factory format --check .
docker compose run --rm --entrypoint mypy table-factory src
docker compose run --rm --entrypoint python table-factory -m build
```

Docker acceptance suite запускается с хоста после установки extra `dev`:

```bash
TABLE_FACTORY_RUN_DOCKER_TESTS=1 pytest -q tests/test_docker_runtime.py
```

## Лицензия

Проект распространяется по лицензии
[`Apache License 2.0`](LICENSE).
