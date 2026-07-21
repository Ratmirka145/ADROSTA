import { PRODUCT_CATALOG } from "./catalog.js";

export class PricingError extends Error {
  constructor(
    message: string,
    public readonly code: "UNKNOWN_SKU" | "INVALID_QUANTITY"
  ) {
    super(message);
    this.name = "PricingError";
  }
}

export type PricedOrderItem = {
  sku: string;
  name: string;
  boxes: number;
  unitsPerBox: number;
  totalUnits: number;
  pricePerUnit: number;
  pricePerBox: number;
  totalAmount: number;
};

export type PricedOrder = {
  items: PricedOrderItem[];
  totals: {
    products: number;
    boxes: number;
    units: number;
    amount: number;
  };
};

/**
 * Тариф применяется отдельно к количеству коробок каждого SKU.
 *
 * 1–4 коробки: 320 ₽ за средство
 * 5–9 коробок: 290 ₽ за средство
 * 10+ коробок: 260 ₽ за средство
 */
export function getPricePerUnit(boxes: number): number {
  if (!Number.isInteger(boxes) || boxes < 1) {
    throw new PricingError(
      "Количество коробок должно быть положительным целым числом",
      "INVALID_QUANTITY"
    );
  }

  if (boxes >= 10) {
    return 260;
  }

  if (boxes >= 5) {
    return 290;
  }

  return 320;
}

export function calculateOrder(
  sourceItems: ReadonlyArray<{ sku: string; boxes: number }>
): PricedOrder {
  const boxesBySku = new Map<string, number>();

  for (const item of sourceItems) {
    if (!Number.isInteger(item.boxes) || item.boxes < 1) {
      throw new PricingError(
        `Некорректное количество коробок для SKU ${item.sku}`,
        "INVALID_QUANTITY"
      );
    }

    boxesBySku.set(
      item.sku,
      (boxesBySku.get(item.sku) ?? 0) + item.boxes
    );
  }

  const items: PricedOrderItem[] = [];

  for (const [sku, boxes] of boxesBySku) {
    const product = PRODUCT_CATALOG[sku];

    if (!product) {
      throw new PricingError(`Неизвестный SKU: ${sku}`, "UNKNOWN_SKU");
    }

    const pricePerUnit = getPricePerUnit(boxes);
    const totalUnits = boxes * product.unitsPerBox;
    const pricePerBox = pricePerUnit * product.unitsPerBox;
    const totalAmount = boxes * pricePerBox;

    items.push({
      sku,
      name: product.name,
      boxes,
      unitsPerBox: product.unitsPerBox,
      totalUnits,
      pricePerUnit,
      pricePerBox,
      totalAmount
    });
  }

  items.sort((left, right) => left.sku.localeCompare(right.sku));

  const totals = items.reduce(
    (result, item) => {
      result.products += 1;
      result.boxes += item.boxes;
      result.units += item.totalUnits;
      result.amount += item.totalAmount;
      return result;
    },
    {
      products: 0,
      boxes: 0,
      units: 0,
      amount: 0
    }
  );

  return { items, totals };
}
