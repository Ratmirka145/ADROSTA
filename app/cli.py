from __future__ import annotations

import argparse
import sys

from app.config import ConfigError, get_settings
from app.database import Database
from app.repositories import Product, ProductNotFoundError, ProductRepository


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.cli",
        description="Управление локальной базой ADROSTA без JSON-файлов.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init-db", help="Создать таблицы SQLite")

    product = commands.add_parser("product", help="Управление доверенным каталогом")
    product_commands = product.add_subparsers(dest="product_command", required=True)

    upsert = product_commands.add_parser("upsert", help="Добавить или обновить товар")
    upsert.add_argument("--sku", required=True)
    upsert.add_argument("--name", required=True)
    upsert.add_argument("--weight-grams", required=True, type=int)
    upsert.add_argument("--length-mm", required=True, type=int)
    upsert.add_argument("--width-mm", required=True, type=int)
    upsert.add_argument("--height-mm", required=True, type=int)

    listing = product_commands.add_parser("list", help="Показать каталог")
    listing.add_argument("--all", action="store_true", help="Включить неактивные SKU")

    activate = product_commands.add_parser("activate", help="Активировать SKU")
    activate.add_argument("--sku", required=True)
    deactivate = product_commands.add_parser("deactivate", help="Отключить SKU")
    deactivate.add_argument("--sku", required=True)
    return parser


def _database() -> Database:
    settings = get_settings()
    database = Database(
        settings.database_path,
        busy_timeout_ms=settings.sqlite_busy_timeout_ms,
    )
    database.initialize()
    return database


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        database = _database()
        if args.command == "init-db":
            print("База данных готова.")
            return 0

        products = ProductRepository(database)
        if args.product_command == "upsert":
            volume_mm3 = args.length_mm * args.width_mm * args.height_mm
            saved = products.upsert(
                Product(
                    sku=args.sku,
                    name=args.name,
                    box_weight_grams=args.weight_grams,
                    box_volume_mm3=volume_mm3,
                    box_length_mm=args.length_mm,
                    box_width_mm=args.width_mm,
                    box_height_mm=args.height_mm,
                    active=True,
                )
            )
            print(f"SKU {saved.sku} сохранён и активен.")
            return 0

        if args.product_command == "list":
            rows = products.list(active_only=not args.all)
            if not rows:
                print("Каталог пуст.")
                return 0
            print("SKU\tНазвание\tВес, г\tДxШxВ, мм\tАктивен")
            for item in rows:
                dimensions = (
                    f"{item.box_length_mm}x{item.box_width_mm}x{item.box_height_mm}"
                )
                print(
                    f"{item.sku}\t{item.name}\t{item.box_weight_grams}\t"
                    f"{dimensions}\t{'да' if item.active else 'нет'}"
                )
            return 0

        active = args.product_command == "activate"
        saved = products.set_active(args.sku, active)
        state = "активирован" if saved.active else "отключён"
        print(f"SKU {saved.sku} {state}.")
        return 0
    except (ConfigError, ValueError, ProductNotFoundError) as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
