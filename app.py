import os
import secrets
import xmlrpc.client
from datetime import datetime

from fastapi import FastAPI, Depends, HTTPException, status
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from jinja2 import Template
from fastapi.staticfiles import StaticFiles


app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")
security = HTTPBasic()


# =========================
# Odoo connection settings
# =========================
ODOO_URL = os.environ.get("ODOO_URL")
ODOO_DB = os.environ.get("ODOO_DB")
ODOO_USERNAME = os.environ.get("ODOO_USERNAME")
ODOO_API_KEY = os.environ.get("ODOO_API_KEY")


# =========================
# Portal settings
# =========================
PORTAL_USERNAME = os.environ.get("PORTAL_USERNAME", "customer")
PORTAL_PASSWORD = os.environ.get("PORTAL_PASSWORD", "change-this-password")

COMPANY_LOGO_URL = os.environ.get("COMPANY_LOGO_URL", "")
COMPANY_PHONE = os.environ.get("COMPANY_PHONE", "787-000-0000")


# =========================
# Warehouse location IDs
# =========================
BAYAMON_LOCATION_ID = 14
SAN_SEBASTIAN_LOCATION_ID = 5


def check_password(credentials: HTTPBasicCredentials = Depends(security)):
    correct_username = secrets.compare_digest(credentials.username, PORTAL_USERNAME)
    correct_password = secrets.compare_digest(credentials.password, PORTAL_PASSWORD)

    if not correct_username or not correct_password:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Basic"},
        )

    return credentials.username


def get_odoo_models():
    if not ODOO_URL or not ODOO_DB or not ODOO_USERNAME or not ODOO_API_KEY:
        raise Exception(
            "Missing Odoo environment variables. Check ODOO_URL, ODOO_DB, "
            "ODOO_USERNAME, and ODOO_API_KEY."
        )

    common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
    uid = common.authenticate(ODOO_DB, ODOO_USERNAME, ODOO_API_KEY, {})

    if not uid:
        raise Exception("Odoo authentication failed. Check database, email, API key, and URL.")

    models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")
    return uid, models


def odoo_execute(model, method, args=None, kwargs=None):
    uid, models = get_odoo_models()
    args = args or []
    kwargs = kwargs or {}

    return models.execute_kw(
        ODOO_DB,
        uid,
        ODOO_API_KEY,
        model,
        method,
        args,
        kwargs,
    )


def get_inventory():
    products = odoo_execute(
        "product.product",
        "search_read",
        [[["active", "=", True]]],
        {
            "fields": [
                "id",
                "default_code",
                "name",
                "description_sale",
                "list_price",
            ],
            "limit": 5000,
            "order": "default_code asc",
        },
    )

    product_ids = [product["id"] for product in products]

    if not product_ids:
        return []

    quants = odoo_execute(
        "stock.quant",
        "search_read",
        [[
            ["product_id", "in", product_ids],
            ["location_id", "in", [BAYAMON_LOCATION_ID, SAN_SEBASTIAN_LOCATION_ID]],
        ]],
        {
            "fields": ["product_id", "location_id", "quantity", "reserved_quantity"],
            "limit": 50000,
        },
    )

    stock_by_product = {}

    for product in products:
        product_id = product["id"]

        stock_by_product[product_id] = {
            "product_id": product_id,
            "sku": product.get("default_code") or "",
            "product": product.get("name") or "",
            "description": product.get("description_sale") or "",
            "price": product.get("list_price") or 0.0,
            "bayamon_qty": 0.0,
            "san_sebastian_qty": 0.0,
        }

    for quant in quants:
        product_info = quant.get("product_id")
        location_info = quant.get("location_id")

        if not product_info or not location_info:
            continue

        product_id = product_info[0]
        location_id = location_info[0]

        quantity = quant.get("quantity") or 0.0
        reserved_quantity = quant.get("reserved_quantity") or 0.0

        available_qty = quantity - reserved_quantity

        if product_id not in stock_by_product:
            continue

        if location_id == BAYAMON_LOCATION_ID:
            stock_by_product[product_id]["bayamon_qty"] += available_qty

        elif location_id == SAN_SEBASTIAN_LOCATION_ID:
            stock_by_product[product_id]["san_sebastian_qty"] += available_qty

    inventory = []

    for item in stock_by_product.values():
        inventory.append(item)

    return inventory


HTML_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <title>Inventory Availability</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">

    <style>
        body {
            font-family: Arial, sans-serif;
            margin: 16px;
            background: #f8f8f8;
            color: #222;
        }

        .header {
            display: flex;
            align-items: center;
            gap: 14px;
            margin-bottom: 8px;
        }

        .company-logo {
            max-height: 58px;
            max-width: 180px;
            object-fit: contain;
        }

        .company-info h1 {
            margin: 0;
            font-size: 22px;
        }

        .phone {
            margin-top: 3px;
            font-size: 13px;
            color: #444;
            font-weight: bold;
        }

        .updated {
            margin-bottom: 6px;
            color: #666;
            font-size: 12px;
        }

        .summary {
            margin-bottom: 10px;
            font-size: 12px;
            color: #444;
        }

        .search-box {
            margin-bottom: 10px;
        }

        input {
            padding: 7px;
            width: 350px;
            max-width: 100%;
            font-size: 13px;
            border: 1px solid #ccc;
            border-radius: 4px;
        }

        table {
            width: 100%;
            border-collapse: collapse;
            background: white;
            table-layout: fixed;
        }

        th, td {
            padding: 4px 6px;
            border-bottom: 1px solid #ddd;
            font-size: 12px;
            line-height: 1.15;
            vertical-align: top;
        }

        th {
            background: #222;
            color: white;
            text-align: left;
            position: sticky;
            top: 0;
            z-index: 2;
        }

        th.sku, td.sku {
            width: 105px;
            white-space: nowrap;
            font-weight: bold;
            overflow: hidden;
            text-overflow: ellipsis;
        }

        th.product, td.product {
            width: 230px;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }

        th.description, td.description {
            width: auto;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }

        th.qty, td.qty {
            width: 80px;
            text-align: right;
            white-space: nowrap;
        }

        th.price, td.price {
            width: 90px;
            text-align: right;
            white-space: nowrap;
            font-weight: bold;
        }

        tr:hover {
            background: #f1f1f1;
        }

        .qty-zero {
            color: red;
            font-weight: bold;
        }

        .qty-positive {
            color: #111;
            font-weight: normal;
        }

        @media (max-width: 900px) {
            body {
                margin: 10px;
            }

            .company-info h1 {
                font-size: 18px;
            }

            .company-logo {
                max-height: 45px;
                max-width: 140px;
            }

            th, td {
                font-size: 11px;
                padding: 4px 5px;
            }

            th.sku, td.sku {
                width: 85px;
            }

            th.product, td.product {
                width: 170px;
            }

            th.qty, td.qty {
                width: 65px;
            }

            th.price, td.price {
                width: 75px;
            }
        }
    </style>
</head>
<body>
    <div class="header">
        <img src="/static/logo.png" alt="Company Logo" class="company-logo">

        <div class="company-info">
            <h1>Inventory Availability</h1>
            <div class="phone">Phone: 787-600-0902</div>
            <div class="phone">Phone: 787-666-8282</div>
        </div>
    </div>

    <div class="updated">
        Last updated: {{ last_updated }}
    </div>

    <div class="updated">
        Last updated: {{ last_updated }}
    </div>

    <div class="updated">
        Last updated: {{ last_updated }}
    </div>

    <div class="summary">
        Products shown: {{ inventory|length }}
    </div>

    <div class="search-box">
        <input type="text" id="searchInput" oninput="filterTable()" placeholder="Search by SKU, product, or description...">
    </div>

    <table id="inventoryTable">
        <thead>
            <tr>
                <th class="sku">SKU</th>
                <th class="product">Product</th>
                <th class="description">Description</th>
                <th class="qty">Bayamón</th>
                <th class="qty">San Sebastián</th>
                <th class="price">Price</th>
            </tr>
        </thead>
        <tbody>
            {% for item in inventory %}
            <tr>
                <td class="sku" title="{{ item.sku }}">{{ item.sku }}</td>
                <td class="product" title="{{ item.product }}">{{ item.product }}</td>
                <td class="description" title="{{ item.description }}">{{ item.description }}</td>

                <td class="qty {% if item.bayamon_qty <= 0 %}qty-zero{% else %}qty-positive{% endif %}">
                    {{ "%.0f"|format(item.bayamon_qty) }}
                </td>

                <td class="qty {% if item.san_sebastian_qty <= 0 %}qty-zero{% else %}qty-positive{% endif %}">
                    {{ "%.0f"|format(item.san_sebastian_qty) }}
                </td>

                <td class="price">
                    ${{ "%.2f"|format(item.price) }}
                </td>
            </tr>
            {% endfor %}
        </tbody>
    </table>

    <script>
        let searchTimeout = null;
        let searchableRows = [];

        window.onload = function () {
            const table = document.getElementById("inventoryTable");
            const rows = Array.from(table.querySelectorAll("tbody tr"));

            searchableRows = rows.map(row => {
                return {
                    row: row,
                    text: row.textContent.toLowerCase()
                };
            });
        };

        function filterTable() {
            clearTimeout(searchTimeout);

            searchTimeout = setTimeout(() => {
                const filter = document.getElementById("searchInput").value.toLowerCase().trim();

                for (const item of searchableRows) {
                    item.row.style.display = item.text.includes(filter) ? "" : "none";
                }
            }, 120);
        }
    </script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
def inventory_page(username: str = Depends(check_password)):
    inventory = get_inventory()
    last_updated = datetime.now().strftime("%Y-%m-%d %I:%M %p")

    template = Template(HTML_TEMPLATE)
    return template.render(
        inventory=inventory,
        last_updated=last_updated,
        company_logo_url=COMPANY_LOGO_URL,
        company_phone=COMPANY_PHONE,
    )


@app.get("/inventory.json")
def inventory_json(username: str = Depends(check_password)):
    inventory = get_inventory()

    return JSONResponse({
        "last_updated": datetime.now().isoformat(),
        "bayamon_location_id": BAYAMON_LOCATION_ID,
        "san_sebastian_location_id": SAN_SEBASTIAN_LOCATION_ID,
        "inventory": inventory,
    })


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/debug/env")
def debug_env(username: str = Depends(check_password)):
    return {
        "odoo_url_set": bool(ODOO_URL),
        "odoo_db_set": bool(ODOO_DB),
        "odoo_username_set": bool(ODOO_USERNAME),
        "odoo_api_key_set": bool(ODOO_API_KEY),
        "portal_username_set": bool(PORTAL_USERNAME),
        "portal_password_set": bool(PORTAL_PASSWORD),
        "company_logo_url_set": bool(COMPANY_LOGO_URL),
        "company_phone_set": bool(COMPANY_PHONE),
    }


@app.get("/debug/locations")
def debug_locations(username: str = Depends(check_password)):
    locations = odoo_execute(
        "stock.location",
        "search_read",
        [[["usage", "=", "internal"]]],
        {
            "fields": ["id", "name", "complete_name", "usage"],
            "limit": 500,
            "order": "complete_name asc",
        },
    )

    return JSONResponse(locations)


@app.get("/debug/quants")
def debug_quants(username: str = Depends(check_password)):
    quants = odoo_execute(
        "stock.quant",
        "search_read",
        [[
            ["location_id", "in", [BAYAMON_LOCATION_ID, SAN_SEBASTIAN_LOCATION_ID]],
            ["quantity", ">", 0],
        ]],
        {
            "fields": ["product_id", "location_id", "quantity", "reserved_quantity"],
            "limit": 200,
        },
    )

    return JSONResponse(quants)


@app.get("/debug/products")
def debug_products(username: str = Depends(check_password)):
    products = odoo_execute(
        "product.product",
        "search_read",
        [[["active", "=", True]]],
        {
            "fields": [
                "id",
                "default_code",
                "name",
                "description_sale",
                "list_price",
                "qty_available",
            ],
            "limit": 100,
            "order": "default_code asc",
        },
    )

    return JSONResponse(products)