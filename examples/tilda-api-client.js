/*
 * Транспортный слой ADROSTA для уже существующего обработчика формы Tilda.
 *
 * Этот файл намеренно не ищет DOM-поля, не нажимает нативную кнопку Tilda и
 * не заменяет MutationObserver существующей формы. Вызовите submit(order) из
 * текущего handler после его клиентской проверки.
 *
 * В frontend допустим только публичный URL API. Не добавляйте сюда токены,
 * пароли, APP_HASH_SECRET или ключи CRM.
 */
(function installAdrostaOrderClient(global) {
  "use strict";

  if (global.AdrostaOrderClient) {
    return;
  }

  var MAX_RESPONSE_CHARS = 65536;
  var DEFAULT_TIMEOUT_MS = 12000;

  function AdrostaOrderError(options) {
    var settings = options || {};
    Error.call(this, settings.message || "Не удалось отправить заказ.");
    this.name = "AdrostaOrderError";
    this.message = settings.message || "Не удалось отправить заказ.";
    this.code = settings.code || "REQUEST_FAILED";
    this.status = Number.isInteger(settings.status) ? settings.status : 0;
    this.details = Array.isArray(settings.details) ? settings.details : [];
    this.requestId = typeof settings.requestId === "string" ? settings.requestId : null;
    this.retryAfter = Number.isInteger(settings.retryAfter) ? settings.retryAfter : null;
    if (Error.captureStackTrace) {
      Error.captureStackTrace(this, AdrostaOrderError);
    }
  }

  AdrostaOrderError.prototype = Object.create(Error.prototype);
  AdrostaOrderError.prototype.constructor = AdrostaOrderError;

  function safeCall(callback, value) {
    if (typeof callback !== "function") {
      return;
    }
    try {
      callback(value);
    } catch (_callbackError) {
      // Ошибка UI-callback не должна менять результат уже выполненного запроса.
    }
  }

  function createUuid() {
    if (!global.crypto || typeof global.crypto.getRandomValues !== "function") {
      throw new AdrostaOrderError({
        code: "SECURE_RANDOM_UNAVAILABLE",
        message: "Браузер не поддерживает безопасную отправку. Обновите браузер."
      });
    }
    if (typeof global.crypto.randomUUID === "function") {
      return global.crypto.randomUUID();
    }

    var bytes = new Uint8Array(16);
    global.crypto.getRandomValues(bytes);
    bytes[6] = (bytes[6] & 15) | 64;
    bytes[8] = (bytes[8] & 63) | 128;
    var hex = Array.prototype.map.call(bytes, function toHex(value) {
      return value.toString(16).padStart(2, "0");
    });
    return (
      hex.slice(0, 4).join("") + "-" +
      hex.slice(4, 6).join("") + "-" +
      hex.slice(6, 8).join("") + "-" +
      hex.slice(8, 10).join("") + "-" +
      hex.slice(10, 16).join("")
    );
  }

  function normalizeEndpoint(value) {
    if (typeof value !== "string" || value.trim() === "") {
      throw new AdrostaOrderError({
        code: "INVALID_ENDPOINT",
        message: "Не настроен адрес сервиса заказов."
      });
    }

    var url;
    try {
      url = new URL(value);
    } catch (_urlError) {
      throw new AdrostaOrderError({
        code: "INVALID_ENDPOINT",
        message: "Адрес сервиса заказов указан неверно."
      });
    }

    var localHttp = url.protocol === "http:" && (
      url.hostname === "localhost" ||
      url.hostname === "127.0.0.1" ||
      url.hostname === "[::1]"
    );
    if (url.protocol !== "https:" && !localHttp) {
      throw new AdrostaOrderError({
        code: "INSECURE_ENDPOINT",
        message: "Сервис заказов должен использовать HTTPS."
      });
    }
    if (url.username || url.password || url.search || url.hash) {
      throw new AdrostaOrderError({
        code: "INVALID_ENDPOINT",
        message: "Адрес сервиса заказов содержит недопустимые параметры."
      });
    }
    return url.toString();
  }

  function requireObject(value, field) {
    if (!value || typeof value !== "object" || Array.isArray(value)) {
      throw new AdrostaOrderError({
        code: "CLIENT_VALIDATION_ERROR",
        message: "Не заполнен раздел формы: " + field + "."
      });
    }
    return value;
  }

  function optional(value) {
    return value === undefined || value === null ? undefined : value;
  }

  function buildDeliveryPayload(delivery) {
    var payload;
    if (delivery.method === "self_pickup") {
      payload = { method: "self_pickup" };
    } else if (delivery.method === "cdek" && delivery.type === "pickup") {
      payload = {
        method: "cdek",
        type: "pickup",
        toCityCode: delivery.toCityCode,
        tariffCode: delivery.tariffCode,
        region: optional(delivery.region),
        city: optional(delivery.city),
        officeCode: delivery.officeCode
      };
    } else if (delivery.method === "cdek" && delivery.type === "door") {
      payload = {
        method: "cdek",
        type: "door",
        toCityCode: delivery.toCityCode,
        tariffCode: delivery.tariffCode,
        region: optional(delivery.region),
        city: delivery.city,
        postcode: optional(delivery.postcode),
        street: delivery.street,
        house: delivery.house,
        apartment: optional(delivery.apartment)
      };
    } else {
      throw new AdrostaOrderError({
        code: "CLIENT_VALIDATION_ERROR",
        message: "Выберите самовывоз или способ доставки СДЭК."
      });
    }

    if (delivery.recipient !== undefined && delivery.recipient !== null) {
      var recipient = requireObject(delivery.recipient, "получатель");
      payload.recipient = {
        contactName: recipient.contactName,
        phone: recipient.phone,
        email: optional(recipient.email)
      };
    }
    return payload;
  }

  function buildPayload(source) {
    var order = requireObject(source, "заказ");
    var buyer = requireObject(order.buyer, "покупатель");
    var delivery = requireObject(order.delivery, "доставка");

    if (!Array.isArray(order.items) || order.items.length === 0) {
      throw new AdrostaOrderError({
        code: "CLIENT_VALIDATION_ERROR",
        message: "Добавьте хотя бы один товар."
      });
    }

    var payload = {
      buyer: {
        type: buyer.type,
        contactName: buyer.contactName,
        phone: buyer.phone,
        email: buyer.email
      },
      delivery: buildDeliveryPayload(delivery),
      comment: optional(order.comment),
      items: order.items.map(function copyTrustedItemFields(item, index) {
        var sourceItem = requireObject(item, "товар " + (index + 1));
        if (typeof sourceItem.sku !== "string" || sourceItem.sku.trim() === "") {
          throw new AdrostaOrderError({
            code: "CLIENT_VALIDATION_ERROR",
            message: "Не указан SKU товара " + (index + 1) + "."
          });
        }
        if (!Number.isInteger(sourceItem.boxes) || sourceItem.boxes < 1) {
          throw new AdrostaOrderError({
            code: "CLIENT_VALIDATION_ERROR",
            message: "Количество коробок товара " + (index + 1) + " указано неверно."
          });
        }
        // Вес, объём, габариты, цена и totals принципиально не копируются.
        return {
          sku: sourceItem.sku,
          boxes: sourceItem.boxes
        };
      })
    };

    if (buyer.type === "business") {
      var company = requireObject(order.company, "организация");
      payload.company = {
        name: company.name,
        inn: company.inn,
        kpp: optional(company.kpp),
        legalAddress: company.legalAddress
      };
    }

    return payload;
  }

  function boundedString(value, fallback, maxLength) {
    if (typeof value !== "string" || value.length === 0 || value.length > maxLength) {
      return fallback;
    }
    return value;
  }

  function sanitizeDetails(value) {
    if (!Array.isArray(value)) {
      return [];
    }
    return value.slice(0, 20).map(function sanitizeDetail(item) {
      if (!item || typeof item !== "object") {
        return null;
      }
      return {
        field: boundedString(item.field, "request", 200),
        code: boundedString(item.code, "INVALID_VALUE", 100),
        message: boundedString(item.message, "Проверьте значение поля.", 300)
      };
    }).filter(Boolean);
  }

  function retryAfterSeconds(response) {
    var raw = response.headers.get("Retry-After");
    if (!raw || !/^\d+$/.test(raw)) {
      return null;
    }
    var seconds = Number(raw);
    return Number.isSafeInteger(seconds) ? seconds : null;
  }

  async function readResponse(response) {
    var text = await response.text();
    if (text.length > MAX_RESPONSE_CHARS) {
      throw new AdrostaOrderError({
        code: "INVALID_SERVER_RESPONSE",
        status: response.status,
        message: "Сервис заказов вернул некорректный ответ."
      });
    }
    if (text === "") {
      return null;
    }
    try {
      return JSON.parse(text);
    } catch (_parseError) {
      throw new AdrostaOrderError({
        code: "INVALID_SERVER_RESPONSE",
        status: response.status,
        message: "Сервис заказов вернул некорректный ответ."
      });
    }
  }

  function errorFromResponse(response, body) {
    var envelope = body && typeof body === "object" ? body.error : null;
    var fallback = response.status === 429
      ? "Слишком много запросов. Попробуйте немного позже."
      : "Не удалось отправить заказ. Попробуйте ещё раз.";
    return new AdrostaOrderError({
      code: boundedString(envelope && envelope.code, "HTTP_" + response.status, 100),
      status: response.status,
      message: boundedString(envelope && envelope.message, fallback, 300),
      details: sanitizeDetails(envelope && envelope.details),
      requestId: boundedString(
        (envelope && envelope.requestId) || response.headers.get("X-Request-ID"),
        "",
        100
      ) || null,
      retryAfter: retryAfterSeconds(response)
    });
  }

  function createSubmission(options) {
    var settings = requireObject(options, "настройки отправки");
    var endpoint = normalizeEndpoint(settings.endpoint);
    var timeoutMs = settings.timeoutMs === undefined
      ? DEFAULT_TIMEOUT_MS
      : settings.timeoutMs;
    if (!Number.isInteger(timeoutMs) || timeoutMs < 1000 || timeoutMs > 60000) {
      throw new AdrostaOrderError({
        code: "INVALID_TIMEOUT",
        message: "Таймаут отправки должен быть от 1 до 60 секунд."
      });
    }

    var idempotencyKey = createUuid();
    var bodySnapshot = null;
    var completedResult = null;
    var inFlight = null;

    function submit(order) {
      if (inFlight) {
        return inFlight;
      }

      var requestBody;
      try {
        if (
          typeof global.fetch !== "function" ||
          typeof global.AbortController !== "function"
        ) {
          throw new AdrostaOrderError({
            code: "BROWSER_UNSUPPORTED",
            message: "Браузер не поддерживает безопасную отправку. Обновите браузер."
          });
        }
        requestBody = JSON.stringify(buildPayload(order));
        if (bodySnapshot !== null && bodySnapshot !== requestBody) {
          throw new AdrostaOrderError({
            code: "SUBMISSION_CHANGED",
            message: "Данные заказа изменились. Начните новую отправку."
          });
        }
        bodySnapshot = requestBody;
      } catch (error) {
        var validationError = error instanceof AdrostaOrderError
          ? error
          : new AdrostaOrderError({
            code: "CLIENT_VALIDATION_ERROR",
            message: "Проверьте заполнение формы."
          });
        safeCall(settings.onError, validationError);
        return Promise.reject(validationError);
      }

      if (completedResult !== null) {
        return Promise.resolve(completedResult);
      }

      var controller = new global.AbortController();
      safeCall(settings.onLoading, true);
      var timedOut = false;
      var timer = global.setTimeout(function abortTimedOutRequest() {
        timedOut = true;
        controller.abort();
      }, timeoutMs);

      inFlight = Promise.resolve().then(function startRequest() {
        return global.fetch(endpoint, {
          method: "POST",
          mode: "cors",
          // Customer session is an HttpOnly cookie issued by the API. JavaScript
          // never reads it; the browser sends it only for credentialed requests.
          credentials: "include",
          cache: "no-store",
          redirect: "error",
          referrerPolicy: "strict-origin-when-cross-origin",
          headers: {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Idempotency-Key": idempotencyKey
          },
          body: requestBody,
          signal: controller.signal
        });
      }).then(async function handleResponse(response) {
        var body = await readResponse(response);
        if (!response.ok) {
          throw errorFromResponse(response, body);
        }
        if (
          !body ||
          typeof body !== "object" ||
          typeof body.orderId !== "string" ||
          typeof body.orderNumber !== "string" ||
          typeof body.orderPageUrl !== "string"
        ) {
          throw new AdrostaOrderError({
            code: "INVALID_SERVER_RESPONSE",
            status: response.status,
            message: "Сервис заказов вернул некорректный ответ."
          });
        }
        completedResult = body;
        safeCall(settings.onSuccess, body);
        return body;
      }).catch(function handleFailure(error) {
        var publicError;
        if (error instanceof AdrostaOrderError) {
          publicError = error;
        } else if (timedOut) {
          publicError = new AdrostaOrderError({
            code: "REQUEST_TIMEOUT",
            message: "Сервис не ответил вовремя. Повторите отправку."
          });
        } else {
          publicError = new AdrostaOrderError({
            code: "NETWORK_ERROR",
            message: "Нет связи с сервисом заказов. Проверьте интернет и повторите отправку."
          });
        }
        safeCall(settings.onError, publicError);
        throw publicError;
      }).finally(function finishRequest() {
        global.clearTimeout(timer);
        inFlight = null;
        safeCall(settings.onLoading, false);
      });

      return inFlight;
    }

    function reset() {
      if (inFlight) {
        throw new AdrostaOrderError({
          code: "REQUEST_IN_PROGRESS",
          message: "Дождитесь завершения текущей отправки."
        });
      }
      idempotencyKey = createUuid();
      bodySnapshot = null;
      completedResult = null;
    }

    return Object.freeze({
      submit: submit,
      reset: reset
    });
  }

  global.AdrostaOrderClient = Object.freeze({
    createSubmission: createSubmission,
    Error: AdrostaOrderError
  });
})(window);
