import express from "express";
import { wholesaleOrderSchema } from "./order-schema.js";
import { calculateOrder, PricingError } from "./pricing.js";
import { createDocumentNumber } from "./document-number.js";
function parseAllowedOrigins() {
    const rawValue = process.env.ALLOWED_ORIGINS ??
        "http://localhost:3000,http://localhost:5500";
    return new Set(rawValue
        .split(",")
        .map((origin) => origin.trim())
        .filter(Boolean));
}
function corsMiddleware(request, response, next) {
    const origin = request.get("origin");
    const allowedOrigins = parseAllowedOrigins();
    if (!origin) {
        next();
        return;
    }
    if (!allowedOrigins.has(origin)) {
        response.status(403).json({
            success: false,
            error: "ORIGIN_NOT_ALLOWED",
            message: "Этот домен не разрешён для обращения к API"
        });
        return;
    }
    response.setHeader("Access-Control-Allow-Origin", origin);
    response.setHeader("Vary", "Origin");
    response.setHeader("Access-Control-Allow-Methods", "GET,POST,OPTIONS");
    response.setHeader("Access-Control-Allow-Headers", "Content-Type, X-Request-ID");
    if (request.method === "OPTIONS") {
        response.sendStatus(204);
        return;
    }
    next();
}
function securityHeaders(_request, response, next) {
    response.setHeader("X-Content-Type-Options", "nosniff");
    response.setHeader("Referrer-Policy", "no-referrer");
    response.setHeader("Cache-Control", "no-store");
    next();
}
export function createApp() {
    const app = express();
    app.disable("x-powered-by");
    app.use(securityHeaders);
    app.use(corsMiddleware);
    app.use(express.json({ limit: "100kb", strict: true }));
    app.get("/", (_request, response) => {
        response.json({
            service: "adrosta-backend",
            status: "ok",
            version: "0.1.0"
        });
    });
    app.get("/health", (_request, response) => {
        response.json({
            success: true,
            status: "healthy",
            timestamp: new Date().toISOString()
        });
    });
    app.post("/api/wholesale-order", (request, response) => {
        const parsedOrder = wholesaleOrderSchema.safeParse(request.body);
        if (!parsedOrder.success) {
            response.status(422).json({
                success: false,
                error: "VALIDATION_ERROR",
                message: "Проверьте данные заказа",
                issues: parsedOrder.error.issues.map((issue) => ({
                    path: issue.path.join("."),
                    message: issue.message
                }))
            });
            return;
        }
        try {
            const order = parsedOrder.data;
            const pricedOrder = calculateOrder(order.items);
            const orderNumber = createDocumentNumber("ADR", order.clientRequestId, order.createdAt);
            const invoiceNumber = createDocumentNumber("INV", order.clientRequestId, order.createdAt);
            const warnings = [];
            if (order.clientTotals &&
                order.clientTotals.amount !== pricedOrder.totals.amount) {
                warnings.push("Сумма на клиенте отличалась от серверного расчёта. Использован серверный расчёт.");
            }
            console.info(JSON.stringify({
                event: "wholesale_order_validated",
                orderNumber,
                clientRequestId: order.clientRequestId,
                buyerType: order.buyer.type,
                email: order.buyer.email,
                boxes: pricedOrder.totals.boxes,
                amount: pricedOrder.totals.amount
            }));
            response.status(201).json({
                success: true,
                stage: "validated",
                message: "Заказ принят и пересчитан сервером. PDF и база данных будут подключены следующим этапом.",
                orderNumber,
                invoiceNumber,
                invoiceUrl: null,
                items: pricedOrder.items,
                totals: pricedOrder.totals,
                warnings
            });
        }
        catch (error) {
            if (error instanceof PricingError) {
                response.status(400).json({
                    success: false,
                    error: error.code,
                    message: error.message
                });
                return;
            }
            throw error;
        }
    });
    app.use((_request, response) => {
        response.status(404).json({
            success: false,
            error: "NOT_FOUND",
            message: "Маршрут не найден"
        });
    });
    const errorHandler = (error, _request, response, _next) => {
        console.error(error);
        if (error instanceof SyntaxError) {
            response.status(400).json({
                success: false,
                error: "INVALID_JSON",
                message: "Сервер получил некорректный JSON"
            });
            return;
        }
        response.status(500).json({
            success: false,
            error: "INTERNAL_ERROR",
            message: "Внутренняя ошибка сервера"
        });
    };
    app.use(errorHandler);
    return app;
}
//# sourceMappingURL=app.js.map