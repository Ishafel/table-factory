# table-factory

`table-factory` — CLI-утилита для проверки Hive `CREATE TABLE` и генерации
детерминированного набора служебных SQL-файлов.

Для каждой найденной таблицы создаются пять операций:

| Артефакт | Назначение |
| --- | --- |
| `create` | исходный `CREATE TABLE` |
| `drop` | безопасное удаление через `DROP TABLE IF EXISTS` |
| `describe` | просмотр структуры через `DESCRIBE` |
| `show-create` | получение DDL через `SHOW CREATE TABLE` |
| `analyze` | сбор статистики через `ANALYZE TABLE ... COMPUTE STATISTICS` |

Проект является обычным устанавливаемым Python-пакетом. Docker используется
только как воспроизводимая среда разработки: CLI внутри development-контейнера
и CLI из wheel вызывают одну реализацию `table_factory.cli:main`.

## Возможности

- запуск разработки без локальной установки Python;
- обработка одного `.sql`-файла или рекурсивное чтение директории;
- несколько `CREATE TABLE` в одном входном документе;
- поддержка пробелов и Unicode в путях и именах файлов;
- детерминированные и переносимые имена артефактов;
- проверка коллизий до начала записи;
- атомарная замена каждого выходного файла;
- сохранение результатов на хосте через bind mount;
- одинаковое поведение Docker- и wheel-вариантов;
- отсутствие подключений к Hive, Hadoop или базам данных.

## Требования

Для Docker-first разработки нужны только:

- Docker;
- Docker Compose v2 с командой `docker compose`.

Локальный Python для сборки, запуска CLI и проверок не требуется.

Для установки готового wheel без Docker нужен Python 3.12 или новее.

## Быстрый старт через Docker

Скопируйте безопасный пример DDL в локальную входную директорию:

```bash
cp examples/example.sql work/input/example.sql
```

Соберите development-образ:

```bash
docker compose build
```

Запустите генерацию:

```bash
docker compose run --rm table-factory generate \
  --input ./work/input \
  --output ./work/output \
  --config ./config/table-factory.yaml
```

После успешного запуска в `work/output` появятся:

```text
analytics_customer_orders__create.sql
analytics_customer_orders__drop.sql
analytics_customer_orders__describe.sql
analytics_customer_orders__show-create.sql
analytics_customer_orders__analyze.sql
```

Контейнер одноразовый, но файлы остаются на хосте, потому что корень проекта
примонтирован в `/workspace`.

## CLI

Общая справка:

```bash
docker compose run --rm table-factory --help
```

Доступны три команды:

```text
generate    проверить DDL и создать SQL-артефакты
validate    проверить DDL без записи файлов
inspect     вывести разобранную модель одного файла в JSON
```

Все относительные пути вычисляются от текущей рабочей директории. Для
предсказуемого поведения рекомендуется передавать пути в форме `./path`.

### `generate`

```bash
docker compose run --rm table-factory generate \
  --input ./work/input \
  --output ./work/output \
  --config ./config/table-factory.yaml
```

Параметры:

| Параметр | Описание |
| --- | --- |
| `--input` | один `.sql`-файл или директория с DDL |
| `--output` | директория для сгенерированных SQL-файлов |
| `--config` | YAML-конфигурация |

Директория `--input` обходится рекурсивно. Учитываются файлы с расширением
`.sql` без учёта регистра. Перед записью утилита:

1. находит все входные SQL-файлы;
2. читает их как UTF-8;
3. разбирает все `CREATE TABLE`;
4. проверяет коллизии нормализованных имён;
5. создаёт по пять файлов на таблицу.

При двух таблицах будет создано десять файлов, при трёх — пятнадцать и так
далее.

Файлы с совпадающими именами заменяются атомарно. Остальные файлы в output
автоматически не удаляются, поэтому директорию можно использовать для
накопления результатов нескольких независимых запусков.

### `validate`

```bash
docker compose run --rm table-factory validate \
  --input ./work/input \
  --config ./config/table-factory.yaml
```

Команда использует те же правила поиска и разбора, что и `generate`, но ничего
не записывает. При успехе выводится количество проверенных таблиц и файлов:

```text
Validated 1 table(s) in 1 file(s).
```

### `inspect`

```bash
docker compose run --rm table-factory inspect \
  ./work/input/example.sql \
  --config ./config/table-factory.yaml
```

`inspect` принимает ровно один SQL-файл и выводит JSON с:

- безопасным относительным именем источника;
- эффективной конфигурацией;
- именем базы и таблицы;
- квалифицированным именем;
- списком колонок и типов.

JSON выводится в UTF-8 без экранирования Unicode.

### Коды завершения

| Код | Значение |
| --- | --- |
| `0` | команда выполнена успешно |
| `2` | ошибка аргументов, конфигурации, DDL или файловой операции |

Ожидаемые ошибки выводятся в `stderr` без traceback и абсолютных путей хоста.

## Поддерживаемый Hive DDL

Парсер намеренно принимает проверяемое подмножество Hive DDL: `CREATE TABLE` с
явным непустым списком колонок.

Поддерживаются:

- `TEMPORARY`, `EXTERNAL` и `IF NOT EXISTS`;
- обычные и квалифицированные имена `database.table`;
- идентификаторы в обратных кавычках, включая экранированные двойные обратные
  кавычки;
- простые типы Hive;
- `DECIMAL`, `NUMERIC`, `CHAR` и `VARCHAR`;
- вложенные `ARRAY`, `MAP`, `STRUCT` и `UNIONTYPE`;
- column comments и распространённые column/table constraints;
- table clauses в официальном порядке:
  `COMMENT`, `PARTITIONED BY`, `CLUSTERED BY`, `SKEWED BY`, `ROW FORMAT`,
  `STORED`, `LOCATION`, `TBLPROPERTIES`.

Поддерживаемые built-in storage formats: `TEXTFILE`, `SEQUENCEFILE`, `RCFILE`,
`ORC`, `PARQUET`, `AVRO` и `JSONFILE`. Также распознаются формы
`STORED AS INPUTFORMAT ... OUTPUTFORMAT ...` и `STORED BY ...`.

Ограничения параметризованных типов:

- `DECIMAL`/`NUMERIC`: precision от `1` до `38`, scale не больше precision;
- `CHAR`: длина от `1` до `255`;
- `VARCHAR`: длина от `1` до `65535`;
- ключ `MAP` должен иметь примитивный Hive-тип.

Каждый table clause может встречаться только один раз. Неправильный порядок,
дубликаты и неподдерживаемые фрагменты приводят к ошибке валидации.

Не поддерживаются:

- `CREATE TABLE AS SELECT`;
- `CREATE TABLE LIKE`;
- DDL-команды кроме `CREATE TABLE`;
- произвольные Hive-запросы и выполнение SQL.

CTAS и `LIKE` отклоняются явно: без полноценного Hive query parser утилита не
может достоверно подтвердить их корректность.

## Выходные файлы

По умолчанию имя строится из квалифицированного имени таблицы, разделителя из
конфигурации и типа операции:

```text
<database>_<table><separator><operation>.sql
```

Для `analytics.customer_orders` и разделителя `__`:

```text
analytics_customer_orders__create.sql
analytics_customer_orders__drop.sql
analytics_customer_orders__describe.sql
analytics_customer_orders__show-create.sql
analytics_customer_orders__analyze.sql
```

Небезопасные символы заменяются, Unicode нормализуется, а длина имени
ограничивается с учётом UTF-8. Если разные таблицы после нормализации получают
одинаковое имя, генерация завершается ошибкой до создания файлов.

При включённом `include_source_comment` каждый файл начинается с комментария:

```sql
-- Generated by table-factory from example.sql
```

`create.sql` сохраняет исходный `CREATE TABLE`, включая поддерживаемые comments,
constraints и table clauses. Остальные четыре файла используют безопасно
закавыченное квалифицированное имя таблицы.

## Конфигурация

Конфигурация по умолчанию находится в
[`config/table-factory.yaml`](config/table-factory.yaml):

```yaml
version: 1
dialect: hive

output:
  include_source_comment: true
  filename_separator: "__"
```

Параметр `--config` обязателен для `generate`, `validate` и `inspect`.
Автоматического поиска конфигурации нет: путь всегда передаётся явно.

Поля:

| Поле | Тип | Значение |
| --- | --- | --- |
| `version` | integer | версия схемы; сейчас только `1` |
| `dialect` | string | SQL-диалект; сейчас только `hive` |
| `output.include_source_comment` | boolean | добавлять комментарий с источником |
| `output.filename_separator` | string | разделитель имени и операции |

`filename_separator` должен:

- быть непустой строкой;
- содержать не более восьми символов;
- состоять только из `.`, `_` и `-`.

Пример без комментария и с разделителем `-`:

```yaml
version: 1
dialect: hive

output:
  include_source_comment: false
  filename_separator: "-"
```

## Работа с путями и файлами

Утилита придерживается следующих правил:

- относительные пути разрешаются от текущей рабочей директории;
- входные документы читаются только как UTF-8;
- symlink для входного файла, входной директории или вложенной SQL-цели
  отклоняется;
- symlink в качестве output-директории отклоняется;
- генерируемое имя всегда является обычным именем файла без вложенного пути;
- абсолютные пути хоста не попадают в stdout, stderr, JSON и SQL-комментарии;
- управляющие символы в отображаемых путях экранируются;
- временные файлы удаляются при ошибке записи.

`work/input/*` и `work/output/*` исключены из Git. Пустые директории сохраняются
через `.gitkeep`.

## Docker-first разработка

Development-образ содержит:

- Python 3.12;
- пакет `table-factory` в editable-режиме;
- pytest;
- Ruff;
- mypy;
- Python build.

Корень проекта монтируется как `/workspace`. Изменения в `src/`, `tests/`,
`config/` и `examples/` доступны следующему контейнеру без пересборки.

Образ необходимо пересобрать после изменения:

- `pyproject.toml`;
- зависимостей;
- `Dockerfile`.

```bash
docker compose build
```

### Права файлов на Linux

По умолчанию Compose использует UID/GID `1000:1000`. Если идентификаторы
текущего пользователя отличаются, перед сборкой задайте:

```bash
export LOCAL_UID="$(id -u)"
export LOCAL_GID="$(id -g)"
docker compose build
```

Эти значения используются и при `docker compose run`, поэтому файлы в
bind-mounted директориях создаются от имени хост-пользователя, а не `root`.

## Проверки качества

Все основные проверки запускаются без локального Python.

Тесты:

```bash
docker compose run --rm \
  --entrypoint pytest \
  table-factory
```

Lint:

```bash
docker compose run --rm \
  --entrypoint ruff \
  table-factory check .
```

Проверка форматирования:

```bash
docker compose run --rm \
  --entrypoint ruff \
  table-factory format --check .
```

Форматирование:

```bash
docker compose run --rm \
  --entrypoint ruff \
  table-factory format .
```

Строгая проверка типов:

```bash
docker compose run --rm \
  --entrypoint mypy \
  table-factory src
```

### Opt-in Docker acceptance

Обычная команда `pytest` внутри development-контейнера запускает unit- и
contract-тесты. Тесты, которые сами собирают образ и управляют
`docker compose`, пропускаются, чтобы не запускать Docker рекурсивно из
контейнера.

Полный acceptance-набор запускается с хоста и является необязательным. Для него
нужен локальный Python 3.12 с dev-зависимостями:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
TABLE_FACTORY_RUN_DOCKER_TESTS=1 pytest -q tests/test_docker_runtime.py
```

Acceptance-набор проверяет сборку образа, non-root пользователя, editable
install, persistent output, `generate`/`validate`/`inspect`, сборку wheel,
чистую установку wheel и совпадение результата с Docker CLI.

## Сборка wheel и sdist

Соберите пакет внутри контейнера:

```bash
docker compose run --rm \
  --entrypoint python \
  table-factory -m build
```

Результат останется на хосте:

```text
dist/
├── table_factory-<version>-py3-none-any.whl
└── table_factory-<version>.tar.gz
```

`dist/` является воспроизводимым build output и исключён из Git.

## Установка без Docker

Установка готового wheel:

```bash
python -m pip install dist/table_factory-<version>-py3-none-any.whl
```

После установки доступна та же команда:

```bash
table-factory generate \
  --input ./input \
  --output ./output \
  --config ./table-factory.yaml
```

Runtime-зависимость `PyYAML` устанавливается через metadata wheel.

Для локальной editable-разработки без Docker:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

Этот способ необязателен и не используется в Docker-first сценарии.

## Структура проекта

```text
.
├── Dockerfile
├── compose.yaml
├── pyproject.toml
├── config/
│   └── table-factory.yaml
├── examples/
│   └── example.sql
├── src/
│   └── table_factory/
│       ├── cli.py
│       ├── config.py
│       ├── generator.py
│       ├── models.py
│       ├── parser.py
│       └── workflow.py
├── tests/
├── work/
│   ├── input/
│   └── output/
└── README.md
```

Основные модули:

| Модуль | Ответственность |
| --- | --- |
| `cli.py` | аргументы, команды, коды завершения |
| `config.py` | загрузка и проверка YAML |
| `parser.py` | разбор поддерживаемого Hive DDL |
| `models.py` | неизменяемые модели таблиц и колонок |
| `generator.py` | имена, SQL-шаблоны и безопасная запись |
| `workflow.py` | поиск входов и координация генерации |

## Типовые ошибки

### `no SQL files found in input`

Проверьте, что:

- путь существует;
- внутри есть обычные файлы с расширением `.sql`;
- SQL-файлы не являются symlink.

### `multiple tables map to the same output name`

Две таблицы после нормализации получили одинаковое имя файла. Переименуйте
таблицу или запускайте наборы отдельно.

### `output directory must not be a symbolic link`

Передайте обычную директорию внутри проекта вместо symlink.

### Файлы на Linux принадлежат UID `1000`

Пересоберите образ со своими UID/GID:

```bash
export LOCAL_UID="$(id -u)"
export LOCAL_GID="$(id -g)"
docker compose build --no-cache
```

### Изменились зависимости, но контейнер использует старые версии

Пересоберите development-образ:

```bash
docker compose build
```

## Лицензия

Проект распространяется по лицензии
[`Apache License 2.0`](LICENSE).
