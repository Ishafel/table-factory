# table-factory

`table-factory` читает Hive `CREATE TABLE`, проверяет DDL и создаёт для каждой
таблицы пять детерминированных SQL-файлов:

1. `create`;
2. `drop`;
3. `describe`;
4. `show-create`;
5. `analyze`.

Docker используется только как воспроизводимая среда разработки. Сам проект
остаётся обычным Python-пакетом с единственной CLI-реализацией для Docker и
wheel-установки.

## Быстрый старт через Docker

Для разработки достаточно Docker с Docker Compose; локальный Python не нужен.

```bash
cp examples/example.sql work/input/example.sql
docker compose build
docker compose run --rm table-factory generate \
  --input ./work/input \
  --output ./work/output \
  --config ./config/table-factory.yaml
```

Пять SQL-файлов появятся в `work/output` на хосте и сохранятся после удаления
одноразового контейнера.

Корень проекта монтируется в `/workspace`, а пакет в development-образе
установлен в editable-режиме. Поэтому изменения в `src/` подхватываются без
пересборки. Образ нужно пересобрать после изменения `pyproject.toml`,
зависимостей или `Dockerfile`.

### Права файлов на Linux

По умолчанию Compose использует UID/GID `1000:1000`. Если UID или GID
пользователя отличаются, перед первой сборкой задайте их явно:

```bash
export LOCAL_UID="$(id -u)"
export LOCAL_GID="$(id -g)"
docker compose build
```

Эти же переменные автоматически применяются к последующим `docker compose run`,
и результаты остаются владельцем хост-пользователя.

## Команды

Проверка всех SQL-файлов в директории без записи результата:

```bash
docker compose run --rm table-factory validate \
  --input ./work/input \
  --config ./config/table-factory.yaml
```

Просмотр разобранной модели в JSON:

```bash
docker compose run --rm table-factory inspect \
  ./work/input/example.sql \
  --config ./config/table-factory.yaml
```

Тесты и статические проверки:

```bash
docker compose run --rm --entrypoint pytest table-factory
docker compose run --rm --entrypoint ruff table-factory check .
docker compose run --rm --entrypoint mypy table-factory src
```

Сборка wheel и sdist:

```bash
docker compose run --rm \
  --entrypoint python \
  table-factory -m build
```

Артефакты сохраняются в примонтированной директории:

```text
dist/
├── table_factory-<version>-py3-none-any.whl
└── table_factory-<version>.tar.gz
```

## Установка wheel без Docker

Готовый wheel устанавливается стандартным способом:

```bash
python -m pip install dist/table_factory-<version>-py3-none-any.whl
table-factory generate \
  --input ./input \
  --output ./output \
  --config ./table-factory.yaml
```

Консольный скрипт из wheel вызывает тот же `table_factory.cli:main`, который
используется development-контейнером.

## Конфигурация

Репозиторий содержит безопасную конфигурацию по умолчанию:

```yaml
version: 1
dialect: hive

output:
  include_source_comment: true
  filename_separator: "__"
```

При стандартном разделителе таблица `analytics.customer_orders` создаёт:

```text
analytics_customer_orders__create.sql
analytics_customer_orders__drop.sql
analytics_customer_orders__describe.sql
analytics_customer_orders__show-create.sql
analytics_customer_orders__analyze.sql
```

`create.sql` сохраняет исходный `CREATE TABLE` целиком, включая `TEMPORARY`,
`EXTERNAL`, comments, column constraints и table-level clauses (`STORED AS`,
`LOCATION`, `PARTITIONED BY`, `ROW FORMAT`, `TBLPROPERTIES`). Остальные четыре
файла адресуют ту же разобранную таблицу.

Парсер намеренно принимает безопасное подмножество Hive DDL: `CREATE TABLE` с
явным непустым списком колонок. Идентификаторы со специальными символами должны
быть заключены в обратные кавычки. Формы `CREATE TABLE AS SELECT` и
`CREATE TABLE LIKE` отклоняются, поскольку без полноценного Hive query parser
их нельзя достоверно валидировать.

Имена файлов нормализуются и не могут добавлять вложенные пути. Генерация
сначала разбирает все входы, проверяет коллизии имён и только потом атомарно
записывает файлы. CLI не включает абсолютные пути хоста в JSON, сообщениях или
SQL-комментариях и корректно обрабатывает пробелы и Unicode в путях.
