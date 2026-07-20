# ADROSTA Backend — этап 1

Первая рабочая версия backend для оптовой формы ADROSTA.

## Уже работает

- `GET /health` — проверка состояния сервера.
- `POST /api/wholesale-order` — приём заказа из Tilda.
- Проверка структуры входящих данных через Zod.
- Серверный пересчёт цен.
- Запрет неизвестных SKU.
- Объединение повторяющихся строк одного SKU.
- CORS для разрешённых доменов.
- Генерация стабильного тестового номера заказа и счёта.
- Тесты ценовых уровней.

## Пока не подключено

- PostgreSQL.
- Последовательная бухгалтерская нумерация счетов.
- PDF.
- Хранилище PDF.
- Серверная отправка писем.
- Интеграции с транспортными компаниями.

Текущие номера документов подходят для прототипа. Для production нумерацию перенесём в PostgreSQL.

---

## Правило цен

Тариф применяется **отдельно к каждому SKU**:

| Коробки одного SKU | Цена за средство |
|---|---:|
| 1–4 | 320 ₽ |
| 5–9 | 290 ₽ |
| от 10 | 260 ₽ |

Одна коробка содержит 10 средств.

Пример:

- 6 коробок зелёного товара → 290 ₽ за средство.
- 4 коробки синего товара → 320 ₽ за средство.
- Итог: 30 200 ₽.

---

## 1. Установка на Windows

Установи Node.js 24 LTS.

Распакуй проект, открой папку в терминале PowerShell и выполни:

```powershell
Copy-Item .env.example .env
npm install
npm run dev
```

Сервер запустится по адресу:

```text
http://localhost:3000
```

Проверка:

```powershell
Invoke-RestMethod http://localhost:3000/health
```

Ожидаемый ответ:

```json
{
  "success": true,
  "status": "healthy"
}
```

---

## 2. Тестовый заказ

В отдельном окне PowerShell:

```powershell
Invoke-RestMethod `
  -Method Post `
  -Uri "http://localhost:3000/api/wholesale-order" `
  -ContentType "application/json" `
  -InFile ".\samples\order.json"
```

Сервер должен вернуть:

- `success: true`;
- `orderNumber`;
- `invoiceNumber`;
- серверный список товаров;
- сумму `30200`;
- `invoiceUrl: null`.

---

## 3. Тесты

```powershell
npm test
```

---

## 4. Загрузка на GitHub

Создай пустой репозиторий `adrosta-backend`, затем выполни:

```powershell
git init
git add .
git commit -m "Create ADROSTA backend"
git branch -M main
git remote add origin https://github.com/ТВОЙ-ЛОГИН/adrosta-backend.git
git push -u origin main
```

---

## 5. Размещение на Render

1. Открой Render.
2. Нажми **New → Web Service**.
3. Подключи репозиторий `adrosta-backend`.
4. Укажи:

```text
Runtime: Node
Build Command: npm install && npm run build
Start Command: npm start
Health Check Path: /health
```

5. Добавь переменную окружения:

```text
ALLOWED_ORIGINS=https://adrosta.ru,https://www.adrosta.ru,https://adrosta.tilda.ws
```

6. Запусти deploy.

Render выдаст адрес наподобие:

```text
https://adrosta-backend.onrender.com
```

Endpoint станет:

```text
https://adrosta-backend.onrender.com/api/wholesale-order
```

---

## 6. Подключение к Tilda

После успешного deploy в T123 замени:

```javascript
const ENABLE_BACKEND = false;
```

на:

```javascript
const ENABLE_BACKEND = true;
```

И укажи адрес:

```javascript
const SERVER_URL =
  "https://adrosta-backend.onrender.com/api/wholesale-order";
```

Сначала проверь endpoint через PowerShell, и только потом включай его в Tilda.

---

## Следующий этап

Добавление PostgreSQL и PDF-счёта:

1. сохраняем заказ;
2. получаем последовательный номер счёта;
3. создаём PDF;
4. отправляем PDF покупателю и менеджеру;
5. возвращаем `invoiceUrl` в Tilda.
