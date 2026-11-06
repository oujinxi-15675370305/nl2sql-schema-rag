"""生成演示用的 SQLite 数据库（4 张表 + 课程无关的模拟数据）。

用法：python scripts/seed_demo_db.py
"""
from __future__ import annotations

import random
import sqlite3
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "demo.sqlite"

REGIONS = [(1, "华东"), (2, "华南"), (3, "华北"), (4, "西南")]
CATEGORIES = {
    "家电": ["空气炸锅", "扫地机器人", "电饭煲", "净水器", "吹风机", "电磁炉"],
    "数码": ["蓝牙耳机", "机械键盘", "显示器", "移动电源", "摄像头", "路由器"],
    "服饰": ["冲锋衣", "运动鞋", "卫衣", "牛仔裤", "羽绒服", "速干T恤"],
    "食品": ["坚果礼盒", "咖啡豆", "速食面", "蛋白棒", "花草茶", "蜂蜜"],
}
BRANDS = ["自有品牌", "代工白牌", "联名款"]


def build() -> None:
    DB.parent.mkdir(parents=True, exist_ok=True)
    if DB.exists():
        DB.unlink()
    rng = random.Random(20261123)
    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    cur.executescript(
        """
        CREATE TABLE regions (
            region_id INTEGER PRIMARY KEY,
            region_name TEXT NOT NULL
        );
        CREATE TABLE customers (
            customer_id TEXT PRIMARY KEY,
            region_id INTEGER NOT NULL REFERENCES regions(region_id),
            customer_name TEXT NOT NULL
        );
        CREATE TABLE products (
            product_id TEXT PRIMARY KEY,
            product_name TEXT NOT NULL,
            category TEXT NOT NULL,
            brand TEXT NOT NULL,
            list_price REAL NOT NULL
        );
        CREATE TABLE orders (
            order_id TEXT PRIMARY KEY,
            customer_id TEXT NOT NULL,
            region_id INTEGER NOT NULL,
            order_date TEXT NOT NULL,
            status TEXT NOT NULL,
            pay_amount REAL NOT NULL,
            refund_amount REAL NOT NULL,
            cost_amount REAL NOT NULL
        );
        CREATE TABLE order_items (
            order_id TEXT NOT NULL,
            product_id TEXT NOT NULL,
            quantity INTEGER NOT NULL,
            unit_price REAL NOT NULL,
            item_amount REAL NOT NULL
        );
        CREATE INDEX idx_orders_date ON orders(order_date);
        CREATE INDEX idx_orders_region ON orders(region_id);
        """
    )

    cur.executemany("INSERT INTO regions(region_id, region_name) VALUES (?, ?)", REGIONS)
    customers = [
        (f"C{i:04d}", rng.choice([r[0] for r in REGIONS]), f"客户{i:04d}") for i in range(1, 121)
    ]
    cur.executemany("INSERT INTO customers VALUES (?, ?, ?)", customers)

    products = []
    pid = 1
    for cat, names in CATEGORIES.items():
        for name in names:
            price = round(rng.uniform(59, 1599), 2)
            products.append((f"P{pid:03d}", name, cat, rng.choice(BRANDS), price))
            pid += 1
    cur.executemany("INSERT INTO products VALUES (?, ?, ?, ?, ?)", products)

    start = date(2026, 5, 1)
    end = date(2026, 11, 20)
    days = (end - start).days
    orders, items = [], []
    for i in range(1, 801):
        d = start + timedelta(days=rng.randint(0, days))
        cust = rng.choice(customers)
        region_id = cust[1]
        status = rng.choices(["paid", "refunded", "cancelled"], weights=[84, 11, 5])[0]
        chosen = rng.sample(products, rng.randint(1, 3))
        amount = 0.0
        for p in chosen:
            qty = rng.randint(1, 4)
            item_amount = round(p[4] * qty, 2)
            amount += item_amount
            items.append((f"O{i:05d}", p[0], qty, p[4], item_amount))
        refund = round(amount * rng.uniform(0.3, 1.0), 2) if status == "refunded" else 0.0
        cost = round(amount * rng.uniform(0.45, 0.75), 2)
        orders.append((f"O{i:05d}", cust[0], region_id, d.isoformat(), status,
                       round(amount, 2), refund, cost))

    cur.executemany("INSERT INTO orders VALUES (?, ?, ?, ?, ?, ?, ?, ?)", orders)
    cur.executemany("INSERT INTO order_items VALUES (?, ?, ?, ?, ?)", items)
    conn.commit()

    counts = {t: cur.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
              for t in ("regions", "customers", "products", "orders", "order_items")}
    conn.close()
    print(f"已生成 {DB}")
    for k, v in counts.items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    build()
