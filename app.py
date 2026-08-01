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
# Existing PORTAL_* values remain valid as fallbacks for the customer login.
CUSTOMER_USERNAME = os.environ.get(
    "CUSTOMER_USERNAME",
    os.environ.get("PORTAL_USERNAME", "customer"),
)
CUSTOMER_PASSWORD = os.environ.get(
    "CUSTOMER_PASSWORD",
    os.environ.get("PORTAL_PASSWORD", "change-this-password"),
)

INTERNAL_USERNAME = os.environ.get("INTERNAL_USERNAME", "inventory")
INTERNAL_PASSWORD = os.environ.get("INTERNAL_PASSWORD", "change-this-internal-password")

COMPANY_LOGO_URL = os.environ.get("COMPANY_LOGO_URL", "")
COMPANY_PHONE = os.environ.get("COMPANY_PHONE", "787-000-0000")


# =========================
# Warehouse location IDs
# =========================
BAYAMON_LOCATION_ID = 14
SAN_SEBASTIAN_LOCATION_ID = 5


def authenticate_user(credentials: HTTPBasicCredentials = Depends(security)):
    username = credentials.username
    password = credentials.password

    is_internal = (
        secrets.compare_digest(username, INTERNAL_USERNAME)
        and secrets.compare_digest(password, INTERNAL_PASSWORD)
    )

    is_customer = (
        secrets.compare_digest(username, CUSTOMER_USERNAME)
        and secrets.compare_digest(password, CUSTOMER_PASSWORD)
    )

    if is_internal:
        return {
            "username": username,
            "show_exact_stock": True,
            "role": "internal",
        }

    if is_customer:
        return {
            "username": username,
            "show_exact_stock": False,
            "role": "customer",
        }

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Incorrect username or password",
        headers={"WWW-Authenticate": "Basic"},
    )


def require_internal_user(current_user: dict = Depends(authenticate_user)):
    if current_user["role"] != "internal":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Internal access required",
        )

    return current_user


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

    .table-container {
        width: fit-content;
        max-width: 100%;
        margin: 0;
        overflow-x: auto;
        background: white;
    }

    table {
        width: auto;
        min-width: 650px;
        border-collapse: collapse;
        background: white;
        table-layout: auto;
    }

    th,
    td {
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
        cursor: pointer;
        user-select: none;
    }

    th:hover {
        background: #333;
    }

    th.product,
    td.product {
        width: 110px;
        max-width: 110px;
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
    }

    th.description,
    td.description {
        width: 300px;
        max-width: 300px;
        white-space: normal;
        overflow-wrap: break-word;
        word-break: normal;
        line-height: 1.2;
    }

    th.qty,
    td.qty {
        width: 55px;
        max-width: 55px;
        text-align: right;
        white-space: nowrap;
    }

    th.price,
    td.price {
        width: 70px;
        max-width: 70px;
        text-align: right;
        white-space: nowrap;
        font-weight: bold;
    }

    tbody tr:nth-child(even) {
        background: #f7f7f7;
    }

    tbody tr:hover {
        background: #eeeeee;
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

        th,
        td {
            font-size: 11px;
            padding: 4px;
        }

        table {
            min-width: 600px;
        }

        th.product,
        td.product {
            width: 90px;
            max-width: 90px;
        }

        th.description,
        td.description {
            width: 260px;
            max-width: 260px;
        }

        th.qty,
        td.qty {
            width: 50px;
            max-width: 50px;
        }

        th.price,
        td.price {
            width: 65px;
            max-width: 65px;
        }
    }
</style>
</head>
<body>
    <div class="header">
        <img src="/static/logo.png" alt="Company Logo" class="company-logo">

        <div class="company-info">
            <h1>Inventory Availability</h1>
            <div class="phone">San Sebastián: 787-600-0902</div>
            <div class="phone">Bayamón: 787-666-8282</div>
        </div>
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

<div class="table-container">
    <table id="inventoryTable">
        <thead>
            <tr>
                <th class="product">Product</th>
                <th class="description">Description</th>
                <th class="qty">Bayamón</th>
                <th class="qty">San Sebastián</th>
                <th class="price" id="priceHeader" onclick="sortByPrice()">Price ↕</th>
            </tr>
        </thead>
        <tbody>
    {% for item in inventory %}
    <tr>
        <td class="product" title="{{ item.product }}">
            {{ item.product }}
        </td>

        <td class="description" title="{{ item.description }}">
            {{ item.description }}
        </td>

        <td
            class="qty {% if item.bayamon_qty <= 0 %}qty-zero{% else %}qty-positive{% endif %}"
            data-value="{% if show_exact_stock %}{{ item.bayamon_qty }}{% elif item.bayamon_qty > 24 %}25{% else %}{{ item.bayamon_qty }}{% endif %}"
        >
            {% if show_exact_stock %}
                {{ "%.0f"|format(item.bayamon_qty) }}
            {% elif item.bayamon_qty > 24 %}
                24+
            {% else %}
                {{ "%.0f"|format(item.bayamon_qty) }}
            {% endif %}
        </td>

        <td
            class="qty {% if item.san_sebastian_qty <= 0 %}qty-zero{% else %}qty-positive{% endif %}"
            data-value="{% if show_exact_stock %}{{ item.san_sebastian_qty }}{% elif item.san_sebastian_qty > 24 %}25{% else %}{{ item.san_sebastian_qty }}{% endif %}"
        >
            {% if show_exact_stock %}
                {{ "%.0f"|format(item.san_sebastian_qty) }}
            {% elif item.san_sebastian_qty > 24 %}
                24+
            {% else %}
                {{ "%.0f"|format(item.san_sebastian_qty) }}
            {% endif %}
        </td>

        <td
            class="price"
            data-value="{{ item.price }}"
        >
            ${{ "%.2f"|format(item.price) }}
        </td>
    </tr>
    {% endfor %}
</tbody>
    </table>
</div>

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

            // Default display: price from lowest to highest.
            sortByPrice();
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

        let priceSortAscending = true;

        function sortByPrice() {
            const table = document.getElementById("inventoryTable");
            const tbody = table.querySelector("tbody");
            const rows = Array.from(tbody.querySelectorAll("tr"));
            const priceHeader = document.getElementById("priceHeader");

            rows.sort((rowA, rowB) => {
                const priceA = parseFloat(rowA.cells[4].dataset.value) || 0;
                const priceB = parseFloat(rowB.cells[4].dataset.value) || 0;

                return priceSortAscending
                    ? priceA - priceB
                    : priceB - priceA;
            });

            rows.forEach(row => tbody.appendChild(row));

            priceHeader.textContent = priceSortAscending
                ? "Price ↑"
                : "Price ↓";

            priceSortAscending = !priceSortAscending;
        }
    </script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
def inventory_page(current_user: dict = Depends(authenticate_user)):
    inventory = get_inventory()
    last_updated = datetime.now().strftime("%Y-%m-%d %I:%M %p")

    template = Template(HTML_TEMPLATE)
    return template.render(
        inventory=inventory,
        last_updated=last_updated,
        company_logo_url=COMPANY_LOGO_URL,
        company_phone=COMPANY_PHONE,
        show_exact_stock=current_user["show_exact_stock"],
    )


@app.get("/inventory.json")
def inventory_json(current_user: dict = Depends(require_internal_user)):
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
def debug_env(current_user: dict = Depends(require_internal_user)):
    return {
        "odoo_url_set": bool(ODOO_URL),
        "odoo_db_set": bool(ODOO_DB),
        "odoo_username_set": bool(ODOO_USERNAME),
        "odoo_api_key_set": bool(ODOO_API_KEY),
        "customer_username_set": bool(CUSTOMER_USERNAME),
        "customer_password_set": bool(CUSTOMER_PASSWORD),
        "internal_username_set": bool(INTERNAL_USERNAME),
        "internal_password_set": bool(INTERNAL_PASSWORD),
        "company_logo_url_set": bool(COMPANY_LOGO_URL),
        "company_phone_set": bool(COMPANY_PHONE),
    }


@app.get("/debug/locations")
def debug_locations(current_user: dict = Depends(require_internal_user)):
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
def debug_quants(current_user: dict = Depends(require_internal_user)):
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
def debug_products(current_user: dict = Depends(require_internal_user)):
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
