import { createHash } from "node:crypto";
export function createDocumentNumber(prefix, clientRequestId, createdAt) {
    const date = new Date(createdAt);
    if (Number.isNaN(date.getTime())) {
        throw new Error("Некорректная дата заказа");
    }
    const datePart = [
        date.getUTCFullYear(),
        String(date.getUTCMonth() + 1).padStart(2, "0"),
        String(date.getUTCDate()).padStart(2, "0")
    ].join("");
    const hashPart = createHash("sha256")
        .update(clientRequestId)
        .digest("hex")
        .slice(0, 8)
        .toUpperCase();
    return `${prefix}-${datePart}-${hashPart}`;
}
//# sourceMappingURL=document-number.js.map