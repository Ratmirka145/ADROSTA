# ADROSTA Backend — Stage 01

Production-oriented backend формы оптового заказа ADROSTA на **Python 3.12, FastAPI, SQLAlchemy 2 и PostgreSQL 18**. Проект продолжает исходный FastAPI-skeleton из приложенного архива: второй backend не создавался.

Каталог не хранится в JSON-файлах. Товары добавляются Python-командой в PostgreSQL. Браузер технически передаёт тело HTTP-запроса в формате JSON, потому что это стандартный формат FastAPI, но вручную создавать или редактировать `.json` не требуется: для проверки запроса есть Swagger, а для Tilda — готовый клиент в `examples/tilda-api-client.js`.

## Аудит исходного архива

В `adrosta_backend_stage_01.zip` было ровно семь файлов:

- `app/main.py`, `app/__init__.py`, `app/routers/health.py`, `app/routers/__init__.py`;
- `requirements.txt`, `README.md`, `.gitignore`.

Исходный код был настоящим, но минимальным backend-skeleton: FastAPI-приложение версии `0.1.0`, `GET /`, `GET /health` и единственная зависимость FastAPI. Эти два маршрута работали и были сохранены совместимыми.

В архиве не было:

- HTML, CSS или JavaScript формы и кода скрытой нативной формы Tilda;
- запросов к API и маршрута создания заказа;
- моделей покупателя, компании, доставки и товаров;
- доверенного каталога, расчёта веса/объёма и постоянного хранилища;
- защиты от дублей, ограничения частоты, CORS и безопасного логирования;
- интеграции с Tilda, CRM, почтой или другим получателем;
- `.env.example`, использования переменных окружения, Docker-конфигурации;
- тестов и CI;
- каталога `.git`, поэтому история Git, ветки и коммиты в переданном архиве недоступны.

В исходных Python-файлах не было TODO или комментариев, описывающих будущий контракт. README содержал только команды установки и запуска. Поэтому неизвестные домены, SKU, характеристики товаров, CRM и хостинг не были выдуманы и по-прежнему требуют реальной настройки.

## Текущая архитектура

Приложение остаётся компактным монолитом FastAPI с одной PostgreSQL-базой:

```mermaid
flowchart LR
    T["Существующий обработчик формы Tilda"] --> C["Безопасный JS-клиент"]
    C -->|"POST /api/orders + Idempotency-Key"| A["FastAPI"]
    A --> V["Pydantic: проверка и нормализация"]
    V --> S["Сервис заказа"]
    S -->|"SKU + boxes"| DB[("PostgreSQL: товары и price tiers")]
    S --> D["Единый domain calculator"]
    D -->|"рассчитанный snapshot"| O[("заказ, снимок товара, idempotency, outbox")]
    O --> A
    O --> W["Python worker"]
    W -->|"HTTPS webhook, повторные попытки"| X["Tilda / CRM / почтовый шлюз / другой сервис"]
```

Основные части:

| Файл | Назначение |
|---|---|
| `app/main.py` | Создание FastAPI-приложения, middleware, CORS, trusted hosts, маршруты |
| `app/config.py` | Типизированная загрузка и проверка env, строгие production-ограничения |
| `app/schemas.py` | Строгий публичный контракт заказа, телефон/email и условные поля |
| `app/routers/orders.py` | `POST /api/orders` |
| `app/routers/cart.py` | Предварительный `POST /api/cart/calculate` без записи заказа |
| `app/routers/health.py` | Liveness `GET /health` и readiness `GET /ready` |
| `app/database.py`, `app/models.py` | SQLAlchemy engine/session и единый Declarative Base |
| `alembic/` | Версионированные миграции PostgreSQL |
| `app/repositories.py` | Каталог, заказ, снимки характеристик, дедупликация, rate limit, outbox |
| `app/domain.py` | Единственный расчёт цены, количества, груза и totals |
| `app/services.py` | Оркестрация заказа и фоновой доставки |
| `app/container.py` | Сборка зависимостей приложения и worker |
| `app/integrations.py` | Универсальный server-to-server webhook без предположений о CRM |
| `app/security.py` | HMAC-отпечатки, проверка idempotency key, доверенные proxy IP |
| `app/errors.py` | Единый безопасный формат ошибок без отражения введённых данных |
| `app/observability.py` | Request ID, метаданные запросов и ограничение размера тела без логирования PII |
| `app/cli.py` | Управление PostgreSQL-каталогом Python-командами, без JSON-файлов |
| `app/worker.py` | Доставка outbox-сообщений во внешний webhook |
| `examples/tilda-api-client.js` | Изолированный клиент для подключения к существующему Tilda handler |
| `tests/` | Маршрутные, валидационные и инфраструктурные тесты |
| `Dockerfile` | Один непривилегированный API-процесс Uvicorn |

### Что уже реализовано

- строгая проверка структуры и типов, неизвестные поля запрещены;
- нормализация российского телефона `8XXXXXXXXXX` или `7XXXXXXXXXX` в `+7XXXXXXXXXX` и проверка email;
- реквизиты компании обязательны для `buyer.type = business` и запрещены для `individual`;
- строгий выбор получения заказа: самовывоз либо СДЭК до ПВЗ/двери;
- приём от клиента только `sku` и `boxes` для каждой позиции;
- точный, регистрозависимый поиск активного SKU в PostgreSQL;
- серверный расчёт units, SKU-specific price tier, точной цены в копейках, веса, объёма и грузовых мест;
- атомарное сохранение заказа и снимка характеристик товара на момент заказа;
- обязательный `Idempotency-Key`, повтор ответа на безопасный retry и защита от похожего повторного заказа;
- PostgreSQL rate limit по HMAC-отпечатку IP;
- точный список CORS origins, trusted hosts и ограниченный размер запроса;
- логи только с метаданными и request ID, без тела заказа и полных персональных данных;
- надёжный PostgreSQL outbox с `FOR UPDATE SKIP LOCKED` и отдельный worker;
- health/readiness, Swagger в development и Docker healthcheck.

### Что намеренно не реализовано без исходных данных

- конкретный адаптер CRM/Tilda/email: доступен только универсальный webhook;
- публичный API-домен, TLS/reverse proxy и настройки конкретного хостинга;
- правки существующей формы Tilda: её HTML/CSS/JS не было в архиве;
- административный интерфейс, выгрузка заказов, политика удаления PII и резервное копирование как сервис.

PostgreSQL содержит персональные данные заказов. Доступ к базе и резервным копиям должен быть ограничен, а срок хранения необходимо определить отдельно.

## Локальный запуск

Нужен Python 3.12.

### Windows PowerShell

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.txt
Copy-Item .env.example .env
```

Сгенерируйте отдельный секрет и вставьте его как `APP_HASH_SECRET` в локальный `.env`:

```powershell
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Запустите PostgreSQL, примените миграции и идемпотентно загрузите каталог ADROSTA:

```powershell
docker compose up -d postgres
python -m alembic upgrade head
python -m app.cli catalog seed-adrosta
python -m app.cli product list
```

Запустите API:

```powershell
python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000 --no-access-log
```

Проверка после запуска:

- API: <http://127.0.0.1:8000/>;
- liveness: <http://127.0.0.1:8000/health>;
- readiness: <http://127.0.0.1:8000/ready>;
- Swagger: <http://127.0.0.1:8000/docs>.

`/health` подтверждает, что процесс отвечает. `/ready` возвращает HTTP 200 только при доступной базе, непустом каталоге, заданном `APP_HASH_SECRET` и допустимой конфигурации получателя. До добавления товара статус `not_ready` ожидаем.

### Linux/macOS

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
cp .env.example .env
docker compose up -d postgres
python -m alembic upgrade head
python -m app.cli catalog seed-adrosta
python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000 --no-access-log
```

## Каталог товаров без JSON

Схема создаётся только командой `python -m alembic upgrade head`; приложение не вызывает `create_all()` при старте. Габариты вводятся в миллиметрах, вес одной коробки — в граммах; объём рассчитывается CLI. Используйте только проверенные складские/товарные данные.

```powershell
# Добавить новый SKU или обновить существующий
python -m app.cli product upsert --sku "SKU-001" --name "Товар" --units-per-box 10 --weight-grams 9000 --length-mm 500 --width-mm 300 --height-mm 200

# Активные товары
python -m app.cli product list

# Включая отключённые
python -m app.cli product list --all

# Временно запретить или снова разрешить заказы SKU
python -m app.cli product deactivate --sku "SKU-001"
python -m app.cli product activate --sku "SKU-001"
```

Неактивный или отсутствующий SKU для публичного API считается неизвестным. Регистр важен: `SKU-001` и `sku-001` — разные значения.

### Переход с локальной SQLite-базы

Initial Alembic migration создаёт пустую PostgreSQL-схему. Автоматический перенос
старого `.sqlite3` не выполняется: текущие данные считались dev/test данными, а
каталог восстанавливается идемпотентной командой `catalog seed-adrosta`. Если в
SQLite окажутся необходимые реальные заказы, нужен отдельный проверяемый export/
import с валидацией количества строк и snapshot-полей; запускать такой перенос
как побочный эффект старта API нельзя.

## Server-side pricing

Frontend передаёт для каждой позиции только `sku` и целое `boxes >= 1`. Поля `unitsPerBox`, цены, суммы, веса, габаритов и totals запрещены входной схемой. Backend загружает из PostgreSQL канонические `name`, `units_per_box`, вес и габариты одной коробки, затем выбирает price tier и рассчитывает позиции и итог корзины в одном `app.domain.calculate_order()`. Этот же calculator используется при предварительном расчёте и при создании заказа; repository сохраняет готовый snapshot и не выбирает тариф.

Команда `python -m app.cli catalog seed-adrosta` безопасна при повторном запуске и создаёт/обновляет:

- `opt-san-green` — SAN Green;
- `opt-san-blue` — SAN Blue;
- для обоих: 10 единиц в коробке, 10 900 г, 330 × 200 × 265 мм;
- price tiers: 1–4 коробки → 320 ₽/ед., 5–9 → 290 ₽/ед., 10+ → 260 ₽/ед.

Деньги хранятся и считаются только как integer kopecks (`32000`, `29000`, `26000`), без `float`. Tier выбирается отдельно по количеству коробок каждого SKU. Поэтому 6 Green получают 290 ₽/ед., а 4 Blue — 320 ₽/ед.; общий размер корзины 10 коробок не переводит обе позиции в tier 10+.

### `POST /api/cart/calculate`

Endpoint выполняет тот же server-side calculation, но не создаёт `orders`, idempotency record или outbox event.

```json
{
  "items": [
    {"sku": "opt-san-green", "boxes": 5}
  ]
}
```

Полный канонический response:

```json
{
  "items": [
    {
      "sku": "opt-san-green",
      "name": "SAN Green",
      "boxes": 5,
      "unitsPerBox": 10,
      "units": 50,
      "pricePerUnitKopecks": 29000,
      "pricePerBoxKopecks": 290000,
      "lineAmountKopecks": 1450000,
      "weightPerBoxGrams": 10900,
      "totalWeightGrams": 54500,
      "boxVolumeMm3": 17490000,
      "lengthMm": 330,
      "widthMm": 200,
      "heightMm": 265,
      "cargoPlaces": 5,
      "totalVolumeMm3": 87450000
    }
  ],
  "totals": {
    "totalBoxes": 5,
    "totalUnits": 50,
    "productsAmountKopecks": 1450000,
    "totalWeightGrams": 54500,
    "cargoPlaces": 5,
    "totalVolumeMm3": 87450000
  }
}
```

Backend возвращает только канонические технические единицы: копейки, граммы, миллиметры и мм³. Форматирование в ₽, килограммы или м³ при необходимости выполняет frontend. `productsAmountKopecks` содержит только стоимость товаров. Цена доставки пока не рассчитывается: backend не обращается к API СДЭК и не создаёт фиктивный delivery amount.

## Контракт `POST /api/orders`

Обязательный заголовок `Idempotency-Key` — UUID одной логической отправки. При сетевой ошибке повторите тот же запрос с тем же ключом. Новый заказ должен получить новый UUID. Заголовок не является секретом.

Самый простой способ изучить контракт без ручной работы с JSON — открыть `/docs`, выбрать `POST /api/orders`, нажать **Try it out**, заполнить автоматически показанную схему и выполнить запрос. Swagger доступен, пока `API_DOCS_ENABLED=true`.

Публичные имена полей:

| Поле | Правило |
|---|---|
| `buyer.type` | `individual` или `business` |
| `buyer.contactName` | Обязательно, 1–200 символов |
| `buyer.phone` | 11 цифр, начинается с `7` или `8`; сервер сохраняет как `+7…` |
| `buyer.email` | Обязательный корректный email |
| `company` | Обязательно для `business`, запрещено для `individual` |
| `company.name` | 1–300 символов |
| `company.inn` | 10 цифр для организации или 12 цифр для ИП |
| `company.kpp` | Ровно 9 цифр и обязательно для 10-значного ИНН; отсутствует для ИП с 12-значным ИНН |
| `company.legalAddress` | 1–500 символов |
| `delivery.method` | Строго `self_pickup` или `cdek` |
| `delivery.type` | Для СДЭК обязательно: `pickup` или `door`; для самовывоза отсутствует |
| `delivery.region` | Необязательно для СДЭК, до 200 символов |
| `delivery.city` | Обязательно для СДЭК, до 200 символов |
| `delivery.officeCode` | Необязательный код ПВЗ для `cdek/pickup`; для `door` запрещён |
| `delivery.street`, `delivery.house` | Обязательны только для `cdek/door` |
| `delivery.postcode`, `delivery.apartment` | Необязательны только для `cdek/door` |
| `delivery.recipient.contactName` | Необязательно; если передан `recipient`, имя обязательно, до 200 символов |
| `delivery.recipient.phone` | Те же правила телефона |
| `delivery.recipient.email` | Необязательный корректный email |
| `comment` | Необязательно, до 2000 символов |
| `items` | 1–100 уникальных позиций |
| `items[].sku` | Обязательный активный SKU, максимум 64 символа |
| `items[].boxes` | Целое число от 1 до 10000 |

Валидный самовывоз не требует параметров СДЭК:

```json
{
  "delivery": {
    "method": "self_pickup"
  }
}
```

Доставка СДЭК до ПВЗ:

```json
{
  "delivery": {
    "method": "cdek",
    "type": "pickup",
    "region": "Москва",
    "city": "Москва",
    "officeCode": null
  }
}
```

Доставка СДЭК до двери использует структурированный адрес:

```json
{
  "delivery": {
    "method": "cdek",
    "type": "door",
    "region": "Москва",
    "city": "Москва",
    "postcode": "115054",
    "street": "Дубининская",
    "house": "53",
    "apartment": "12"
  }
}
```

На текущем этапе backend только принимает и сохраняет выбранный способ получения. Расчёт тарифа СДЭК и получение списка ПВЗ будут подключены отдельно; обращения к API СДЭК сейчас не выполняются.

Для позиции заказа API принимает только `sku` и `boxes`. Поля цены, веса, объёма, размеров и клиентские totals будут отклонены как неизвестные. Общий предел коробок дополнительно задаёт `MAX_TOTAL_BOXES`.

Успешное создание возвращает HTTP 201, `orderId`, `status=accepted`, `integrationStatus`, серверные позиции и totals. Коммерческие snapshot-поля (`unitsPerBox`, `units`, цены и `lineAmountKopecks`) вместе с весом, объёмом и грузовыми местами рассчитаны backend. Повтор того же запроса с тем же ключом возвращает сохранённый результат с HTTP 200, `replayed=true` и заголовком `Idempotency-Replayed: true`.

Статусы интеграции:

- `stored` — заказ сохранён локально, webhook выключен;
- `pending` — заказ сохранён и ожидает/повторяет доставку worker;
- `delivered` — webhook подтвердил HTTP 2xx;
- `failed` — исчерпаны попытки или получен окончательный отказ.

Формат ошибки единый: объект `error` содержит стабильный `code`, безопасное русское `message`, список `details` и `requestId`. Основные ответы:

| HTTP | Коды/причины |
|---:|---|
| 400 | `IDEMPOTENCY_KEY_REQUIRED`, некорректный запрос |
| 409 | `DUPLICATE_ORDER`, `IDEMPOTENCY_CONFLICT` |
| 413 | `PAYLOAD_TOO_LARGE` |
| 422 | `VALIDATION_ERROR`, `UNKNOWN_SKU` |
| 429 | `RATE_LIMITED`; время ожидания есть в `Retry-After` |
| 503 | `PRICE_TIER_NOT_FOUND`, `CATALOG_UNAVAILABLE` или `SERVICE_UNAVAILABLE` |

Не показывайте ответ через `innerHTML`; используйте `textContent`. Передавая `requestId` поддержке, пользователь не раскрывает содержимое заказа.

## Подключение существующей формы Tilda

В архиве не было кода формы, поэтому `examples/tilda-api-client.js` не ищет поля, не нажимает нативную кнопку Tilda и не меняет уже работающий `MutationObserver`. Это самостоятельный транспортный слой, который нужно вызвать из существующего handler после его клиентской валидации.

Пример подключения (URL — заполнитель, его нужно заменить реальным HTTPS API):

```html
<script src="https://ВАШ-СТАТИЧЕСКИЙ-ДОМЕН/tilda-api-client.js"></script>
<script>
  const orderSubmission = window.AdrostaOrderClient.createSubmission({
    endpoint: "https://ВАШ-API-ДОМЕН/api/orders",
    onLoading: function (isLoading) {
      // Здесь включить/выключить уже существующий loader и кнопку.
    },
    onSuccess: function (result) {
      // Здесь показать успех или продолжить существующую нативную отправку Tilda.
    },
    onError: function (error) {
      // Показать error.message через textContent; сохранить error.requestId для поддержки.
    }
  });

  async function sendFromExistingTildaHandler(orderFromForm) {
    return orderSubmission.submit(orderFromForm);
  }
</script>
```

Один объект `orderSubmission` соответствует одной логической отправке. Он генерирует криптографический UUID, объединяет одновременные клики и повторно использует UUID при retry. Если пользователь изменил заказ после ошибки или начал новый заказ, вызовите `orderSubmission.reset()` и только потом `submit`. После успешной отправки проще создать новый объект для следующего заказа.

Клиент явно копирует только `sku` и `boxes` из каждой позиции, поэтому предварительные totals и размеры из браузера на сервер не уходят. `endpoint` публичен и не является секретом. Токены, пароль webhook, `APP_HASH_SECRET` и любые ключи CRM в Tilda вставлять нельзя.

Нужно вручную решить, что будет источником истины:

- рекомендуемый порядок — сначала получить успех backend, затем при необходимости вызвать существующую нативную отправку Tilda;
- нативная отправка Tilda не должна создавать второй заказ в том же backend;
- при неясном результате сети повторяется `submit` того же `orderSubmission`, а не создаётся новый;
- DOM-селекторы, `MutationObserver`, таймаут 4500 мс и снятие маски/`required` остаются в существующем коде формы и здесь не дублируются.

## Переменные окружения

Скопируйте `.env.example` в `.env`. Реальные секреты не коммитятся и не передаются во frontend.

| Переменная | Назначение |
|---|---|
| `APP_ENV` | `development`, `test` или `production` |
| `DEBUG` | В production только `false` |
| `API_DOCS_ENABLED` | Swagger/ReDoc/OpenAPI; в production только `false` |
| `LOG_LEVEL` | `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL` |
| `DATABASE_URL` | SQLAlchemy URL вида `postgresql+psycopg://user:password@host:5432/database`; обязателен |
| `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD` | Параметры только для Compose PostgreSQL; приложение читает `DATABASE_URL` |
| `CORS_ALLOWED_ORIGINS` | Точные origins Tilda через запятую, без `*`, path и завершающего `/`; в production только HTTPS |
| `ALLOWED_HOSTS` | Точные DNS-hostnames/IPv4 API через запятую; без схемы URL и порта |
| `TRUSTED_PROXY_IPS` | IP/CIDR только фактически доверенных reverse proxy |
| `APP_HASH_SECRET` | Секрет не короче 32 символов для HMAC; обязателен в production и для приёма заказов |
| `IDEMPOTENCY_TTL_SECONDS` | Срок хранения результата ключа |
| `DUPLICATE_WINDOW_SECONDS` | Окно обнаружения одинакового заказа с другим ключом |
| `IDEMPOTENCY_KEY_MAX_LENGTH` | Максимальная длина заголовка |
| `ORDER_RATE_LIMIT_COUNT` | Число запросов одного IP в окне |
| `ORDER_RATE_LIMIT_WINDOW_SECONDS` | Длина rate-limit окна |
| `MAX_REQUEST_BODY_BYTES` | Максимальный размер тела запроса |
| `MAX_ORDER_ITEMS` | Максимум разных SKU |
| `MAX_BOXES_PER_ITEM` | Максимум коробок одной позиции |
| `MAX_TOTAL_BOXES` | Максимум коробок заказа |
| `WEBHOOK_ENABLED` | Включить внешний server-to-server webhook |
| `ORDER_WEBHOOK_URL` | Реальный URL получателя; в production только HTTPS, без query-токенов |
| `ORDER_WEBHOOK_TOKEN` | Bearer token получателя; обязателен для production webhook |
| `WEBHOOK_TIMEOUT_SECONDS` | Таймаут одной попытки |
| `WEBHOOK_MAX_ATTEMPTS` | Максимум попыток доставки |
| `WEBHOOK_BACKOFF_SECONDS` | База экспоненциальной задержки |
| `OUTBOX_ENABLED` | Durable outbox; обязан быть включён вместе с webhook |
| `OUTBOX_POLL_INTERVAL_SECONDS` | Интервал опроса worker |
| `OUTBOX_BATCH_SIZE` | Размер одной пачки worker |
| `OUTBOX_LOCK_SECONDS` | Lease сообщения для безопасной обработки |
| `ALLOW_STORE_ONLY` | Явно разрешить production без получателя; обычно `false` |

Production-конфигурация проверяется при старте. Runtime принимает только драйвер `postgresql+psycopg`; wildcard CORS, слабый секрет, HTTP webhook, включённый debug/docs и противоречивые outbox-настройки приводят к отказу запуска. Не помещайте пароль базы в логи или frontend.

## Webhook worker

При `WEBHOOK_ENABLED=true` API сохраняет заказ и событие `order.created` в одной транзакции и сразу отвечает браузеру. Внешняя недоступность не теряет принятый заказ: worker повторяет доставку с экспоненциальной задержкой и отмечает окончательный результат в outbox.

```powershell
# Постоянный процесс рядом с API
python -m app.worker

# Одна пачка для cron/диагностики
python -m app.worker --once
```

Worker отправляет полный сохранённый заказ, доверенные totals и снимки товаров методом POST. Заголовок `Idempotency-Key` равен `orderId`; при наличии токена используется `Authorization: Bearer …`. Получатель обязан безопасно обрабатывать повтор одного `orderId`. Тела ошибок внешнего сервиса не сохраняются и не логируются.

Точный контракт конкретной CRM/Tilda/email API неизвестен. Если её формат отличается, нужен отдельный server-side adapter в `app/integrations.py`; секрет всё равно остаётся только на backend.

## Тесты

```powershell
python -m pip install -r requirements-dev.txt
python -m pytest -q
```

Основной набор использует тот же SQLAlchemy repository слой с изолированной
SQLite in-memory/file базой только как быстрый test double. Runtime-конфигурация
development/production SQLite не принимает. Для обязательной проверки реального
PostgreSQL укажите URL отдельной disposable test database:

```powershell
$env:TEST_DATABASE_URL="postgresql+psycopg://user:password@127.0.0.1:5432/adrosta_test"
python -m pytest -q -m integration
```

Integration-тест сам выполняет `alembic upgrade head`, очищает только указанную
test database, дважды запускает seed через repository и проверяет cart, order,
idempotency concurrency и outbox. Не указывайте в `TEST_DATABASE_URL` production
database.

При изменении моделей создайте ревизию только на локальной PostgreSQL-базе,
просмотрите сгенерированный файл и проверьте отсутствие незаписанных изменений:

```powershell
python -m alembic revision --autogenerate -m "describe schema change"
python -m alembic upgrade head
python -m alembic check
```

Набор тестов должен оставаться обязательным перед деплоем и проверять как минимум:

- health/readiness и успешный заказ;
- физическое и юридическое лицо;
- серверный расчёт по доверенному каталогу;
- неизвестный SKU;
- неверные телефон и email;
- отсутствующие условно обязательные поля;
- безопасный повтор с тем же idempotency key, конфликт ключа и дубликат с новым ключом;
- rate limit и ограничение размера запроса;
- недоступность/отказ webhook, retry и окончательный статус outbox;
- строгий CORS и отсутствие секретов в клиентском файле.

## Docker Compose и PostgreSQL

`compose.yaml` запускает только PostgreSQL 18.4, публикует его на локальном
`127.0.0.1:5432`, хранит данные в named volume и проверяет готовность через
`pg_isready`. API и worker остаются обычными Python-процессами.

```powershell
Copy-Item .env.example .env
# замените POSTGRES_PASSWORD и тот же пароль внутри DATABASE_URL
docker compose up -d postgres
docker compose ps
python -m alembic upgrade head
python -m app.cli catalog seed-adrosta
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --no-access-log
```

Для локального сброса тестовой базы остановите процессы и выполните
`docker compose down -v`; флаг `-v` безвозвратно удаляет локальный volume.
Обычный `docker compose down` данные сохраняет.

Образ приложения содержит Alembic, но не применяет миграции автоматически:

```powershell
docker build -t adrosta-backend:stage-01 .
docker run --rm --env-file .env --network host adrosta-backend:stage-01 python -m alembic upgrade head
docker run -d --name adrosta-api --restart unless-stopped --env-file .env --network host adrosta-backend:stage-01
```

В production сначала применяйте миграции отдельным release-job, затем запускайте
API и при включённом webhook отдельный `python -m app.worker`. PostgreSQL допускает
несколько API/worker процессов; outbox claim использует row locks и `SKIP LOCKED`.

## Checklist деплоя

1. Получить реальный HTTPS hostname API и настроить TLS/reverse proxy.
2. Создать production `.env` вне Git: `APP_ENV=production`, `DEBUG=false`, `API_DOCS_ENABLED=false`.
3. Создать PostgreSQL database/user, сохранить секретный `DATABASE_URL` и выполнить `python -m alembic upgrade head`.
4. Сгенерировать уникальный `APP_HASH_SECRET`; сохранить его в secret manager/на хосте, не в Tilda.
5. Внести точные опубликованные Tilda origins в `CORS_ALLOWED_ORIGINS`; отдельно перечислить API hostname и healthcheck IP в `ALLOWED_HOSTS`.
6. Если reverse proxy передаёт `X-Forwarded-For`, внести только его реальные IP/CIDR в `TRUSTED_PROXY_IPS`.
7. Загрузить все реальные активные SKU через `python -m app.cli product upsert` и сверить граммы/миллиметры с источником данных.
8. Выбрать режим: настроить реальный HTTPS webhook и worker либо осознанно включить `ALLOW_STORE_ONLY=true`.
9. Выполнить тесты, затем проверить `/health` и `/ready` на целевом окружении.
10. Настроить PostgreSQL backup (`pg_dump`/управляемые snapshots), retention и регулярный тест восстановления.
11. Настроить мониторинг HTTP 5xx, `not_ready`, падения worker и сообщений outbox со статусом `failed`.
12. Определить срок хранения PII, доступ операторов и процедуру удаления/выгрузки заказов.
13. Разместить версионированный `examples/tilda-api-client.js`, встроить вызов в существующий handler и опубликовать страницу Tilda.
14. Сделать реальный тест физлица и юрлица, проверить заказ в PostgreSQL и в конечном внешнем сервисе.

## Что нужно предоставить/настроить вручную

- публичный HTTPS URL backend;
- точный список опубликованных доменов Tilda, включая варианты с `www` и отдельный preview-домен только если он действительно нужен;
- список SKU с названием, весом одной коробки и тремя габаритами;
- выбранный PostgreSQL-хостинг, backup/restore, reverse proxy и его доверенные IP;
- назначение заявки: только PostgreSQL или конкретная Tilda/CRM/email система;
- URL, способ авторизации и ожидаемый контракт внешнего получателя;
- решение, должна ли после успеха backend дополнительно срабатывать текущая нативная отправка Tilda;
- политика хранения персональных данных, резервного копирования и доступа.

Без этих данных нельзя безопасно указывать production CORS, домен, каталог и интеграцию. Значения-примеры из этого README нельзя переносить в production как реальные.
