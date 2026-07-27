# ==============================================================================
# SCRIPT VERSION MARKER: wc-to-magento-v1-2026-07-22-A
# ==============================================================================
# WOOCOMMERCE -> MAGENTO 2 CSV CONVERTER (ALL-IN-ONE)
#
# WHAT THIS SCRIPT DOES:
#   Reads your WooCommerce export files (Products, Customers/Users, Orders)
#   and converts them into Magento-2-import-ready CSVs.
#
# HOW TO USE:
#   1. Run: python woocommerce_to_magento_converter.py
#   2. It will ask for each file path, one at a time. Press Enter to skip
#      any you don't have.
#   3. Converted files are saved into a "magento_output" folder.
#
# EXPECTED INPUT FORMATS (standard WooCommerce/WordPress exports):
#   - PRODUCTS: the built-in WooCommerce Products > Export CSV
#     (columns like ID, Type, SKU, Name, Categories, Images, Regular price,
#     Stock, Attribute 1 name/value(s), Parent, etc.)
#   - CUSTOMERS: a WordPress users export that includes billing/shipping
#     fields (columns like user_email, first_name, last_name,
#     billing_address_1, billing_city, billing_country, etc.)
#   - ORDERS: a WooCommerce orders export with one row per order and
#     numbered "Product Item N ..." columns for each line item (columns
#     like order_number, status, order_total, billing_*, shipping_*,
#     "Product Item 1 SKU", "Product Item 1 Quantity", etc.)
#
# OUTPUT FILES (in magento_output/):
#   - magento_products_import.csv              -> Entity Type: Products
#   - magento_customers_main_import.csv         -> Entity Type: Customers Main File
#   - magento_customer_addresses_import.csv     -> Entity Type: Customer Addresses
#   - magento_orders_import.csv                 -> via a third-party order-import extension
#
# DESIGN PRINCIPLES (same as the Shopify->Magento converter):
#   - Never invent identity or restricted-value fields (email, name, country).
#     Rows missing these are excluded rather than faked.
#   - Missing free-text fields (city, street, phone) get a clearly-marked
#     placeholder ("N/A" / "0000000000") so real data isn't lost elsewhere
#     in the same row.
#   - Customers are split into two files because Magento's combined
#     "Customers and Addresses (single file)" entity type rejects ANY row
#     with an incomplete address, even a fully blank one.
# ==============================================================================

import pandas as pd
import numpy as np
import os
import re


def upload_one(label):
    """Asks for a local file path in the terminal, returns a DataFrame or
    None if left blank (skipped)."""
    path = input(f"\n>>> Enter path to your WooCommerce '{label}' CSV "
                 f"(or press Enter to skip): ").strip().strip('"')
    if not path:
        print(f"Skipped '{label}'.")
        return None
    if not os.path.isfile(path):
        print(f"File not found: {path} — skipping '{label}'.")
        return None
    df = pd.read_csv(path, dtype=str).fillna("")
    print(f"Loaded '{path}' with {len(df)} rows.")
    return df


# --------------------------------------------------------------------------
# GENERIC COLUMN HELPER (same resilience approach as the Shopify converter)
# --------------------------------------------------------------------------
def resolve_col(df, *candidates):
    lookup = {c.strip().lower(): c for c in df.columns}
    for cand in candidates:
        key = cand.strip().lower()
        if key in lookup:
            return lookup[key]
    return None


def truncate_meta_description(text, limit=255):
    text = text or ""
    if len(text) <= limit:
        return text
    trimmed = text[:limit]
    last_space = trimmed.rfind(" ")
    if last_space > 0:
        trimmed = trimmed[:last_space]
    return trimmed.rstrip(" ,.;:-")


def _clean_phone(value):
    if not value:
        return ""
    return value.strip().lstrip("'").strip()


def make_fallback_sku(name, unique_key, prefix="GEN"):
    if not name:
        return f"{prefix}-{unique_key}"
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", name).strip("-").upper()
    return f"{prefix}-{slug}-{unique_key}"[:64]


# Used when WooCommerce's "Stock" quantity column is blank (common when
# "Manage Stock" isn't enabled on a product) but the product IS marked as
# in-stock — rather than importing qty=0 (which Magento treats as Out of
# Stock on the frontend), we use this default so the product still shows
# as purchasable. Set to "0" if you'd rather leave it as before.
DEFAULT_QTY_WHEN_BLANK = "10"


def resolve_qty(stock_value, is_in_stock_flag):
    """stock_value: raw 'Stock' cell (string, may be blank).
    is_in_stock_flag: the raw 'In stock?' cell value (string, e.g. '1'/'0')."""
    stock_value = (stock_value or "").strip()
    if stock_value:
        return stock_value
    return DEFAULT_QTY_WHEN_BLANK if str(is_in_stock_flag).strip() == "1" else "0"


# ==========================================================================
# PRODUCTS: WooCommerce -> Magento 2 product import format
# ==========================================================================
def convert_products_woocommerce(df):
    df = df.copy()

    c_id       = resolve_col(df, "ID")
    c_type     = resolve_col(df, "Type")
    c_sku      = resolve_col(df, "SKU")
    c_name     = resolve_col(df, "Name")
    c_parent   = resolve_col(df, "Parent")
    c_published = resolve_col(df, "Published")
    c_instock  = resolve_col(df, "In stock?")
    c_stock    = resolve_col(df, "Stock")
    c_regprice = resolve_col(df, "Regular price")
    c_saleprice = resolve_col(df, "Sale price")
    c_categories = resolve_col(df, "Categories")
    c_images   = resolve_col(df, "Images")
    c_desc     = resolve_col(df, "Description")
    c_shortdesc = resolve_col(df, "Short description")
    c_weight   = resolve_col(df, "Weight (lbs)", "Weight (kg)", "Weight")
    c_seo_title = resolve_col(df, "Meta: _yoast_wpseo_title")
    c_seo_desc  = resolve_col(df, "Meta: _yoast_wpseo_metadesc")

    # Attribute columns used to build variation names (Attribute 1..N name/value(s))
    attr_name_cols = [c for c in df.columns if re.match(r"Attribute \d+ name$", c)]
    attr_val_cols  = [c for c in df.columns if re.match(r"Attribute \d+ value\(s\)$", c)]

    def convert_categories(raw):
        if not raw:
            return "Default Category"
        parts = [p.strip() for p in raw.split(",") if p.strip()]
        paths = []
        for p in parts:
            p = p.replace(" > ", "/").replace(">", "/").strip()
            if p and p.lower() != "uncategorized":
                paths.append(f"Default Category/{p}")
        return ",".join(paths) if paths else "Default Category"

    def convert_images(raw):
        if not raw:
            return []
        return [u.strip() for u in raw.split(",") if u.strip()]

    def attr_summary(row):
        parts = []
        for nc, vc in zip(attr_name_cols, attr_val_cols):
            n = row.get(nc, "")
            v = row.get(vc, "")
            if n and v:
                v = v.replace("\\,", ",")   # WooCommerce escapes literal commas inside attribute values as "\,"
                parts.append(v)
        return " ".join(parts)

    magento_rows = []
    seen_urlkeys = set()

    def unique_url_key(base):
        key = base
        i = 2
        while key in seen_urlkeys:
            key = f"{base}-{i}"
            i += 1
        seen_urlkeys.add(key)
        return key

    simple_rows = df[df[c_type] == "simple"] if c_type else df
    variable_rows = df[df[c_type] == "variable"] if c_type else pd.DataFrame(columns=df.columns)
    variation_rows = df[df[c_type] == "variation"] if c_type else pd.DataFrame(columns=df.columns)

    # ---- SIMPLE PRODUCTS ----
    for _, row in simple_rows.iterrows():
        sku = row.get(c_sku, "") if c_sku else ""
        name = row.get(c_name, "") if c_name else ""
        if not sku:
            # Skip products with genuinely no SKU and no name to key off of
            if not name:
                continue
            sku = make_fallback_sku(name, row.get(c_id, "") if c_id else "0")

        images = convert_images(row.get(c_images, "") if c_images else "")
        main_image = images[0] if images else ""
        extra_images = ",".join(images[1:]) if len(images) > 1 else ""

        url_key = unique_url_key(re.sub(r"[^a-z0-9]+", "-", (name or sku).lower()).strip("-") or sku.lower())

        magento_rows.append({
            "sku": sku,
            "store_view_code": "",
            "attribute_set_code": "Default",
            "product_type": "simple",
            "categories": convert_categories(row.get(c_categories, "") if c_categories else ""),
            "product_websites": "base",
            "name": name or sku,
            "description": row.get(c_desc, "") if c_desc else "",
            "short_description": row.get(c_shortdesc, "") if c_shortdesc else "",
            "weight": row.get(c_weight, "0") if c_weight else "0",
            "product_online": "1" if (row.get(c_published, "1") if c_published else "1") == "1" else "0",
            "tax_class_name": "Taxable Goods",
            "visibility": "Catalog, Search",
            "price": row.get(c_regprice, "0") if c_regprice else "0",
            "special_price": row.get(c_saleprice, "") if c_saleprice else "",
            "url_key": url_key,
            "meta_title": truncate_meta_description(row.get(c_seo_title, "") if c_seo_title else "", limit=255),
            "meta_description": truncate_meta_description(row.get(c_seo_desc, "") if c_seo_desc else ""),
            "base_image": main_image,
            "small_image": main_image,
            "thumbnail_image": main_image,
            "additional_images": extra_images,
            "qty": resolve_qty(row.get(c_stock, "") if c_stock else "", row.get(c_instock, "1") if c_instock else "1"),
            "out_of_stock_qty": "0",
            "is_in_stock": "1" if (row.get(c_instock, "1") if c_instock else "1") == "1" else "0",
            "website_id": "1",
            "cost_per_item": "",
        })

    # ---- CONFIGURABLE PRODUCTS (variable parent + variation children) ----
    for _, prow in variable_rows.iterrows():
        parent_sku = prow.get(c_sku, "") if c_sku else ""
        parent_name = prow.get(c_name, "") if c_name else ""
        if not parent_sku:
            if not parent_name:
                continue
            parent_sku = make_fallback_sku(parent_name, prow.get(c_id, "") if c_id else "0", prefix="CONF")

        children = variation_rows[variation_rows[c_parent] == parent_sku] if c_parent else pd.DataFrame(columns=df.columns)
        # Some exports link variations to parent by ID instead of SKU — fall back to that if no match by SKU
        if children.empty and c_id and c_parent:
            parent_id = prow.get(c_id, "")
            children = variation_rows[variation_rows[c_parent] == parent_id]

        images = convert_images(prow.get(c_images, "") if c_images else "")
        main_image = images[0] if images else ""
        extra_images = ",".join(images[1:]) if len(images) > 1 else ""
        categories = convert_categories(prow.get(c_categories, "") if c_categories else "")

        child_skus = []
        for _, vrow in children.iterrows():
            v_sku = vrow.get(c_sku, "") if c_sku else ""
            v_id = vrow.get(c_id, "") if c_id else ""
            if not v_sku:
                v_sku = make_fallback_sku(parent_name, v_id or f"{parent_sku}-{len(child_skus)+1}", prefix="VAR")
            child_skus.append(v_sku)

            v_images = convert_images(vrow.get(c_images, "") if c_images else "")
            v_main_image = v_images[0] if v_images else main_image

            variant_label = attr_summary(vrow)
            child_name = f"{parent_name} - {variant_label}".strip(" -") if variant_label else f"{parent_name} (variant {len(child_skus)})"

            v_url_key = unique_url_key(re.sub(r"[^a-z0-9]+", "-", child_name.lower()).strip("-") or v_sku.lower())

            magento_rows.append({
                "sku": v_sku,
                "store_view_code": "",
                "attribute_set_code": "Default",
                "product_type": "simple",
                "categories": categories,
                "product_websites": "base",
                "name": child_name,
                "description": prow.get(c_desc, "") if c_desc else "",
                "short_description": prow.get(c_shortdesc, "") if c_shortdesc else "",
                "weight": vrow.get(c_weight, "0") if c_weight else "0",
                "product_online": "1" if (prow.get(c_published, "1") if c_published else "1") == "1" else "0",
                "tax_class_name": "Taxable Goods",
                "visibility": "Not Visible Individually",
                "price": vrow.get(c_regprice, "0") if c_regprice else "0",
                "special_price": vrow.get(c_saleprice, "") if c_saleprice else "",
                "url_key": v_url_key,
                "base_image": v_main_image,
                "small_image": v_main_image,
                "thumbnail_image": v_main_image,
                "qty": resolve_qty(vrow.get(c_stock, "") if c_stock else "", vrow.get(c_instock, "1") if c_instock else "1"),
                "out_of_stock_qty": "0",
                "is_in_stock": "1" if (vrow.get(c_instock, "1") if c_instock else "1") == "1" else "0",
                "website_id": "1",
                "cost_per_item": "",
                # NOTE: uses attribute value text directly as the label; if you
                # have a real Magento super-attribute (e.g. "cri") already set
                # up, you may want to align this value with its option labels.
                "additional_attributes": f"variation_label={variant_label}" if variant_label else "",
            })

        parent_url_key = unique_url_key(re.sub(r"[^a-z0-9]+", "-", (parent_name or parent_sku).lower()).strip("-") or parent_sku.lower())

        magento_rows.append({
            "sku": parent_sku,
            "store_view_code": "",
            "attribute_set_code": "Default",
            "product_type": "configurable",
            "categories": categories,
            "product_websites": "base",
            "name": parent_name or parent_sku,
            "description": prow.get(c_desc, "") if c_desc else "",
            "short_description": prow.get(c_shortdesc, "") if c_shortdesc else "",
            "product_online": "1" if (prow.get(c_published, "1") if c_published else "1") == "1" else "0",
            "tax_class_name": "Taxable Goods",
            "visibility": "Catalog, Search",
            "url_key": parent_url_key,
            "base_image": main_image,
            "small_image": main_image,
            "thumbnail_image": main_image,
            "additional_images": extra_images,
            "website_id": "1",
            # NOTE: requires the super attribute used here to already exist
            # as a Magento product attribute (e.g. create a "cri" dropdown
            # attribute) before importing, or Magento will reject this row.
            "configurable_variations": "|".join([f"sku={s}" for s in child_skus]) if child_skus else "",
        })

    return pd.DataFrame(magento_rows)


# ==========================================================================
# CUSTOMERS: WooCommerce -> Magento 2 customer import format
# Split into two files (main + addresses) for the same reason as the
# Shopify converter: Magento's combined single-file entity type rejects
# ANY row with an incomplete address, even a fully blank one.
# ==========================================================================
def _extract_wc_customer_fields(df):
    c_email    = resolve_col(df, "user_email", "Email")
    c_fname    = resolve_col(df, "first_name", "First Name")
    c_lname    = resolve_col(df, "last_name", "Last Name")
    c_bill_fname = resolve_col(df, "billing_first_name")
    c_bill_lname = resolve_col(df, "billing_last_name")
    c_company  = resolve_col(df, "billing_company")
    c_phone    = resolve_col(df, "billing_phone")
    c_addr1    = resolve_col(df, "billing_address_1")
    c_addr2    = resolve_col(df, "billing_address_2")
    c_city     = resolve_col(df, "billing_city")
    c_state    = resolve_col(df, "billing_state")
    c_country  = resolve_col(df, "billing_country")
    c_zip      = resolve_col(df, "billing_postcode")

    # Shipping address columns (separate from billing) — optional, only
    # used if present and meaningfully different from billing.
    c_ship_fname = resolve_col(df, "shipping_first_name")
    c_ship_lname = resolve_col(df, "shipping_last_name")
    c_ship_company = resolve_col(df, "shipping_company")
    c_ship_phone = resolve_col(df, "shipping_phone")
    c_ship_addr1 = resolve_col(df, "shipping_address_1")
    c_ship_addr2 = resolve_col(df, "shipping_address_2")
    c_ship_city  = resolve_col(df, "shipping_city")
    c_ship_state = resolve_col(df, "shipping_state")
    c_ship_country = resolve_col(df, "shipping_country")
    c_ship_zip   = resolve_col(df, "shipping_postcode")

    # Original account registration date — optional, preserved if present
    # so Magento shows the customer's real signup date instead of the
    # import date.
    c_registered = resolve_col(df, "user_registered")

    rows = []
    skipped_no_email = 0
    for _, row in df.iterrows():
        email_val = row.get(c_email, "") if c_email else ""
        if not email_val:
            skipped_no_email += 1
            continue

        fname = (row.get(c_fname, "") if c_fname else "") or (row.get(c_bill_fname, "") if c_bill_fname else "")
        lname = (row.get(c_lname, "") if c_lname else "") or (row.get(c_bill_lname, "") if c_bill_lname else "")

        street = " ".join(filter(None, [
            row.get(c_addr1, "") if c_addr1 else "",
            row.get(c_addr2, "") if c_addr2 else "",
        ]))
        phone_val = _clean_phone(row.get(c_phone, "") if c_phone else "")

        ship_fname = row.get(c_ship_fname, "") if c_ship_fname else ""
        ship_lname = row.get(c_ship_lname, "") if c_ship_lname else ""
        ship_street = " ".join(filter(None, [
            row.get(c_ship_addr1, "") if c_ship_addr1 else "",
            row.get(c_ship_addr2, "") if c_ship_addr2 else "",
        ]))
        ship_phone = _clean_phone(row.get(c_ship_phone, "") if c_ship_phone else "")

        registered_raw = row.get(c_registered, "") if c_registered else ""
        # Only keep it if it actually looks like a date (avoid saving junk
        # into Magento's created_at if the column had something unexpected)
        registered_date = registered_raw if re.match(r"^\d{4}-\d{2}-\d{2}", registered_raw.strip()) else ""

        rows.append({
            "email": email_val,
            "firstname": fname,
            "lastname": lname,
            "company": row.get(c_company, "") if c_company else "",
            "street": street,
            "city": row.get(c_city, "") if c_city else "",
            "region": row.get(c_state, "") if c_state else "",
            "country_id": row.get(c_country, "") if c_country else "",
            "postcode": row.get(c_zip, "") if c_zip else "",
            "telephone": phone_val,
            "registered_date": registered_date,
            # Shipping block — every value defaults to "" if the column
            # doesn't exist or is blank for this row; never invented.
            "ship_firstname": ship_fname,
            "ship_lastname": ship_lname,
            "ship_company": row.get(c_ship_company, "") if c_ship_company else "",
            "ship_street": ship_street,
            "ship_city": row.get(c_ship_city, "") if c_ship_city else "",
            "ship_region": row.get(c_ship_state, "") if c_ship_state else "",
            "ship_country_id": row.get(c_ship_country, "") if c_ship_country else "",
            "ship_postcode": row.get(c_ship_zip, "") if c_ship_zip else "",
            "ship_telephone": ship_phone,
        })
    if skipped_no_email:
        print(f"NOTE: {skipped_no_email} customer row(s) had NO email — excluded entirely "
              f"(email is required, nothing was invented).")
    return rows


def convert_customers_main_woocommerce(df):
    extracted = _extract_wc_customer_fields(df)
    magento_rows = []
    skipped_no_name = 0
    for r in extracted:
        if not r["firstname"] or not r["lastname"]:
            skipped_no_name += 1
            continue
        magento_rows.append({
            "email": r["email"],
            "_website": "base",
            "_store": "default",
            "confirmation": "",
            "created_at": r["registered_date"],
            "created_in": "Default Store View",
            "disable_auto_group_change": "0",
            "dob": "",
            "firstname": r["firstname"],
            "gender": "",
            "group_id": "1",
            "lastname": r["lastname"],
            "middlename": "",
            "password_hash": "",
            "prefix": "",
            "rp_token": "",
            "rp_token_created_at": "",
            "store_id": "1",
            "suffix": "",
            "taxvat": "",
            "website_id": "1",
            "password": "",
        })
    if skipped_no_name:
        print(f"NOTE: {skipped_no_name} customer(s) had no first/last name and were "
              f"excluded entirely (name is a real identity field, not placeholder-filled).")
    return pd.DataFrame(magento_rows)


def convert_customers_addresses_woocommerce(df, telephone_placeholder="0000000000",
                                             text_placeholder="N/A"):
    extracted = _extract_wc_customer_fields(df)
    magento_rows = []
    placeholder_used_count = 0
    skipped_no_country = 0
    skipped_no_name = 0
    shipping_rows_added = 0

    for r in extracted:
        if not r["firstname"] or not r["lastname"]:
            skipped_no_name += 1
            continue
        if not r["country_id"]:
            skipped_no_country += 1
            continue

        used_placeholder = False
        def fill(value, placeholder):
            nonlocal used_placeholder
            if value:
                return value
            used_placeholder = True
            return placeholder

        street_val = fill(r["street"], text_placeholder)
        city_val   = fill(r["city"], text_placeholder)
        phone_val  = fill(r["telephone"], telephone_placeholder)

        if used_placeholder:
            placeholder_used_count += 1

        # Does this customer have a shipping address that's actually
        # different from billing? Only add a second address row if so —
        # never add a blank/duplicate row. Also never invent a shipping
        # country: if shipping country is missing, no shipping row is added
        # (the billing address alone still covers the customer).
        has_distinct_shipping = bool(r["ship_country_id"]) and (
            r["ship_street"] != r["street"]
            or r["ship_city"] != r["city"]
            or r["ship_country_id"] != r["country_id"]
        )

        # If there's no separate shipping address, the billing address
        # doubles as both default billing AND default shipping (as before).
        # If there IS a separate shipping row, billing keeps default_billing
        # only, and the new row takes default_shipping — avoiding two
        # addresses both claiming to be the default shipping address.
        magento_rows.append({
            "email": r["email"],
            "_website": "base",
            "_address_city": city_val,
            "_address_company": r["company"],
            "_address_country_id": r["country_id"],
            "_address_fax": "",
            "_address_firstname": r["firstname"],
            "_address_lastname": r["lastname"],
            "_address_middlename": "",
            "_address_postcode": r["postcode"],
            "_address_prefix": "",
            "_address_region": r["region"],
            "_address_street": street_val,
            "_address_suffix": "",
            "_address_telephone": phone_val,
            "_address_vat_id": "",
            "_address_default_billing_": "1",
            "_address_default_shipping_": "" if has_distinct_shipping else "1",
        })

        if has_distinct_shipping:
            ship_used_placeholder = False
            def fill_ship(value, placeholder):
                nonlocal ship_used_placeholder
                if value:
                    return value
                ship_used_placeholder = True
                return placeholder

            ship_street_val = fill_ship(r["ship_street"], text_placeholder)
            ship_city_val   = fill_ship(r["ship_city"], text_placeholder)
            ship_phone_val  = fill_ship(r["ship_telephone"] or r["telephone"], telephone_placeholder)
            ship_fname = r["ship_firstname"] or r["firstname"]
            ship_lname = r["ship_lastname"] or r["lastname"]

            if ship_used_placeholder:
                placeholder_used_count += 1

            magento_rows.append({
                "email": r["email"],
                "_website": "base",
                "_address_city": ship_city_val,
                "_address_company": r["ship_company"],
                "_address_country_id": r["ship_country_id"],
                "_address_fax": "",
                "_address_firstname": ship_fname,
                "_address_lastname": ship_lname,
                "_address_middlename": "",
                "_address_postcode": r["ship_postcode"],
                "_address_prefix": "",
                "_address_region": r["ship_region"],
                "_address_street": ship_street_val,
                "_address_suffix": "",
                "_address_telephone": ship_phone_val,
                "_address_vat_id": "",
                "_address_default_billing_": "",
                "_address_default_shipping_": "1",
            })
            shipping_rows_added += 1

    if shipping_rows_added:
        print(f"NOTE: {shipping_rows_added} customer(s) had a shipping address different from "
              f"billing — added as a separate address row (both linked to the same email).")
    if skipped_no_country:
        print(f"NOTE: {skipped_no_country} customer(s) had no country — excluded from this "
              f"address file entirely (country_id can't be faked; Magento only accepts real "
              f"country codes). Their core account still exists via the main customers file.")
    if placeholder_used_count:
        print(f"NOTE: {placeholder_used_count} address row(s) had a missing free-text field "
              f"(city/street/phone), filled with a PLACEHOLDER ('{text_placeholder}' / "
              f"'{telephone_placeholder}') so no real data was lost.")
    return pd.DataFrame(magento_rows)


# ==========================================================================
# ORDERS: WooCommerce -> Magento "Order with Items" format
# WooCommerce exports one ROW PER ORDER with numbered "Product Item N ..."
# columns for line items. We reshape this into one row per line item to
# match Magento's expected layout (same 49 columns used by the Shopify
# converter).
# ==========================================================================
STATUS_MAP = {
    "completed": ("complete", "complete"),
    "processing": ("processing", "processing"),
    "pending": ("pending", "new"),
    "on-hold": ("holded", "holded"),
    "cancelled": ("canceled", "canceled"),
    "refunded": ("closed", "closed"),
    "failed": ("canceled", "canceled"),
}


def convert_orders_woocommerce(df, skip_missing_sku=True):
    c_order_num = resolve_col(df, "order_number", "order_id")
    c_status    = resolve_col(df, "status")
    c_date      = resolve_col(df, "order_date")
    c_currency  = resolve_col(df, "order_currency")
    c_subtotal  = resolve_col(df, "order_subtotal")
    c_shipping  = resolve_col(df, "shipping_total")
    c_tax       = resolve_col(df, "tax_total")
    c_discount  = resolve_col(df, "discount_total")
    c_total     = resolve_col(df, "order_total")
    c_paid_date = resolve_col(df, "paid_date")
    c_payment   = resolve_col(df, "payment_method_title", "payment_method")
    c_ship_method = resolve_col(df, "shipping_method")
    c_coupon    = resolve_col(df, "coupon_items")
    c_email     = resolve_col(df, "customer_email")

    c_bill_fname = resolve_col(df, "billing_first_name")
    c_bill_lname = resolve_col(df, "billing_last_name")
    c_bill_addr1 = resolve_col(df, "billing_address_1")
    c_bill_addr2 = resolve_col(df, "billing_address_2")
    c_bill_city  = resolve_col(df, "billing_city")
    c_bill_state = resolve_col(df, "billing_state")
    c_bill_zip   = resolve_col(df, "billing_postcode")
    c_bill_country = resolve_col(df, "billing_country")
    c_bill_phone = resolve_col(df, "billing_phone")

    c_ship_fname = resolve_col(df, "shipping_first_name")
    c_ship_lname = resolve_col(df, "shipping_last_name")
    c_ship_addr1 = resolve_col(df, "shipping_address_1")
    c_ship_addr2 = resolve_col(df, "shipping_address_2")
    c_ship_city  = resolve_col(df, "shipping_city")
    c_ship_state = resolve_col(df, "shipping_state")
    c_ship_zip   = resolve_col(df, "shipping_postcode")
    c_ship_country = resolve_col(df, "shipping_country")

    # Detect how many "Product Item N ..." sets exist
    item_indices = []
    i = 1
    while resolve_col(df, f"Product Item {i} SKU") or resolve_col(df, f"Product Item {i} Name"):
        item_indices.append(i)
        i += 1

    magento_rows = []
    skipped_count = 0
    aggregate_fallback_count = 0

    for _, row in df.iterrows():
        order_number = row.get(c_order_num, "") if c_order_num else ""
        status = str(row.get(c_status, "") if c_status else "").lower().strip()
        order_status, order_state = STATUS_MAP.get(status, ("pending", "new"))

        bill_fname = row.get(c_bill_fname, "") if c_bill_fname else ""
        bill_lname = row.get(c_bill_lname, "") if c_bill_lname else ""
        billing_name = f"{bill_fname} {bill_lname}".strip()

        ship_fname = row.get(c_ship_fname, "") if c_ship_fname else ""
        ship_lname = row.get(c_ship_lname, "") if c_ship_lname else ""
        shipping_name = f"{ship_fname} {ship_lname}".strip() or billing_name

        billing_street = " ".join(filter(None, [
            row.get(c_bill_addr1, "") if c_bill_addr1 else "",
            row.get(c_bill_addr2, "") if c_bill_addr2 else "",
        ]))
        shipping_street = " ".join(filter(None, [
            row.get(c_ship_addr1, "") if c_ship_addr1 else "",
            row.get(c_ship_addr2, "") if c_ship_addr2 else "",
        ])) or billing_street

        is_paid = bool(row.get(c_paid_date, "") if c_paid_date else "") or status == "completed"

        base_order_fields = {
            "Order #": order_number,
            "Order Status": order_status,
            "Order State": order_state,
            "Order Date": row.get(c_date, "") if c_date else "",
            "Last Updated": row.get(c_date, "") if c_date else "",
            "Store": "Main Website\nMain Website Store\nDefault Store View",  # TODO: confirm your real store path
            "Customer Name": billing_name,
            "Customer Email": row.get(c_email, "") if c_email else "",
            "Customer Group": "General",
            "Billing Name": billing_name,
            "Billing Street": billing_street,
            "Billing City": row.get(c_bill_city, "") if c_bill_city else "",
            "Billing Region": row.get(c_bill_state, "") if c_bill_state else "",
            "Billing Postcode": row.get(c_bill_zip, "") if c_bill_zip else "",
            "Billing Country": row.get(c_bill_country, "") if c_bill_country else "",
            "Billing Phone": row.get(c_bill_phone, "") if c_bill_phone else "",
            "Shipping Name": shipping_name,
            "Shipping Street": shipping_street,
            "Shipping City": (row.get(c_ship_city, "") if c_ship_city else "") or (row.get(c_bill_city, "") if c_bill_city else ""),
            "Shipping Region": (row.get(c_ship_state, "") if c_ship_state else "") or (row.get(c_bill_state, "") if c_bill_state else ""),
            "Shipping Postcode": (row.get(c_ship_zip, "") if c_ship_zip else "") or (row.get(c_bill_zip, "") if c_bill_zip else ""),
            "Shipping Country": (row.get(c_ship_country, "") if c_ship_country else "") or (row.get(c_bill_country, "") if c_bill_country else ""),
            "Shipping Method": row.get(c_ship_method, "") if c_ship_method else "",
            "Payment Method": row.get(c_payment, "") if c_payment else "",
            "Coupon Code": row.get(c_coupon, "") if c_coupon else "",
            "Subtotal": row.get(c_subtotal, "") if c_subtotal else "",
            "Discount": row.get(c_discount, "0") if c_discount else "0",
            "Shipping Amount": row.get(c_shipping, "0") if c_shipping else "0",
            "Tax Amount": row.get(c_tax, "0") if c_tax else "0",
            "Grand Total": row.get(c_total, "") if c_total else "",
            "Total Invoiced": (row.get(c_total, "") if c_total else "") if is_paid else "0",
            "Total Refunded": "0",
            "Currency": row.get(c_currency, "") if c_currency else "",
        }

        order_line_count = 0
        for idx in item_indices:
            c_item_name = resolve_col(df, f"Product Item {idx} Name")
            c_item_sku  = resolve_col(df, f"Product Item {idx} SKU")
            c_item_qty  = resolve_col(df, f"Product Item {idx} Quantity")
            c_item_total = resolve_col(df, f"Product Item {idx} Total")
            c_item_subtotal = resolve_col(df, f"Product Item {idx} Subtotal")

            item_name = row.get(c_item_name, "") if c_item_name else ""
            item_sku  = row.get(c_item_sku, "") if c_item_sku else ""
            if not item_name and not item_sku:
                continue   # this numbered slot is empty for this order

            if not item_sku:
                if skip_missing_sku:
                    skipped_count += 1
                    continue
                else:
                    item_sku = make_fallback_sku(item_name, f"{order_number}-{idx}")

            qty = row.get(c_item_qty, "1") if c_item_qty else "1"
            qty = qty or "1"
            item_total = row.get(c_item_total, "0") if c_item_total else "0"
            item_subtotal = row.get(c_item_subtotal, "") if c_item_subtotal else ""
            price_basis = item_subtotal or item_total or "0"
            try:
                unit_price = float(price_basis) / float(qty) if float(qty) else float(price_basis)
            except (ValueError, ZeroDivisionError):
                unit_price = 0
            try:
                row_total_val = float(item_total) if item_total else unit_price * float(qty)
            except ValueError:
                row_total_val = 0

            line_row = dict(base_order_fields)
            line_row.update({
                "Item SKU": item_sku,
                "Item Name": item_name or item_sku,
                "Item Type": "simple",
                "Qty Ordered": qty,
                "Qty Invoiced": qty if is_paid else "0",
                "Qty Shipped": qty if is_paid else "0",
                "Qty Refunded": "0",
                "Qty Canceled": "0",
                "Original Price": round(unit_price, 4),
                "Item Price": round(unit_price, 4),
                "Item Discount": "0",
                "Item Tax": "0",
                "Row Total": round(row_total_val, 2),
                "Row Total (incl. Tax)": round(row_total_val, 2),
                "Product ID": "",
                "Item Options": "",
            })
            magento_rows.append(line_row)
            order_line_count += 1

        if order_line_count == 0:
            # No valid line item survived for this order (e.g. all missing
            # SKU) — add one aggregate line from the order subtotal instead
            # of silently dropping the whole order.
            aggregate_fallback_count += 1
            fallback_sku = make_fallback_sku("Order Items", order_number, prefix="ORDER")
            subtotal_val = row.get(c_subtotal, "0") if c_subtotal else "0"
            try:
                subtotal_num = float(subtotal_val) if subtotal_val else 0.0
            except ValueError:
                subtotal_num = 0.0
            line_row = dict(base_order_fields)
            line_row.update({
                "Item SKU": fallback_sku,
                "Item Name": "Order Items (aggregate — original line items had no SKU)",
                "Item Type": "simple",
                "Qty Ordered": "1",
                "Qty Invoiced": "1" if is_paid else "0",
                "Qty Shipped": "1" if is_paid else "0",
                "Qty Refunded": "0",
                "Qty Canceled": "0",
                "Original Price": round(subtotal_num, 2),
                "Item Price": round(subtotal_num, 2),
                "Item Discount": "0",
                "Item Tax": "0",
                "Row Total": round(subtotal_num, 2),
                "Row Total (incl. Tax)": round(subtotal_num, 2),
                "Product ID": "",
                "Item Options": "",
            })
            magento_rows.append(line_row)

    if skip_missing_sku and skipped_count:
        print(f"NOTE: Skipped {skipped_count} order line-item(s) with no SKU "
              f"(set skip_missing_sku=False to include them with a generated placeholder SKU instead).")
    if aggregate_fallback_count:
        print(f"NOTE: {aggregate_fallback_count} order(s) had no line item with a usable SKU — "
              f"added one aggregate line based on the order subtotal instead of dropping the order.")

    # Column order matches the official Magento "Order with Items" template
    ordered_cols = ["Order #", "Order Status", "Order State", "Order Date", "Last Updated", "Store",
                    "Customer Name", "Customer Email", "Customer Group", "Billing Name", "Billing Street",
                    "Billing City", "Billing Region", "Billing Postcode", "Billing Country", "Billing Phone",
                    "Shipping Name", "Shipping Street", "Shipping City", "Shipping Region", "Shipping Postcode",
                    "Shipping Country", "Shipping Method", "Payment Method", "Coupon Code", "Subtotal", "Discount",
                    "Shipping Amount", "Tax Amount", "Grand Total", "Total Invoiced", "Total Refunded", "Currency",
                    "Item SKU", "Item Name", "Item Type", "Qty Ordered", "Qty Invoiced", "Qty Shipped",
                    "Qty Refunded", "Qty Canceled", "Original Price", "Item Price", "Item Discount", "Item Tax",
                    "Row Total", "Row Total (incl. Tax)", "Product ID", "Item Options"]
    result = pd.DataFrame(magento_rows)
    if not result.empty:
        result = result[ordered_cols]
    return result


# ==========================================================================
# MAIN
# ==========================================================================
if __name__ == "__main__":
    print("=" * 70)
    print("WOOCOMMERCE -> MAGENTO CONVERTER — will ask for 3 files one by one:")
    print("  1) products   2) customers/users   3) orders")
    print("(Press Enter to skip any file you don't have)")
    print("=" * 70)

    results = {}

    products_df = upload_one("products")
    if products_df is not None:
        magento_products = convert_products_woocommerce(products_df)
        print(f"\nConverted {len(magento_products)} product rows.")
        print(magento_products.head())
        results["magento_products_import.csv"] = magento_products

    customers_df = upload_one("customers/users")
    if customers_df is not None:
        magento_customers_main = convert_customers_main_woocommerce(customers_df)
        print(f"\nConverted {len(magento_customers_main)} customer (core) rows.")
        print(magento_customers_main.head())
        results["magento_customers_main_import.csv"] = magento_customers_main
 
        magento_customer_addresses = convert_customers_addresses_woocommerce(customers_df)
        print(f"\nConverted {len(magento_customer_addresses)} customer address rows.")
        if len(magento_customer_addresses):
            print(magento_customer_addresses.head())
            results["magento_customer_addresses_import.csv"] = magento_customer_addresses

    orders_df = upload_one("orders")
    if orders_df is not None:
        magento_orders = convert_orders_woocommerce(orders_df, skip_missing_sku=True)
        print(f"\nConverted {len(magento_orders)} order line-item rows.")
        print(magento_orders.head())
        results["magento_orders_import.csv"] = magento_orders

    if not results:
        print("\nNo files were provided — nothing to convert.")
    else:
        output_dir = "magento_output"
        os.makedirs(output_dir, exist_ok=True)
        print("\n" + "=" * 70)
        print(f"Saving converted files to '{output_dir}/' ...")
        for fname, out_df in results.items():
            out_path = os.path.join(output_dir, fname)
            out_df.to_csv(out_path, index=False)
            print(f"  -> {out_path} ({len(out_df)} rows) saved.")
        print("=" * 70)
        print("DONE! Import these files in Magento Admin under:")
        print("  Products/Customers -> System > Data Transfer > Import")
        print("  Orders -> via your chosen third-party order-import extension")
