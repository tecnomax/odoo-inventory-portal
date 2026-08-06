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
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Inventory Availability</title>
    <style>
        :root {
            --navy: #0b3b78;
            --navy-dark: #082e61;
            --navy-soft: #174f9b;
            --green: #16a34a;
            --green-soft: #dcfce7;
            --blue-soft: #e8f1ff;
            --border: #dbe3ef;
            --text: #17324d;
            --muted: #64748b;
            --bg: #eef3f8;
            --card: #ffffff;
            --danger: #dc2626;
            --shadow: 0 12px 30px rgba(15, 23, 42, 0.08);
            --radius: 18px;
        }

        * { box-sizing: border-box; }

        body {
            margin: 0;
            font-family: Arial, sans-serif;
            background: linear-gradient(180deg, #f4f7fb 0%, #edf2f7 100%);
            color: var(--text);
        }

        .page-shell {
            max-width: 1460px;
            margin: 0 auto;
            padding: 20px;
        }

        .app-card {
            background: var(--card);
            border: 1px solid rgba(219, 227, 239, 0.95);
            border-radius: 24px;
            overflow: hidden;
            box-shadow: var(--shadow);
        }

        .hero {
            background: linear-gradient(
            105deg,
            #ffffff 0%,
            #ffffff 34%,
            #eaf1f8 43%,
            #88a8ca 62%,
            #0b3b78 100%
    );
            color: #ffffff;
            padding: 18px 28px;
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 20px;
            flex-wrap: wrap;
}

        .hero-left {
            display: flex;
            align-items: center;
            gap: 24px;
            min-width: 0;
        }

        .logo-wrap img {
            display: block;
            height: 64px;
            width: auto;
            object-fit: contain;
        }

        .hero-divider {
            width: 1px;
            height: 58px;
            background: rgba(255, 255, 255, 0.35);
        }

        .hero-title {
            min-width: 0;
        }

        .hero-title h1 {
            margin: 0;
            font-size: 26px;
            line-height: 1.1;
            font-weight: 700;
            color: #1956b8;
        }

        .hero-meta {
            display: flex;
            align-items: center;
            gap: 18px;
            flex-wrap: wrap;
            color: rgba(255, 255, 255, 0.96);
            font-size: 13px;
            font-weight: 600;
        }

        .hero-meta .meta-item {
            display: flex;
            align-items: center;
            gap: 8px;
        }

        .hero-meta .meta-dot {
            width: 6px;
            height: 6px;
            border-radius: 999px;
            background: rgba(255,255,255,0.5);
        }

        .content {
            padding: 22px;
        }

        .status-row {
            display: flex;
            align-items: center;
            gap: 14px;
            flex-wrap: wrap;
            color: var(--muted);
            font-size: 14px;
            margin-bottom: 18px;
        }

        .status-pill {
            display: inline-flex;
            align-items: center;
            gap: 8px;
            background: #f8fbff;
            border: 1px solid var(--border);
            border-radius: 999px;
            padding: 8px 12px;
        }

        .metrics-grid {
            display: grid;
            grid-template-columns: repeat(4, minmax(0, 1fr));
            gap: 18px;
            margin-bottom: 20px;
        }

        .metric-card {
            background: var(--card);
            border: 1px solid var(--border);
            border-radius: var(--radius);
            box-shadow: 0 8px 20px rgba(15, 23, 42, 0.04);
            padding: 20px;
            display: flex;
            align-items: center;
            gap: 16px;
            min-height: 108px;
        }

        .metric-icon {
            width: 64px;
            height: 64px;
            border-radius: 18px;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 28px;
            font-weight: 700;
            flex: 0 0 64px;
        }

        .metric-icon.blue {
            background: var(--blue-soft);
            color: #1956b8;
        }

        .metric-icon.green {
            background: var(--green-soft);
            color: var(--green);
        }

        .metric-copy {
            min-width: 0;
        }

        .metric-label {
            font-size: 14px;
            color: #2d4a66;
            font-weight: 700;
            margin-bottom: 6px;
        }

        .metric-value {
            font-size: 22px;
            font-weight: 800;
            line-height: 1;
            margin-bottom: 6px;
            color: var(--navy);
        }

        .metric-note {
            font-size: 13px;
            color: var(--muted);
        }

        .metric-value.green-text {
            color: var(--green);
        }

        .toolbar-card, .table-card {
            background: var(--card);
            border: 1px solid var(--border);
            border-radius: var(--radius);
            box-shadow: 0 8px 20px rgba(15, 23, 42, 0.04);
        }

        .toolbar-card {
            padding: 18px 20px;
            margin-bottom: 18px;
        }

        .toolbar-grid {
            display: grid;
            grid-template-columns: 1.8fr 1fr 1.2fr 1fr;
            gap: 18px;
            align-items: end;
        }

        .control-group {
            min-width: 0;
        }

        .control-label {
            display: block;
            margin-bottom: 8px;
            font-size: 14px;
            font-weight: 700;
            color: #36516b;
        }

        .search-input, .select-control {
            width: 100%;
            height: 46px;
            border: 1px solid #cfd9e6;
            background: #fff;
            border-radius: 12px;
            padding: 0 14px;
            font-size: 15px;
            color: var(--text);
            outline: none;
            transition: border-color 0.2s ease, box-shadow 0.2s ease;
        }

        .search-input:focus, .select-control:focus {
            border-color: #5d8fda;
            box-shadow: 0 0 0 4px rgba(93, 143, 218, 0.16);
        }

        .search-wrap {
            position: relative;
        }

        .search-wrap .search-input {
            padding-left: 46px;
        }

        .search-icon {
            position: absolute;
            left: 14px;
            top: 50%;
            transform: translateY(-50%);
            color: #7b8da6;
            font-size: 18px;
            pointer-events: none;
        }

        .segmented {
            display: inline-flex;
            width: 100%;
            border: 1px solid #cfd9e6;
            border-radius: 12px;
            overflow: hidden;
            background: #fff;
            height: 46px;
        }

        .segmented button {
            flex: 1 1 0;
            border: none;
            background: transparent;
            font-size: 14px;
            font-weight: 700;
            color: #45617b;
            cursor: pointer;
        }

        .segmented button.active {
            background: #1550b7;
            color: #fff;
        }

        .table-card {
            overflow: hidden;
        }

        .table-scroll {
            overflow-x: auto;
        }

        table {
            width: 100%;
            min-width: 1000px;
            border-collapse: separate;
            border-spacing: 0;
        }

        thead th {
            background: linear-gradient(180deg, var(--navy) 0%, var(--navy-dark) 100%);
            color: #fff;
            padding: 16px 14px;
            font-size: 14px;
            text-align: left;
            font-weight: 700;
            position: sticky;
            top: 0;
            z-index: 2;
        }

        thead th:first-child {
            border-top-left-radius: 14px;
        }

        thead th:last-child {
            border-top-right-radius: 14px;
        }

        tbody td {
            padding: 12px 14px;
            border-bottom: 1px solid #e8eef5;
            font-size: 14px;
            vertical-align: middle;
        }

        tbody tr:nth-child(even) {
            background: #f8fbff;
        }

        tbody tr:hover {
            background: #eef5ff;
        }

        .col-product {
            width: 190px;
            font-weight: 700;
        }

        .col-description {
            width: 430px;
        }

        .col-qty, .col-price {
            width: 140px;
            text-align: center;
            white-space: nowrap;
        }

        .col-price {
            text-align: right;
            font-weight: 800;
        }

        .qty-zero {
            color: var(--danger);
            font-weight: 700;
        }

        .qty-positive {
            color: #243b53;
            font-weight: 700;
        }

        .price-sort-header {
            cursor: pointer;
        }

        .table-footer {
            display: flex;
            justify-content: space-between;
            align-items: center;
            gap: 16px;
            flex-wrap: wrap;
            padding: 14px 18px;
            color: var(--muted);
            font-size: 14px;
            border-top: 1px solid #e8eef5;
            background: #fff;
        }

        .pagination {
            display: flex;
            align-items: center;
            gap: 8px;
            flex-wrap: wrap;
        }

        .page-btn, .nav-btn {
            min-width: 38px;
            height: 38px;
            border-radius: 10px;
            border: 1px solid #d5dfec;
            background: #fff;
            color: #284566;
            font-weight: 700;
            cursor: pointer;
            padding: 0 12px;
        }

        .page-btn.active {
            background: #1550b7;
            color: #fff;
            border-color: #1550b7;
        }

        .page-btn.ellipsis {
            border: none;
            background: transparent;
            cursor: default;
            min-width: auto;
            padding: 0 4px;
        }

        .page-size-wrap {
            display: flex;
            align-items: center;
            gap: 10px;
        }

        .page-size-select {
            height: 40px;
            border: 1px solid #d5dfec;
            border-radius: 10px;
            padding: 0 12px;
            color: #29425f;
            font-size: 14px;
            background: #fff;
        }

        .hidden-row {
            display: none;
        }

        .sr-only {
            position: absolute;
            width: 1px;
            height: 1px;
            padding: 0;
            margin: -1px;
            overflow: hidden;
            clip: rect(0, 0, 0, 0);
            white-space: nowrap;
            border: 0;
        }

        @media (max-width: 1180px) {
            .metrics-grid {
                grid-template-columns: repeat(2, minmax(0, 1fr));
            }

            .toolbar-grid {
                grid-template-columns: 1fr 1fr;
            }
        }

        @media (max-width: 760px) {
            .page-shell {
                padding: 10px;
            }

            .hero {
                padding: 16px;
            }

            .hero-left {
                gap: 16px;
            }

            .hero-divider {
                display: none;
            }

            .hero-title h1 {
                font-size: 22px;
            }

            .content {
                padding: 14px;
            }

            .metrics-grid,
            .toolbar-grid {
                grid-template-columns: 1fr;
            }

            .metric-card {
                min-height: unset;
            }

            .table-footer {
                flex-direction: column;
                align-items: stretch;
            }

            .pagination {
                justify-content: center;
            }

            .page-size-wrap {
                justify-content: space-between;
            }
        }
    </style>
</head>
<body>
    <div class="page-shell">
        <div class="app-card">
            <header class="hero">
                <div class="hero-left">
                    <div class="logo-wrap">
                        <img src="/static/logo.png" alt="QTD logo">
                    </div>
                    <div class="hero-divider"></div>
                    <div class="hero-title">
                        <h1>Inventory Availability</h1>
                    </div>
                </div>

                <div class="hero-meta">
                    <div class="meta-item">📍 <span>San Sebastián: 787-600-0902</span></div>
                    <div class="meta-dot"></div>
                    <div class="meta-item">📍 <span>Bayamón: 787-666-8282</span></div>
                </div>
            </header>

            <div class="content">
                <div class="status-row">
                    <div class="status-pill">🕒 <span>Last updated: {{ last_updated }}</span></div>
                    
                </div>


                <section class="toolbar-card">
                    <div class="toolbar-grid">
                        <div class="control-group">
                            <label class="control-label" for="searchInput">Search</label>
                            <div class="search-wrap">
                                <span class="search-icon">🔎</span>
                                <input class="search-input" type="text" id="searchInput" placeholder="Search by SKU, product, or description...">
                            </div>
                        </div>

                        <div class="control-group">
                            <span class="control-label">Stock Visibility</span>
                            <div class="segmented" aria-label="Stock Visibility">
                                <button type="button" class="active" data-stock-filter="all">All</button>
                                <button type="button" data-stock-filter="in">In Stock</button>
                                <button type="button" data-stock-filter="out">Out of Stock</button>
                            </div>
                        </div>
                    </div>
                </section>

                <section class="table-card">
                    <div class="table-scroll">
                        <table id="inventoryTable">
                            <thead>
                                <tr>
                                    <th class="col-product">Product</th>
                                    <th class="col-description">Description</th>
                                    <th class="col-qty">Bayamón</th>
                                    <th class="col-qty">San Sebastián</th>
                                    <th class="col-price price-sort-header" id="priceHeader">Price ↑</th>
                                </tr>
                            </thead>
                            <tbody>
                                {% for item in inventory %}
                                <tr
                                    data-product="{{ (item.product or '')|lower }}"
                                    data-description="{{ (item.description or '')|lower }}"
                                    data-sku="{{ (item.sku or '')|lower }}"
                                    data-bayamon="{{ item.bayamon_qty }}"
                                    data-san="{{ item.san_sebastian_qty }}"
                                    data-price="{{ item.price }}"
                                >
                                    <td class="col-product" title="{{ item.product }}">{{ item.product }}</td>
                                    <td class="col-description" title="{{ item.description }}">{{ item.description }}</td>

                                    <td class="col-qty {% if item.bayamon_qty <= 0 %}qty-zero{% else %}qty-positive{% endif %}">
                                        {% if show_exact_stock %}
                                            {{ "%.0f"|format(item.bayamon_qty) }}
                                        {% elif item.bayamon_qty > 24 %}
                                            24+
                                        {% else %}
                                            {{ "%.0f"|format(item.bayamon_qty) }}
                                        {% endif %}
                                    </td>

                                    <td class="col-qty {% if item.san_sebastian_qty <= 0 %}qty-zero{% else %}qty-positive{% endif %}">
                                        {% if show_exact_stock %}
                                            {{ "%.0f"|format(item.san_sebastian_qty) }}
                                        {% elif item.san_sebastian_qty > 24 %}
                                            24+
                                        {% else %}
                                            {{ "%.0f"|format(item.san_sebastian_qty) }}
                                        {% endif %}
                                    </td>

                                    <td class="col-price">${{ "%.2f"|format(item.price) }}</td>
                                </tr>
                                {% endfor %}
                            </tbody>
                        </table>
                    </div>

                    <div class="table-footer">
                        <div id="resultsSummary">Showing 1 to 20 of {{ total_products }} products</div>

                        <div class="pagination" id="pagination"></div>

                        <div class="page-size-wrap">
                            <label class="sr-only" for="pageSizeSelect">Rows per page</label>
                            <select id="pageSizeSelect" class="page-size-select">
                                <option value="20" selected>20 per page</option>
                                <option value="50">50 per page</option>
                                <option value="100">100 per page</option>
                            </select>
                        </div>
                    </div>
                </section>
            </div>
        </div>
    </div>

    <script>
        const state = {
            search: "",
            warehouse: "all",
            stockFilter: "all",
            sort: "price_asc",
            page: 1,
            pageSize: 20,
        };

        let allRows = [];
        let filteredRows = [];

        const searchInput = document.getElementById("searchInput");
        const warehouseFilter = document.getElementById("warehouseFilter");
        const stockButtons = Array.from(document.querySelectorAll("[data-stock-filter]"));
        const sortSelect = document.getElementById("sortSelect");
        const pageSizeSelect = document.getElementById("pageSizeSelect");
        const pagination = document.getElementById("pagination");
        const resultsSummary = document.getElementById("resultsSummary");
        const sortStateLabel = document.getElementById("sortStateLabel");
        const tbody = document.querySelector("#inventoryTable tbody");
        const priceHeader = document.getElementById("priceHeader");

        function updateSortLabels() {
            if (state.sort === "price_asc") {
                sortStateLabel.textContent = "Low to High";
                priceHeader.textContent = "Price ↑";
            } else if (state.sort === "price_desc") {
                sortStateLabel.textContent = "High to Low";
                priceHeader.textContent = "Price ↓";
            } else if (state.sort === "product_asc") {
                sortStateLabel.textContent = "Product A to Z";
                priceHeader.textContent = "Price";
            } else {
                sortStateLabel.textContent = "Product Z to A";
                priceHeader.textContent = "Price";
            }
        }

        function rowMatchesFilters(row) {
            const searchBlob = [
                row.dataset.sku,
                row.dataset.product,
                row.dataset.description,
            ].join(" ");

            const bayamon = parseFloat(row.dataset.bayamon || "0");
            const san = parseFloat(row.dataset.san || "0");
            const total = bayamon + san;

            if (state.search && !searchBlob.includes(state.search)) {
                return false;
            }

            if (state.warehouse === "bayamon" && bayamon <= 0) {
                return false;
            }

            if (state.warehouse === "san_sebastian" && san <= 0) {
                return false;
            }

            if (state.stockFilter === "in" && total <= 0) {
                return false;
            }

            if (state.stockFilter === "out" && total > 0) {
                return false;
            }

            return true;
        }

        function sortRows(rows) {
            rows.sort((a, b) => {
                if (state.sort === "price_asc") {
                    return (parseFloat(a.dataset.price || "0") - parseFloat(b.dataset.price || "0"));
                }
                if (state.sort === "price_desc") {
                    return (parseFloat(b.dataset.price || "0") - parseFloat(a.dataset.price || "0"));
                }
                if (state.sort === "product_desc") {
                    return (b.dataset.product || "").localeCompare(a.dataset.product || "", undefined, { numeric: true, sensitivity: "base" });
                }
                return (a.dataset.product || "").localeCompare(b.dataset.product || "", undefined, { numeric: true, sensitivity: "base" });
            });
        }

        function renderPagination(totalPages) {
            pagination.innerHTML = "";

            const prevBtn = document.createElement("button");
            prevBtn.className = "nav-btn";
            prevBtn.textContent = "‹";
            prevBtn.disabled = state.page === 1;
            prevBtn.onclick = () => {
                if (state.page > 1) {
                    state.page -= 1;
                    applyFilters();
                }
            };
            pagination.appendChild(prevBtn);

            const pages = [];
            if (totalPages <= 7) {
                for (let i = 1; i <= totalPages; i++) pages.push(i);
            } else {
                pages.push(1);
                if (state.page > 3) pages.push("...");
                const start = Math.max(2, state.page - 1);
                const end = Math.min(totalPages - 1, state.page + 1);
                for (let i = start; i <= end; i++) pages.push(i);
                if (state.page < totalPages - 2) pages.push("...");
                pages.push(totalPages);
            }

            pages.forEach((pageValue) => {
                const btn = document.createElement("button");
                if (pageValue === "...") {
                    btn.className = "page-btn ellipsis";
                    btn.textContent = "...";
                } else {
                    btn.className = "page-btn" + (pageValue === state.page ? " active" : "");
                    btn.textContent = pageValue;
                    btn.onclick = () => {
                        state.page = pageValue;
                        applyFilters();
                    };
                }
                pagination.appendChild(btn);
            });

            const nextBtn = document.createElement("button");
            nextBtn.className = "nav-btn";
            nextBtn.textContent = "›";
            nextBtn.disabled = state.page >= totalPages;
            nextBtn.onclick = () => {
                if (state.page < totalPages) {
                    state.page += 1;
                    applyFilters();
                }
            };
            pagination.appendChild(nextBtn);
        }

        function applyFilters() {
            filteredRows = allRows.filter(rowMatchesFilters);
            sortRows(filteredRows);

            const total = filteredRows.length;
            const totalPages = Math.max(1, Math.ceil(total / state.pageSize));
            if (state.page > totalPages) {
                state.page = totalPages;
            }

            const startIndex = (state.page - 1) * state.pageSize;
            const endIndex = startIndex + state.pageSize;
            const visibleRows = filteredRows.slice(startIndex, endIndex);

            allRows.forEach((row) => row.classList.add("hidden-row"));
            tbody.innerHTML = "";
            visibleRows.forEach((row) => {
                row.classList.remove("hidden-row");
                tbody.appendChild(row);
            });

            const from = total === 0 ? 0 : startIndex + 1;
            const to = Math.min(endIndex, total);
            resultsSummary.textContent = `Showing ${from} to ${to} of ${total.toLocaleString()} products`;

            renderPagination(totalPages);
            updateSortLabels();
        }

        searchInput.addEventListener("input", (event) => {
            state.search = event.target.value.toLowerCase().trim();
            state.page = 1;
            applyFilters();
        });

        warehouseFilter.addEventListener("change", (event) => {
            state.warehouse = event.target.value;
            state.page = 1;
            applyFilters();
        });

        stockButtons.forEach((button) => {
            button.addEventListener("click", () => {
                stockButtons.forEach((btn) => btn.classList.remove("active"));
                button.classList.add("active");
                state.stockFilter = button.dataset.stockFilter;
                state.page = 1;
                applyFilters();
            });
        });

        sortSelect.addEventListener("change", (event) => {
            state.sort = event.target.value;
            state.page = 1;
            applyFilters();
        });

        pageSizeSelect.addEventListener("change", (event) => {
            state.pageSize = parseInt(event.target.value, 10) || 20;
            state.page = 1;
            applyFilters();
        });

        priceHeader.addEventListener("click", () => {
            state.sort = state.sort === "price_asc" ? "price_desc" : "price_asc";
            sortSelect.value = state.sort;
            state.page = 1;
            applyFilters();
        });

        window.addEventListener("load", () => {
            allRows = Array.from(document.querySelectorAll("#inventoryTable tbody tr"));
            applyFilters();
        });
    </script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
def inventory_page(current_user: dict = Depends(authenticate_user)):
    inventory = get_inventory()
    last_updated = datetime.now().strftime("%Y-%m-%d %I:%M %p")

    total_products = len(inventory)
    bayamon_in_stock_count = sum(1 for item in inventory if (item.get("bayamon_qty") or 0) > 0)
    san_sebastian_in_stock_count = sum(1 for item in inventory if (item.get("san_sebastian_qty") or 0) > 0)

    template = Template(HTML_TEMPLATE)
    return template.render(
        inventory=inventory,
        last_updated=last_updated,
        company_logo_url=COMPANY_LOGO_URL,
        company_phone=COMPANY_PHONE,
        show_exact_stock=current_user["show_exact_stock"],
        total_products=total_products,
        bayamon_in_stock_count=bayamon_in_stock_count,
        san_sebastian_in_stock_count=san_sebastian_in_stock_count,
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
