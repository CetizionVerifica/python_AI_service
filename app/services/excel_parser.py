
import io
import logging
import requests
import pandas as pd
from datetime import datetime, date

from app.core.database import get_connection, get_uploaded_document_by_id, fetch_emission_factor, bulk_insert_emissions_with_conn

logger = logging.getLogger(__name__)

UNIT_CONVERSIONS = {
    "litre": {"gallon": 0.264172, "ml": 1000, "cubic meter": 0.001, "kilo litre": 0.001, "kl": 0.001},
    "kl": {"litre": 1000, "gallon": 264.172, "ml": 1000000, "cubic meter": 1},
    "kilo litre": {"litre": 1000, "gallon": 264.172, "ml": 1000000, "cubic meter": 1},
    "gallon": {"litre": 3.78541, "ml": 3785.41, "cubic meter": 0.00378541, "kl": 0.00378541},
    "ml": {"litre": 0.001, "gallon": 0.000264172, "kl": 0.000001},
    "cubic meter": {"litre": 1000, "gallon": 264.172, "kl": 1},

    # Weight
    "kg": {"lb": 2.20462, "tonne": 0.001, "g": 1000, "ton": 0.00110231},
    "lb": {"kg": 0.453592, "tonne": 0.000453592, "g": 453.592},
    "ton": {"kg": 907.185, "lb": 2000, "g": 907185},
    "tonne": {"kg": 1000, "lb": 2204.62, "g": 1000000},
    "g": {"kg": 0.001, "lb": 0.00220462},

    # Energy
    "kwh": {"mwh": 0.001, "gj": 0.0036, "mj": 3.6},
    "mwh": {"kwh": 1000, "gj": 3.6, "mj": 3600},
    "gj": {"kwh": 277.778, "mwh": 0.277778, "mj": 1000},
    "mj": {"kwh": 0.277778, "gj": 0.001},

    # Currency
    "inr": {"usd": 0.012},
    "usd": {"inr": 83.5, "eur": 0.92},
    "eur": {"usd": 1.09},
}

def get_conversion_factor(from_unit: str, to_unit: str):
    f = (from_unit or "").lower().strip()
    t = (to_unit or "").lower().strip()
    return UNIT_CONVERSIONS.get(f, {}).get(t)

def units_match_exact(u1: str | None, u2: str | None) -> bool:
    if not u1 or not u2:
        return True
    return u1.lower().strip() == u2.lower().strip()

# ---------- Excel reading ----------
def _read_df_from_bytes(content: bytes, ext: str) -> pd.DataFrame:
    for header_row in [0, 1, 2]:
        try:
            if ext == "csv":
                df = pd.read_csv(io.BytesIO(content), dtype=str, header=header_row)
            else:
                df = pd.read_excel(io.BytesIO(content), dtype=str, header=header_row)

            valid_cols = [c for c in df.columns if not str(c).startswith("Unnamed:")]
            if valid_cols:
                df = df.fillna("")
                return df
        except Exception:
            continue
    raise ValueError("Failed to parse file: no valid headers found.")

def map_df(df: pd.DataFrame, mappings: dict[str, str]) -> pd.DataFrame:
    mapped = {}
    for required_field, excel_col in mappings.items():
        col = str(excel_col).strip()
        if col in df.columns:
            mapped[required_field] = df[col]
        else:
            logger.warning(f"Column '{col}' not found in file, skipping.")
    if not mapped:
        raise ValueError("No valid column mappings found.")
    return pd.DataFrame(mapped).fillna("")

def download_document_bytes(document_id: int) -> tuple[bytes, str]:
    doc = get_uploaded_document_by_id(document_id)
    if not doc:
        raise ValueError("Invalid document_id")

    url = doc["cloudinary_url"]
    try:
        r = requests.get(url, timeout=60)
        r.raise_for_status()
    except Exception as e:
        raise ValueError(f"Failed to download file from Cloudinary: {e}")

    # determine extension
    filename = url.split("?")[0].lower()
    ext = filename.rsplit(".", 1)[-1].lower()
    if ext not in {"csv", "xls", "xlsx"}:
        ext = "xlsx"

    return r.content, ext

# ---------- Step 2: unique categories ----------
def get_unique_categories(document_id: int, mappings: dict[str, str]) -> tuple[list[str], int]:
    content, ext = download_document_bytes(document_id)
    df = _read_df_from_bytes(content, ext)
    mapped_df = map_df(df, mappings)

    if "emission_category" not in mapped_df.columns:
        raise ValueError("Mapping must include emission_category")

    cats = sorted(
        {str(x).strip() for x in mapped_df["emission_category"].tolist() if str(x).strip()}
    )
    return cats, len(mapped_df)


def get_preview_rows(
    document_id: int,
    mappings: dict[str, str],
    selected_categories: list[str],
    page: int,
    page_size: int,
    site_id: int,
    category_id: int,
    date_of_reporting: str,
) -> tuple[list[dict], int]:
    content, ext = download_document_bytes(document_id)
    df = _read_df_from_bytes(content, ext)
    mapped_df = map_df(df, mappings)

    total = len(mapped_df)

    if selected_categories:
        selected = {c.strip() for c in selected_categories}
        if "emission_category" in mapped_df.columns:
            mapped_df = mapped_df[
                mapped_df["emission_category"].astype(str).str.strip().isin(selected)
            ]

    start = max(0, (page - 1) * page_size)
    end = start + page_size
    page_df = mapped_df.iloc[start:end]

    # ✅ compute EF + emission for ONLY these preview rows
    conn = get_connection()
    try:
        out: list[dict] = []
        year = int(date_of_reporting[:4]) - 1

        for _, r in page_df.iterrows():
            activity_data = r.to_dict()
            activity_unit = str(activity_data.get("activity_data_unit") or "").strip() or None
            emission_category = str(activity_data.get("emission_category") or "").strip()

            ef = None
            if emission_category:
                ef = fetch_emission_factor(conn, site_id, category_id, year, emission_category)

            factor_value = float(ef["factor_value"]) if ef and ef.get("factor_value") is not None else None
            denominator_unit = ef.get("denominator_unit") if ef else None

            total_emission = _calc_emission(
                conn=conn,
                site_id=site_id,
                category_id=category_id,
                activity_data=activity_data,
                activity_unit=activity_unit,
                date_of_reporting=date_of_reporting,
            )

            row_out = dict(activity_data)
            row_out["factor_value"] = factor_value
            row_out["denominator_unit"] = denominator_unit
            row_out["total_emission"] = total_emission
            row_out["unit"] = "tCO2e"

            out.append(row_out)

        return out, total
    finally:
        conn.close()

def _extract_activity_value(activity_data: dict) -> float:
    """
    Extract a numeric activity value from a mapped row dict.
    Mirrors your Node/JS logic:
      1) try common field names
      2) else first numeric >= 100
      3) else first numeric > 0
    """
    skip_cols = {
        "material",
        "disposal_method",
        "fuel_type",
        "vehicle_type",
        "source_type",
        "waste_type",
        "transport_mode",
    }

    common_fields = [
        "activity_value",
        "quantity",
        "value",
        "amount",
        "consumption",
        "activity data",
        "activity_data",
    ]

    keys = list(activity_data.keys())

    # 1) exact common fields first
    for field in common_fields:
        match = next((k for k in keys if str(k).lower().strip() == field.lower().strip()), None)
        if match:
            v = activity_data.get(match)
            if v not in (None, ""):
                try:
                    num = float(str(v).replace(",", "").strip())
                    if num > 0:
                        return num
                except Exception:
                    pass

    # 2) numeric >= 100
    for k, v in activity_data.items():
        if str(k).lower().strip() in skip_cols:
            continue
        if str(k).lower().strip() == "emission_category":
            continue
        if v in (None, ""):
            continue
        try:
            num = float(str(v).replace(",", "").strip())
            if num >= 100:
                return num
        except Exception:
            continue

    # 3) any positive numeric
    for k, v in activity_data.items():
        if str(k).lower().strip() in skip_cols:
            continue
        if str(k).lower().strip() == "emission_category":
            continue
        if v in (None, ""):
            continue
        try:
            num = float(str(v).replace(",", "").strip())
            if num > 0:
                return num
        except Exception:
            continue

    return 0.0


def _calc_emission(conn, site_id: int, category_id: int, activity_data: dict, activity_unit: str | None, date_of_reporting: str) -> float:
    emission_category = (activity_data.get("emission_category") or "").strip()
    if not emission_category:
        return 0.0

    # reportingYear - 1 (same as Node)
    year = int(date_of_reporting[:4]) - 1

    ef = fetch_emission_factor(conn, site_id, category_id, year, emission_category)
    if not ef:
        # choose: strict vs lenient
        # lenient => 0
        return 0.0

    factor_value = float(ef["factor_value"])
    denom_unit = ef.get("denominator_unit")

    activity_value = _extract_activity_value(activity_data)
    if activity_value <= 0:
        return 0.0

    if units_match_exact(denom_unit, activity_unit):
        return round((activity_value * factor_value) / 1000.0, 2)

    conv = get_conversion_factor(activity_unit or "", denom_unit or "")
    if not conv:
        # lenient => 0
        return 0.0

    converted = activity_value * float(conv)
    return round((converted * factor_value) / 1000.0, 2)


def import_all_rows(
    document_id: int,
    mappings: dict[str, str],
    selected_categories: list[str],
    site_id: int,
    category_id: int,
    date_of_reporting: str,
    chunk_size: int = 2000,
) -> dict:
    content, ext = download_document_bytes(document_id)
    df = _read_df_from_bytes(content, ext)
    mapped_df = map_df(df, mappings)

    total_rows = len(mapped_df)

    if selected_categories:
        selected = {c.strip() for c in selected_categories}
        if "emission_category" in mapped_df.columns:
            mapped_df = mapped_df[
                mapped_df["emission_category"].astype(str).str.strip().isin(selected)
            ]

    conn = get_connection()
    inserted = 0
    skipped = 0

    # ✅ SPEED: preload emission factors once into a dict (huge speedup)
    year = int(date_of_reporting[:4]) - 1
    ef_map: dict[str, tuple[float, str | None]] = {}
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT emission_category_name, factor_value, denominator_unit
                FROM emission_factors
                WHERE site_id = %s
                  AND category_id = %s
                  AND year = %s
                """,
                (site_id, category_id, year),
            )
            for row in cur.fetchall():
                name = (row["emission_category_name"] or "").strip().lower()
                if not name:
                    continue
                ef_map[name] = (float(row["factor_value"]), row.get("denominator_unit"))
    except Exception:
        # if preload fails, continue (fallback to per-row fetch in _calc_emission)
        ef_map = {}

    try:
        rows_buffer: list[dict] = []

        for _, row in mapped_df.iterrows():
            activity_data = row.to_dict()
            activity_unit = str(activity_data.get("activity_data_unit") or "").strip() or None
            emission_category = str(activity_data.get("emission_category") or "").strip()

            # ✅ Use preloaded EF if available (avoid DB query per row)
            if ef_map and emission_category:
                key = emission_category.strip().lower()
                if key in ef_map:
                    factor_value, denom_unit = ef_map[key]
                    # do calc inline (fast)
                    activity_value = _extract_activity_value(activity_data)
                    if activity_value <= 0:
                        total_emission = 0.0
                    elif units_match_exact(denom_unit, activity_unit):
                        total_emission = round((activity_value * factor_value) / 1000.0, 2)
                    else:
                        conv = get_conversion_factor(activity_unit or "", denom_unit or "")
                        if not conv:
                            total_emission = 0.0
                        else:
                            converted = activity_value * float(conv)
                            total_emission = round((converted * factor_value) / 1000.0, 2)
                else:
                    # no factor found
                    total_emission = 0.0
            else:
                total_emission = _calc_emission(conn, site_id, category_id, activity_data, activity_unit, date_of_reporting)

            rows_buffer.append(
                {
                    "site_id": site_id,
                    "category_id": category_id,
                    "activity_data": activity_data,  
                    "total_emission": total_emission,
                    "unit": "tCO2e",
                    "date_of_reporting": date_of_reporting,
                    "activity_data_unit": activity_unit,
                }
            )

            if len(rows_buffer) >= chunk_size:
                inserted += bulk_insert_emissions_with_conn(conn, rows_buffer)
                rows_buffer = []

        if rows_buffer:
            inserted += bulk_insert_emissions_with_conn(conn, rows_buffer)

        conn.commit()
        return {"inserted": inserted, "skipped": skipped, "total_rows": total_rows}

    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()