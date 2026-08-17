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
    O --> I[("immutable invoice snapshot + PDF BYTEA")]
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
| `app/routers/customer.py` | Session-protected customer order, invoice PDF и logout endpoints |
| `app/routers/cart.py` | Предварительный `POST /api/cart/calculate` без записи заказа |
| `app/routers/health.py` | Liveness `GET /health` и readiness `GET /ready` |
| `app/database.py`, `app/models.py` | SQLAlchemy engine/session и единый Declarative Base |
| `alembic/` | Версионированные миграции PostgreSQL |
| `app/repositories.py` | Каталог, заказ, снимки характеристик, дедупликация, rate limit, outbox |
| `app/domain.py` | Единственный расчёт цены, количества, груза и totals |
| `app/services.py` | Оркестрация заказа и фоновой доставки |
| `app/container.py` | Сборка зависимостей приложения и worker |
| `app/integrations.py` | Тонкие HTTP-адаптеры: CDEK API v2 и универсальный webhook |
| `app/cdek.py` | CDEK application service, package adapter и нормализация тарифов |
| `app/invoice.py` | Immutable invoice snapshot, формат денег и offline PDF renderer |
| `app/security.py` | HMAC-отпечатки, проверка idempotency key, доверенные proxy IP |
| `app/errors.py` | Единый безопасный формат ошибок без отражения введённых данных |
| `app/observability.py` | Request ID, метаданные запросов и ограничение размера тела без логирования PII |
| `app/cli.py` | Управление PostgreSQL-каталогом Python-командами, без JSON-файлов |
| `app/worker.py` | Retry invoice generation и доставка order outbox во внешний webhook |
| `examples/tilda-api-client.js` | Credentialed клиент для подключения к существующему Tilda handler |
| `examples/tilda-order-page.html` | Автономный пример customer order page для блока Tilda T123 |
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
- CDEK API v2: поиск городов и обычных ПВЗ, OAuth и предварительный расчёт списка тарифов.
- автоматический коммерческий счёт из сохранённого order snapshot, backend-нумерация, PDF и SHA-256.

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

Backend возвращает только канонические технические единицы: копейки, граммы, миллиметры и мм³. Форматирование в ₽, килограммы или м³ при необходимости выполняет frontend. `productsAmountKopecks` содержит только стоимость товаров и не смешивается со стоимостью доставки.

## CDEK integration

Backend обращается к официальному CDEK API v2 только server-to-server. Поддержаны test (`api.edu.cdek.ru`) и production (`api.cdek.ru`) environments; конкретный host выбирается только через `CDEK_ENV`, а frontend не может передать URL, client ID, secret или access token. Реализация сверена с [официальным SDK CDEK-IT](https://github.com/cdek-it/sdk2.0).

Интеграция поддерживает:

- OAuth `client_credentials` с process-local cache, блокировкой конкурентного refresh и однократным refresh/retry после HTTP 401;
- поиск городов и получение только обычных ПВЗ, без постаматов;
- расчёт и возврат всех совместимых тарифов без скрытого выбора самого дешёвого;
- повторную server-side проверку выбранного тарифа и ПВЗ при создании заказа;
- ограниченный retry временных сетевых ошибок/502/503/504 и безопасные API errors.

Интеграция не создаёт отправление или заказ в CDEK, не получает трек-номер, не синхронизирует статусы и не вызывает shipment/order endpoints. `POST /api/orders` создаёт только коммерческий заказ ADROSTA с неизменяемым snapshot проверенной доставки.

### Поиск города и ПВЗ

```bash
curl --get 'http://127.0.0.1:8000/api/delivery/cdek/cities' \
  --data-urlencode 'query=Москва' \
  --data-urlencode 'countryCode=RU'

curl --get 'http://127.0.0.1:8000/api/delivery/cdek/offices' \
  --data-urlencode 'cityCode=44'
```

Города и ПВЗ возвращаются в компактном ADROSTA contract, а не как raw CDEK objects. Для checkout `pickup` endpoint офисов возвращает только `type=PVZ`; код выбранного ПВЗ является источником истины.

### Расчёт тарифов

```bash
curl 'http://127.0.0.1:8000/api/delivery/cdek/quote' \
  -H 'Content-Type: application/json' \
  -d '{
    "deliveryType": "pickup",
    "toCityCode": 44,
    "items": [{"sku": "opt-san-green", "boxes": 5}]
  }'
```

Frontend передаёт только `deliveryType`, город назначения, `sku` и `boxes`. Backend повторно использует единый server-side cart calculator и загружает вес/габариты из PostgreSQL-каталога. Каждая коробка становится отдельным CDEK package: пять коробок создают пять грузовых мест по 10 900 г, а 330 × 200 × 265 мм безопасно преобразуются в требуемые CDEK целые сантиметры как 33 × 20 × 27 см (округление вверх). Город отправления и схема забора (`warehouse` или `door`) берутся только из server config.

Сумма CDEK преобразуется из рублей в integer kopecks через `Decimal`. Для фильтрации применяются официальные `delivery_mode`: `1` дверь→дверь, `2` дверь→склад/ПВЗ, `3` склад→дверь, `4` склад→склад/ПВЗ. Ответ содержит список всех совместимых options; quote ничего не сохраняет в PostgreSQL, не создаёт ADROSTA order и не запускает outbox.

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
| `delivery.toCityCode` | Положительный код города CDEK; обязателен для `cdek`, отсутствует для самовывоза |
| `delivery.tariffCode` | Выбранный из quote тариф; обязателен для `cdek` и повторно проверяется backend |
| `delivery.region` | Необязательно для СДЭК, до 200 символов |
| `delivery.city` | Обязательно для `cdek/door`; для `pickup` достаточно `toCityCode` |
| `delivery.officeCode` | Обязательный проверяемый код ПВЗ для `cdek/pickup`; для `door` запрещён |
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
    "toCityCode": 44,
    "tariffCode": 136,
    "officeCode": "MSK123"
  }
}
```

Доставка СДЭК до двери использует структурированный адрес:

```json
{
  "delivery": {
    "method": "cdek",
    "type": "door",
    "toCityCode": 44,
    "tariffCode": 137,
    "region": "Москва",
    "city": "Москва",
    "postcode": "115054",
    "street": "Дубининская",
    "house": "53",
    "apartment": "12"
  }
}
```

### Checkout с доставкой

Порядок оформления:

1. Frontend получает каноническую стоимость товаров через `POST /api/cart/calculate`.
2. Для СДЭК frontend запрашивает `POST /api/delivery/cdek/quote` и даёт пользователю выбрать тариф и, для pickup, ПВЗ.
3. Frontend отправляет в `POST /api/orders` только `toCityCode`, выбранные `tariffCode`/`officeCode`, адрес и товары — без цены доставки и без города отправления.
4. Backend заново рассчитывает товары, строит CDEK packages из канонического каталога и получает актуальные тарифы server-to-server.
5. Выбранный тариф проверяется на доступность, `delivery.type` и `CDEK_ORIGIN_MODE`; для pickup выбранный ПВЗ дополнительно проверяется по `toCityCode`.
6. Актуальная сумма CDEK преобразуется через `Decimal` в integer kopecks, после чего order, item snapshots, delivery snapshot, totals и outbox сохраняются одной транзакцией.

Frontend-поля `deliveryAmount`, `deliveryAmountKopecks`, `deliveryPrice`, `price` и `fromCityCode` не входят в schema и отклоняются как неизвестные. Если CDEK недоступен или выбранный тариф исчез, заказ не создаётся; недоступный тариф возвращает `CDEK_TARIFF_UNAVAILABLE`. При изменении цены между preview и оформлением authoritative является новая сумма из финальной проверки.

Для самовывоза CDEK не вызывается: `deliveryAmountKopecks=0`, а `grandTotalKopecks` равен `productsAmountKopecks`. Для CDEK `grandTotalKopecks = productsAmountKopecks + deliveryAmountKopecks`; все три значения сохраняются в PostgreSQL и возвращаются в response вместе с тарифом, сроком, ПВЗ или структурированным адресом.

Пример части успешного ответа:

```json
{
  "totals": {
    "productsAmountKopecks": 1450000,
    "deliveryAmountKopecks": 123450,
    "grandTotalKopecks": 1573450
  },
  "delivery": {
    "method": "cdek",
    "type": "pickup",
    "toCityCode": 44,
    "tariffCode": 136,
    "tariffName": "Посылка склад-склад",
    "deliveryMode": 4,
    "officeCode": "MSK123",
    "periodMinDays": 2,
    "periodMaxDays": 4
  }
}
```

Повтор уже сохранённого запроса с тем же `Idempotency-Key` возвращает snapshot заказа и не выполняет CDEK quote повторно. На этом этапе shipment, waybill и tracking в CDEK не создаются; это отдельный workflow после подтверждения оплаты/заказа.

## Invoice Phase 1

Для каждого нового коммерческого заказа backend в той же PostgreSQL-транзакции создаёт ровно один immutable invoice snapshot. Frontend не передаёт номер счёта, цены, totals, реквизиты продавца, налоговый текст, назначение платежа или PDF. Счёт использует только уже сохранённые данные заказа: buyer/company snapshot, строки `order_items`, `products_amount_kopecks`, `delivery_amount_kopecks` и `grand_total_kopecks`. Каталог и CDEK повторно не вызываются, поэтому последующее изменение цены или тарифа не меняет выданный счёт.

Снимок хранится в `invoices`, строки — в `invoice_items`. Количество товара в строке выражено в фактических единицах (`шт.`), а не в коробках; unit price копируется из order item snapshot. При положительной стоимости СДЭК добавляется одна строка `Доставка СДЭК` с единицей `усл.`. Для бесплатного самовывоза нулевая строка доставки не создаётся.

Номер имеет формат `INV-YYYY-NNNNNN`. Таблица `invoice_counters` выдаёт следующее значение атомарным PostgreSQL upsert внутри транзакции; `UNIQUE(invoice_number)` и `UNIQUE(order_id)` дополнительно гарантируют уникальность номера и правило «один заказ — один счёт». Повторный `Idempotency-Key` и повтор `ensure_invoice(order_id)` возвращают существующий снимок без обновления реквизитов, totals, строк или номера.

PDF строится ReportLab без браузера, сети и удалённых assets. Кириллица и знак рубля рендерятся локальным DejaVu Sans; Docker устанавливает `fonts-dejavu-core`. PDF сохраняется прямо в PostgreSQL `BYTEA`, рядом хранится SHA-256, вычисленный по точным сохранённым bytes. Это изолировано в `InvoicePdfRenderer`/`InvoiceRepository`, поэтому storage можно заменить позже без изменения расчёта заказа.

Lifecycle: `pending` создаётся вместе с order и durable outbox event `invoice.generate`; API после commit пытается сразу получить `generated`. Ошибка renderer не откатывает и не дублирует уже принятый order: событие остаётся для retry worker, а invoice получает `failed` до следующей попытки. Успешный retry записывает PDF и переводит invoice в `generated`; уже сгенерированный документ не перезаписывается.

`INVOICE_TAX_TEXT` является только конфигурационной строкой и должен быть подтверждён бухгалтером — backend не определяет налоговый режим и не рассчитывает НДС. Поддерживаемые placeholders назначения платежа: `{invoice_number}`, `{invoice_date}`, `{order_id}`.

PDF не имеет публичного угадываемого URL. Он выдаётся только через customer session endpoint, описанный ниже; запрос не пересчитывает заказ, не запускает renderer и не обращается к каталогу или CDEK.

Для позиции заказа API принимает только `sku` и `boxes`. Поля цены, веса, объёма, размеров и клиентские totals будут отклонены как неизвестные. Общий предел коробок дополнительно задаёт `MAX_TOTAL_BOXES`.

Успешное создание возвращает HTTP 201, совместимый прежний `orderId`, новый отображаемый `orderNumber`, `orderPageUrl`, `status=accepted`, `integrationStatus`, серверные позиции и totals. Коммерческие snapshot-поля (`unitsPerBox`, `units`, цены и `lineAmountKopecks`) вместе с весом, объёмом и грузовыми местами рассчитаны backend. Повтор того же запроса с тем же ключом возвращает сохранённый результат с HTTP 200, `replayed=true` и заголовком `Idempotency-Replayed: true`.

## Customer Access Phase 1

При первом успешном `POST /api/orders` backend генерирует непрозрачный token через `secrets.token_urlsafe(32)` (256 бит энтропии), сохраняет только его SHA-256 hash в `customer_sessions` и устанавливает host-only cookie `adrosta_customer_session`. Cookie имеет `HttpOnly`, ограниченный `Path=/api`, настраиваемый fixed TTL (по умолчанию 90 дней) и `SameSite=Lax`; production-конфигурация запрещает `Secure=false`. Raw token отсутствует в JSON, URL, idempotency records, outbox и PostgreSQL.

Связь `customer_session_orders` выдаёт одной browser session доступ к нескольким созданным ею заказам. Новый order, items, invoice snapshot, outbox/idempotency и session grant записываются одной транзакцией. Формат отображаемого номера — `AD-YYYY-NNNNNN`; годовой счётчик обновляется атомарным upsert, но сам номер не является авторизацией.

Защищённые endpoints:

- `GET /api/customer/orders/{order_number}` — customer-safe snapshot без database ID, buyer PII, idempotency/outbox и служебных полей;
- `GET /api/customer/orders/{order_number}/invoice.pdf?disposition=inline|attachment` — уже сохранённые immutable PDF bytes;
- `POST /api/customer/session/logout` — отзыв текущей session и очистка cookie; запрос требует точный `Origin` из `CORS_ALLOWED_ORIGINS`.

Отсутствующая, неизвестная, истёкшая, отозванная или чужая session, а также неизвестный номер дают одинаковый `404 ORDER_NOT_AVAILABLE`. `last_used_at` обновляется не чаще одного раза в час. Повтор по `Idempotency-Key` не создаёт новый order/invoice/session; новая или чужая session при replay не получает grant. Поэтому потерянную cookie нельзя восстановить одним знанием idempotency key — recovery через email/SMS OTP остаётся задачей Phase 2.

Customer API возвращает только номер/дату/статус заказа, безопасные строки товаров и totals, краткое описание доставки, номер/дату invoice, его статус `pending`, `generated` или `failed` и `pdfAvailable`. Телефон, email, ИНН, КПП, полный адрес, внутренние UUID и integration identifiers не возвращаются. Владелец grant получает `409 INVOICE_NOT_READY`, если PDF ещё не сформирован; запрос без доступа по-прежнему получает неотличимый `404 ORDER_NOT_AVAILABLE`.

Статусы интеграции:

- `stored` — заказ сохранён локально, webhook выключен;
- `pending` — заказ сохранён и ожидает/повторяет доставку worker;
- `delivered` — webhook подтвердил HTTP 2xx;
- `failed` — исчерпаны попытки или получен окончательный отказ.

Формат ошибки единый: объект `error` содержит стабильный `code`, безопасное русское `message`, список `details` и `requestId`. Основные ответы:

| HTTP | Коды/причины |
|---:|---|
| 400 | `IDEMPOTENCY_KEY_REQUIRED`, некорректный запрос |
| 404 | `ORDER_NOT_AVAILABLE` для любого недоступного customer order/invoice |
| 409 | `DUPLICATE_ORDER`, `IDEMPOTENCY_CONFLICT`, `INVOICE_NOT_READY` для владельца заказа |
| 413 | `PAYLOAD_TOO_LARGE` |
| 422 | `VALIDATION_ERROR`, `UNKNOWN_SKU` |
| 429 | `RATE_LIMITED`; время ожидания есть в `Retry-After` |
| 503 | `PRICE_TIER_NOT_FOUND`, `CATALOG_UNAVAILABLE`, `INVOICE_NOT_CONFIGURED`, `INVOICE_GENERATION_FAILED` или `SERVICE_UNAVAILABLE` |

Не показывайте ответ через `innerHTML`; используйте `textContent`. Передавая `requestId` поддержке, пользователь не раскрывает содержимое заказа.

## Подключение существующей формы Tilda

В архиве не было кода формы, поэтому `examples/tilda-api-client.js` не ищет поля, не нажимает нативную кнопку Tilda и не меняет уже работающий `MutationObserver`. Это самостоятельный транспортный слой, который нужно вызвать из существующего handler после его клиентской валидации. Его fetch использует `credentials: "include"`, чтобы браузер принял и продолжал HttpOnly customer cookie.

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
      // После необходимых действий существующего handler перейти на страницу заказа.
      window.location.assign(result.orderPageUrl);
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

Для страницы заказа скопируйте содержимое `examples/tilda-order-page.html` в HTML-код блока Tilda T123, замените единственный placeholder `API_BASE`, а страницу опубликуйте по URL из `CUSTOMER_ORDER_PAGE_URL`. Пример читает только публичный `number` из query string, делает credentialed request, создаёт DOM через `textContent`/`createElement` и показывает защищённые ссылки просмотра/скачивания invoice. Пока invoice имеет статус `pending`, пример опрашивает customer API раз в 4 секунды, но не более 15 раз; polling прекращается при `generated`, `failed` или timeout. В `localStorage` он сохраняет только `last_order_number`; session token остаётся недоступен JavaScript.

Для production Tilda и API должны работать через HTTPS. При `adrosta.ru` → `api.adrosta.ru` они остаются same-site, поэтому `SameSite=Lax` подходит. В `CORS_ALLOWED_ORIGINS` перечислите каждый реальный origin отдельно, например `https://adrosta.ru,https://www.adrosta.ru`; wildcard и отражение произвольного `Origin` запрещены. Preview-origin добавляйте только если он действительно используется и доверен.

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
| `CUSTOMER_SESSION_COOKIE_NAME` | Имя HttpOnly cookie; по умолчанию `adrosta_customer_session` |
| `CUSTOMER_SESSION_TTL_SECONDS` | Fixed lifetime session; по умолчанию 7776000 секунд (90 дней) |
| `CUSTOMER_SESSION_COOKIE_SECURE` | В production обязательно `true`; local/test может быть `false` |
| `CUSTOMER_SESSION_COOKIE_SAMESITE` | `lax`, `strict` или `none`; `none` требует Secure |
| `CUSTOMER_SESSION_COOKIE_PATH` | Ограниченный cookie path, по умолчанию `/api` |
| `CUSTOMER_ORDER_PAGE_URL` | Относительный `/order` или HTTPS URL опубликованной Tilda-страницы без query |
| `ORDER_RATE_LIMIT_COUNT` | Число запросов одного IP в окне |
| `ORDER_RATE_LIMIT_WINDOW_SECONDS` | Длина rate-limit окна |
| `MAX_REQUEST_BODY_BYTES` | Максимальный размер тела запроса |
| `MAX_ORDER_ITEMS` | Максимум разных SKU |
| `MAX_BOXES_PER_ITEM` | Максимум коробок одной позиции |
| `MAX_TOTAL_BOXES` | Максимум коробок заказа |
| `CDEK_ENV` | Только `test` или `production`; выбирает официальный CDEK API v2 host |
| `CDEK_CLIENT_ID` | Server-side OAuth client/account; задаётся вместе с secret |
| `CDEK_CLIENT_SECRET` | Server-side OAuth secret; никогда не передаётся во frontend |
| `CDEK_FROM_CITY_CODE` | Код города отправления ADROSTA в справочнике CDEK |
| `CDEK_ORIGIN_MODE` | `warehouse` (ADROSTA сдаёт груз) или `door` (забор курьером) |
| `CDEK_HTTP_TIMEOUT_SECONDS` | Таймаут каждого запроса к CDEK, 0.1–60 секунд |
| `SELLER_LEGAL_NAME`, `SELLER_INN`, `SELLER_KPP`, `SELLER_LEGAL_ADDRESS` | Подтверждённые бухгалтером реквизиты продавца для нового invoice snapshot |
| `SELLER_BANK_NAME`, `SELLER_BIK` | Банк продавца и БИК |
| `SELLER_CHECKING_ACCOUNT`, `SELLER_CORRESPONDENT_ACCOUNT` | Расчётный и корреспондентский счета; server-side only |
| `SELLER_PHONE`, `SELLER_EMAIL` | Необязательные контакты продавца в счёте |
| `INVOICE_TAX_TEXT` | Утверждённая бухгалтером налоговая формулировка; backend её не вычисляет |
| `INVOICE_PAYMENT_PURPOSE_TEMPLATE` | Назначение платежа с разрешёнными placeholders номера, даты и order ID |
| `INVOICE_FONT_PATH` | Локальный TTF с кириллицей; Docker использует DejaVu Sans |
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

## Outbox worker

API сохраняет invoice job `invoice.generate` в той же транзакции, что order и invoice snapshot. Worker безопасно повторяет PDF generation после сбоя API/renderer. При `WEBHOOK_ENABLED=true` в той же транзакции также создаётся `order.created`; внешняя недоступность не теряет принятый заказ, а worker повторяет доставку с экспоненциальной задержкой.

```powershell
# Постоянный процесс рядом с API
python -m app.worker

# Одна пачка для cron/диагностики
python -m app.worker --once
```

Для `invoice.generate` worker не обращается к каталогу, CDEK или внешней сети. Для `order.created` он отправляет полный сохранённый заказ, доверенные totals и снимки товаров методом POST. Заголовок `Idempotency-Key` равен `orderId`; при наличии токена используется `Authorization: Bearer …`. Получатель обязан безопасно обрабатывать повтор одного `orderId`. Тела ошибок внешнего сервиса не сохраняются и не логируются.

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

Обычный набор тестов не обращается к CDEK. Необязательный smoke-тест OAuth,
поиска города, ПВЗ и tariff list запускается только с test credentials и никогда
не создаёт отправление:

```powershell
$env:CDEK_ENV="test"
$env:CDEK_CLIENT_ID="..."
$env:CDEK_CLIENT_SECRET="..."
$env:CDEK_FROM_CITY_CODE="..."
python -m pytest -q -m cdek_live
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
- credentialed CORS только для точных origins и отсутствие секретов в клиентском файле;
- hash-only customer sessions, grants, одинаковый отказ для чужого/неизвестного заказа и защищённый PDF.

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
8. Заполнить только подтверждённые бухгалтером `SELLER_*`, `INVOICE_TAX_TEXT` и payment purpose; проверить наличие локального invoice font.
9. Запустить worker для recoverable invoice generation; выбрать режим webhook либо осознанно включить `ALLOW_STORE_ONLY=true`.
10. Выполнить тесты, затем проверить `/health` и `/ready` на целевом окружении.
11. Настроить PostgreSQL backup (`pg_dump`/управляемые snapshots), retention и регулярный тест восстановления.
12. Настроить мониторинг HTTP 5xx, `not_ready`, падения worker и сообщений outbox со статусом `failed`.
13. Определить срок хранения PII, доступ операторов и процедуру удаления/выгрузки заказов.
14. Разместить версионированный `examples/tilda-api-client.js`, встроить вызов в существующий handler и опубликовать страницу Tilda.
15. Сделать реальный тест физлица и business-покупателя, проверить заказ и invoice в PostgreSQL.

## Что нужно предоставить/настроить вручную

- публичный HTTPS URL backend;
- точный список опубликованных доменов Tilda, включая варианты с `www` и отдельный preview-домен только если он действительно нужен;
- список SKU с названием, весом одной коробки и тремя габаритами;
- выбранный PostgreSQL-хостинг, backup/restore, reverse proxy и его доверенные IP;
- назначение заявки: только PostgreSQL или конкретная Tilda/CRM/email система;
- подтверждённые бухгалтером реквизиты продавца, налоговый текст и назначение платежа;
- URL, способ авторизации и ожидаемый контракт внешнего получателя;
- решение, должна ли после успеха backend дополнительно срабатывать текущая нативная отправка Tilda;
- политика хранения персональных данных, резервного копирования и доступа.

Без этих данных нельзя безопасно указывать production CORS, домен, каталог и интеграцию. Значения-примеры из этого README нельзя переносить в production как реальные.
