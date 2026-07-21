import { z } from "zod";
const trimmedString = (min, max) => z.string().trim().min(min).max(max);
const phoneSchema = z
    .string()
    .trim()
    .min(10)
    .max(30)
    .refine((value) => value.replace(/\D/g, "").length >= 10, "Телефон должен содержать не менее 10 цифр");
const buyerSchema = z
    .object({
    type: z.enum(["individual", "business"]),
    contactName: trimmedString(2, 150),
    phone: phoneSchema,
    email: z.string().trim().email().max(254)
})
    .strict();
const companySchema = z
    .object({
    name: trimmedString(2, 250),
    inn: z
        .string()
        .trim()
        .regex(/^(\d{10}|\d{12})$/, "ИНН должен содержать 10 или 12 цифр"),
    kpp: z
        .string()
        .trim()
        .refine((value) => value === "" || /^\d{9}$/.test(value), "КПП должен содержать 9 цифр"),
    legalAddress: trimmedString(5, 500)
})
    .strict();
const pointSchema = z
    .object({
    type: z.enum(["pvz", "terminal", "point"]),
    address: trimmedString(3, 500)
})
    .strict();
const addressSchema = z
    .object({
    postcode: z.string().trim().regex(/^\d{6}$/, "Индекс должен содержать 6 цифр"),
    locationType: z.enum([
        "apartment",
        "house",
        "private_house",
        "office",
        "store",
        "warehouse",
        "other"
    ]),
    street: trimmedString(1, 200),
    house: trimmedString(1, 50),
    building: z.string().trim().max(100),
    premises: z.string().trim().max(200),
    workHours: z.string().trim().max(300)
})
    .strict();
const accessRestrictionsSchema = z
    .object({
    hasRestrictions: z.boolean(),
    details: z.string().trim().max(1000)
})
    .strict();
const recipientSchema = z
    .object({
    sameAsBuyer: z.boolean(),
    name: trimmedString(2, 150),
    phone: phoneSchema,
    receivingPointName: z.string().trim().max(250)
})
    .strict();
const deliverySchema = z
    .object({
    receivingMethod: z.enum(["self_pickup", "transport_company"]),
    carrier: z.enum(["cdek", "ozon", "dellin", "pek"]).nullable(),
    carrierLabel: z.string().trim().max(100).nullable(),
    destinationType: z.enum(["point", "door"]).nullable(),
    destinationPointType: z.enum(["pvz", "terminal", "point"]).nullable(),
    region: z.string().trim().max(200),
    city: z.string().trim().max(200),
    point: pointSchema.nullable(),
    address: addressSchema.nullable(),
    unloading: z
        .enum(["self", "carrier", "hydraulic_lift", "special_equipment"])
        .nullable(),
    accessRestrictions: accessRestrictionsSchema.nullable(),
    recipient: recipientSchema
})
    .strict();
const orderItemSchema = z
    .object({
    id: z.string().trim().min(1).max(100),
    sku: z.string().trim().min(1).max(100),
    boxes: z.number().int().min(1).max(1000)
})
    .strict();
const clientTotalsSchema = z
    .object({
    products: z.number().int().nonnegative(),
    boxes: z.number().int().nonnegative(),
    units: z.number().int().nonnegative(),
    amount: z.number().nonnegative()
})
    .strict();
export const wholesaleOrderSchema = z
    .object({
    clientRequestId: z.string().trim().min(8).max(150),
    createdAt: z.string().datetime({ offset: true }),
    buyer: buyerSchema,
    company: companySchema.nullable(),
    delivery: deliverySchema,
    comment: z.string().trim().max(3000),
    items: z.array(orderItemSchema).min(1).max(20),
    clientTotals: clientTotalsSchema.optional()
})
    .strict()
    .superRefine((order, context) => {
    if (order.buyer.type === "business" && order.company === null) {
        context.addIssue({
            code: "custom",
            path: ["company"],
            message: "Для юридического лица или ИП нужны реквизиты организации"
        });
    }
    if (order.buyer.type === "individual" && order.company !== null) {
        context.addIssue({
            code: "custom",
            path: ["company"],
            message: "Для физического лица реквизиты организации должны быть null"
        });
    }
    if (order.delivery.receivingMethod === "self_pickup") {
        return;
    }
    if (!order.delivery.carrier) {
        context.addIssue({
            code: "custom",
            path: ["delivery", "carrier"],
            message: "Выберите транспортную компанию"
        });
    }
    if (!order.delivery.destinationType) {
        context.addIssue({
            code: "custom",
            path: ["delivery", "destinationType"],
            message: "Выберите способ доставки"
        });
    }
    if (!order.delivery.region) {
        context.addIssue({
            code: "custom",
            path: ["delivery", "region"],
            message: "Укажите регион"
        });
    }
    if (!order.delivery.city) {
        context.addIssue({
            code: "custom",
            path: ["delivery", "city"],
            message: "Укажите город"
        });
    }
    if (order.delivery.destinationType === "point" && !order.delivery.point) {
        context.addIssue({
            code: "custom",
            path: ["delivery", "point"],
            message: "Укажите ПВЗ или терминал"
        });
    }
    if (order.delivery.destinationType === "door") {
        if (!order.delivery.address) {
            context.addIssue({
                code: "custom",
                path: ["delivery", "address"],
                message: "Укажите адрес доставки"
            });
        }
        if (!order.delivery.unloading) {
            context.addIssue({
                code: "custom",
                path: ["delivery", "unloading"],
                message: "Укажите вариант разгрузки"
            });
        }
        if (!order.delivery.accessRestrictions) {
            context.addIssue({
                code: "custom",
                path: ["delivery", "accessRestrictions"],
                message: "Укажите наличие ограничений на въезд"
            });
        }
        if (order.delivery.accessRestrictions?.hasRestrictions &&
            !order.delivery.accessRestrictions.details) {
            context.addIssue({
                code: "custom",
                path: ["delivery", "accessRestrictions", "details"],
                message: "Опишите ограничения на въезд"
            });
        }
    }
});
//# sourceMappingURL=order-schema.js.map