export type ProductDefinition = {
  sku: string;
  name: string;
  unitsPerBox: number;
};

export const PRODUCT_CATALOG: Readonly<Record<string, ProductDefinition>> = {
  "opt-san-green": {
    sku: "opt-san-green",
    name: "SAN by Adrosta — средство для выгребных ям и дачных туалетов",
    unitsPerBox: 10
  },
  "opt-san-blue": {
    sku: "opt-san-blue",
    name: "SAN by Adrosta — средство для очистки септиков и бетонных колец",
    unitsPerBox: 10
  }
};
