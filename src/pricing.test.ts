import assert from "node:assert/strict";
import test from "node:test";
import { calculateOrder, getPricePerUnit } from "./pricing.js";

test("цена за средство зависит от количества коробок одного SKU", () => {
  assert.equal(getPricePerUnit(1), 320);
  assert.equal(getPricePerUnit(4), 320);
  assert.equal(getPricePerUnit(5), 290);
  assert.equal(getPricePerUnit(9), 290);
  assert.equal(getPricePerUnit(10), 260);
  assert.equal(getPricePerUnit(100), 260);
});

test("сервер рассчитывает заказ из двух SKU отдельно", () => {
  const result = calculateOrder([
    { sku: "opt-san-green", boxes: 6 },
    { sku: "opt-san-blue", boxes: 4 }
  ]);

  assert.equal(result.totals.boxes, 10);
  assert.equal(result.totals.units, 100);
  assert.equal(result.totals.amount, 30_200);

  const green = result.items.find((item) => item.sku === "opt-san-green");
  const blue = result.items.find((item) => item.sku === "opt-san-blue");

  assert.equal(green?.pricePerUnit, 290);
  assert.equal(green?.totalAmount, 17_400);
  assert.equal(blue?.pricePerUnit, 320);
  assert.equal(blue?.totalAmount, 12_800);
});

test("повторяющиеся строки одного SKU объединяются", () => {
  const result = calculateOrder([
    { sku: "opt-san-green", boxes: 4 },
    { sku: "opt-san-green", boxes: 6 }
  ]);

  assert.equal(result.items.length, 1);
  assert.equal(result.items[0]?.boxes, 10);
  assert.equal(result.items[0]?.pricePerUnit, 260);
  assert.equal(result.totals.amount, 26_000);
});
