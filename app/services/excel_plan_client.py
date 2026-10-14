"""Bulk upload on the plans API (IM11, docs/mcp-import/later/IM11-bulk-upload-on-plans).

The in-app Bulk upload page (P27) sends a sheet to this service, which keeps
doing what it is good at: storing the file, detecting the header row and
mapping columns. The calculation is no longer done here. The mapped rows are
turned into ``ImportRow``s and handed to ESG-lite's plans API
(``POST /imports/plans`` and friends, ``01-contracts.md`` §4) with the caller's
own sign-in token and ``channel: "bulk_upload"``. ESG-lite's emission write
service then gives every sheet row the factor, unit conversion, FERA twin,
mode-lock and duplicate checks of a typed entry, and the same total, so there
is one engine for typed entries, Claude imports and bulk uploads.

This service never writes emission rows: the plan is committed by ESG-lite,
which stores the batch and notifies the site's managers.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

import httpx
import pandas as pd

from app.core.config import settings

logger = logging.getLogger(__name__)

# ESG-lite's own limits (src/imports/limits.ts); a request over them is refused there.
ROWS_PER_REQUEST = 2000
ROWS_PER_PLAN = 20000
PAGE_SIZE_MAX = 200
# How many skipped rows an import answer lists (same cap as before IM11).
SKIPPED_ROWS_LIMIT = 1000

CHANNEL = "bulk_upload"

# Mapping keys with a fixed meaning; everything else mapped is a form field.
KEY_CATEGORY = "emission_category"
KEY_VALUE = "activity_value"
KEY_UNIT = "activity_data_unit"
KEY_DATE = "date_of_reporting"
EXTRA_PREFIX = "extra_"

ROW_REF_PATTERN = re.compile(r"^Row (\d+)$")


class EsgLiteError(Exception):
    """An answer from ESG-lite that is not a success, or no answer at all.

    ``status`` is the HTTP status to answer the browser with: ESG-lite's own
    status for an error body it produced (``{code, message, details}``), 502
    when it could not be reached. ``code`` is the IM0 error code.
    """

    def __init__(self, status: int, code: str, message: str, details: Any = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details

    def body(self) -> dict:
        out = {"code": self.code, "message": self.message}
        if self.details is not None:
            out["details"] = self.details
        return out


class PlanClient:
    """The caller's view of ESG-lite's plans API, with their own token forwarded."""

    def __init__(self, token: str, base_url: Optional[str] = None, transport: Optional[httpx.BaseTransport] = None, timeout: float = 120.0):
        if not token:
            raise ValueError("A sign-in token is required to talk to ESG-lite.")
        self._client = httpx.Client(
            base_url=(base_url or settings.ESG_LITE_API_URL).rstrip("/"),
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            timeout=timeout,
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "PlanClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _call(self, method: str, path: str, json: Any = None, params: Optional[dict] = None) -> Any:
        try:
            res = self._client.request(method, path, json=json, params=params)
        except httpx.HTTPError as e:
            logger.error(f"ESG-lite unreachable for {method} {path}: {type(e).__name__}")
            raise EsgLiteError(502, "ESG_LITE_UNREACHABLE", "The ESG-lite API could not be reached. Try again in a moment.")
        if 200 <= res.status_code < 300:
            try:
                return res.json()
            except ValueError:
                raise EsgLiteError(502, "ESG_LITE_BAD_ANSWER", "The ESG-lite API gave an answer this service could not read.")
        try:
            body = res.json()
        except ValueError:
            body = {}
        if not isinstance(body, dict):
            body = {}
        code = str(body.get("code") or ("UNAUTHORIZED" if res.status_code == 401 else "ESG_LITE_ERROR"))
        message = str(body.get("message") or body.get("error") or "The ESG-lite API refused the request.")
        # Only the status, code and message travel on; the body is logged by id, never by row.
        logger.warning(f"ESG-lite answered {res.status_code} {code} for {method} {path}")
        raise EsgLiteError(res.status_code, code, message, body.get("details"))

    # -- plans ---------------------------------------------------------------

    def create_plan(self, source_name: str, rows: list[dict], company_id: Optional[int] = None) -> dict:
        body: dict[str, Any] = {"source_name": source_name, "channel": CHANNEL, "rows": rows}
        if company_id is not None:
            body["company_id"] = company_id
        return self._call("POST", "/imports/plans", json=body)

    def append_rows(self, plan_id: str, version: int, rows: list[dict]) -> dict:
        return self._call("POST", f"/imports/plans/{plan_id}/rows", json={"version": version, "rows": rows})

    def get_plan(self, plan_id: str, page: int = 1, page_size: int = 50, state: Optional[str] = None, code: Optional[str] = None) -> dict:
        params: dict[str, Any] = {"page": page, "page_size": min(max(int(page_size), 1), PAGE_SIZE_MAX)}
        if state:
            params["state"] = state
        if code:
            params["code"] = code
        return self._call("GET", f"/imports/plans/{plan_id}", params=params)

    def commit_plan(self, plan_id: str, version: int, idempotency_key: str, skip_rows_with_issues: bool = True) -> dict:
        return self._call(
            "POST",
            f"/imports/plans/{plan_id}/commit",
            json={"version": version, "skip_rows_with_issues": skip_rows_with_issues, "idempotency_key": idempotency_key},
        )

    def cancel_plan(self, plan_id: str) -> dict:
        return self._call("POST", f"/imports/plans/{plan_id}/cancel")

    def issue_rows(self, plan_id: str, limit: int = SKIPPED_ROWS_LIMIT) -> list[dict]:
        """Every row of the plan in state ``issue``, up to ``limit``."""
        out: list[dict] = []
        page = 1
        while len(out) < limit:
            res = self.get_plan(plan_id, page=page, page_size=PAGE_SIZE_MAX, state="issue")
            rows = res.get("rows") or []
            out.extend(rows)
            if len(rows) < PAGE_SIZE_MAX or page * PAGE_SIZE_MAX >= int(res.get("total_rows") or 0):
                break
            page += 1
        return out[:limit]


# ---------------------------------------------------------------------------
# Sheet rows → ImportRows
# ---------------------------------------------------------------------------


@dataclass
class LocalIssue:
    """A row this service could not send: the plans API would refuse the whole request for it."""

    row: int
    row_ref: str
    emission_category: Optional[str]
    code: str
    reason: str
    input: dict = field(default_factory=dict)

    def as_planned_row(self) -> dict:
        """The same shape as a PlannedRow in state ``issue``, so the preview lists it with the rest."""
        return {
            "row_ref": self.row_ref,
            "state": "issue",
            "issues": [{"code": self.code, "severity": "error", "message": self.reason}],
            "input": self.input,
            "local": True,
        }


@dataclass
class SheetRows:
    rows: list[dict]
    local_issues: list[LocalIssue]
    total_rows: int
    not_selected: int


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and pd.isna(value):
        return ""
    return str(value).strip()


def _number(value: Any) -> Optional[float]:
    """A cell as a number, or None when it is blank or not numeric ("1,200" counts)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        if isinstance(value, float) and pd.isna(value):
            return None
        return float(value)
    text = _text(value).replace(",", "")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def row_ref_for(index: Any) -> str:
    """``Row N`` where 1 is the first data row under the header, as the skipped-rows file numbered them."""
    return f"Row {int(index) + 1}"


def row_number(row_ref: Optional[str]) -> Optional[int]:
    m = ROW_REF_PATTERN.match(str(row_ref or ""))
    return int(m.group(1)) if m else None


def import_row_from_record(index: Any, record: dict, site_id: int, category_id: int, fallback_date: str) -> tuple[Optional[dict], Optional[LocalIssue]]:
    """One mapped sheet row as an ImportRow (contracts §1), or the reason it can't be one.

    Mapped keys: ``emission_category`` → ``activity_name``; ``activity_value`` →
    ``activity_value`` (a number); ``activity_data_unit`` → ``activity_unit``;
    ``date_of_reporting`` → a monthly period on that day (else the upload's
    month end); every other key is a form field, ``extra_<key>`` under ``key``.
    Field values travel as the sheet's text: ESG-lite parses numbers itself,
    commas included, the way it does for a typed entry.
    """
    from app.services.excel_parser import _row_reporting_date

    row_ref = row_ref_for(index)
    category = _text(record.get(KEY_CATEGORY))
    unit = _text(record.get(KEY_UNIT))
    row_date = _row_reporting_date(record, fallback_date)

    fields: dict[str, Any] = {}
    for key, value in record.items():
        if key in (KEY_CATEGORY, KEY_VALUE, KEY_UNIT, KEY_DATE):
            continue
        text = _text(value)
        if not text:
            continue
        name = key[len(EXTRA_PREFIX):] if str(key).startswith(EXTRA_PREFIX) else str(key)
        fields[name] = text

    activity_value: Optional[float] = None
    if KEY_VALUE in record:
        raw = record.get(KEY_VALUE)
        if _text(raw):
            activity_value = _number(raw)
            if activity_value is None:
                return None, LocalIssue(row_number(row_ref) or 0, row_ref, category or None, "VALUE_NOT_NUMERIC", f"The activity value {_text(raw)!r} is not a number.", record)

    if not category:
        return None, LocalIssue(row_number(row_ref) or 0, row_ref, None, "MISSING_CATEGORY", "Missing emission category.", record)
    if not unit:
        return None, LocalIssue(row_number(row_ref) or 0, row_ref, category, "MISSING_UNIT", "Missing activity unit.", record)
    if activity_value is None and not fields:
        return None, LocalIssue(row_number(row_ref) or 0, row_ref, category, "MISSING_VALUE", "No activity value or form fields in this row.", record)

    row: dict[str, Any] = {
        "row_ref": row_ref,
        "site_id": int(site_id),
        "category_id": int(category_id),
        "period": {"kind": "monthly", "date": row_date},
        "activity_name": category,
        "activity_unit": unit,
    }
    if activity_value is not None:
        row["activity_value"] = activity_value
    if fields:
        row["fields"] = fields
    return row, None


def rows_from_frame(mapped_df: pd.DataFrame, selected_categories: Iterable[str], site_id: int, category_id: int, fallback_date: str) -> SheetRows:
    """The sheet's mapped rows as ImportRows, keeping the ticked categories only."""
    total_rows = len(mapped_df)
    selected = {str(c).strip() for c in (selected_categories or []) if str(c).strip()}
    if selected and KEY_CATEGORY in mapped_df.columns:
        mapped_df = mapped_df[mapped_df[KEY_CATEGORY].astype(str).str.strip().isin(selected)]
    not_selected = total_rows - len(mapped_df)
    if len(mapped_df) > ROWS_PER_PLAN:
        raise ValueError(f"The sheet has {len(mapped_df)} rows to import; one upload takes at most {ROWS_PER_PLAN}. Split the file.")

    rows: list[dict] = []
    local: list[LocalIssue] = []
    for index, record in zip(mapped_df.index, mapped_df.to_dict("records")):
        row, issue = import_row_from_record(index, record, site_id, category_id, fallback_date)
        if row is not None:
            rows.append(row)
        elif issue is not None:
            local.append(issue)
    return SheetRows(rows=rows, local_issues=local, total_rows=total_rows, not_selected=not_selected)


# ---------------------------------------------------------------------------
# Plans
# ---------------------------------------------------------------------------


def _chunks(rows: list[dict], size: int = ROWS_PER_REQUEST) -> Iterable[list[dict]]:
    for i in range(0, len(rows), size):
        yield rows[i : i + size]


def create_plan_from_rows(client: PlanClient, source_name: str, rows: list[dict], company_id: Optional[int]) -> dict:
    """``POST /imports/plans`` with the first 2,000 rows, then ``/rows`` for the rest, following the version."""
    if not rows:
        raise ValueError("No rows to import. Check the category and unit columns and the ticked categories.")
    chunks = list(_chunks(rows))
    plan = client.create_plan(source_name, chunks[0], company_id)
    for chunk in chunks[1:]:
        plan = client.append_rows(plan["plan_id"], int(plan["version"]), chunk)
    return plan


def plan_summary(plan: dict) -> dict:
    """The plan fields the upload screen shows (no rows)."""
    return {k: plan.get(k) for k in ("plan_id", "version", "status", "counts", "totals", "issues_summary", "expires_at", "source_name", "channel", "committed") if k in plan}


def preview_row(planned: dict) -> dict:
    """A PlannedRow as the Preview table reads it: the sheet's cells plus the result."""
    inp = planned.get("input") or {}
    result = planned.get("result") or {}
    issues = planned.get("issues") or []
    errors = [i for i in issues if i.get("severity") == "error"]
    warnings = [i for i in issues if i.get("severity") != "error"]
    fields = inp.get("fields") or {}
    out: dict[str, Any] = {**fields}
    out.update(
        {
            "row_ref": planned.get("row_ref"),
            "row": row_number(planned.get("row_ref")),
            "state": planned.get("state"),
            "issues": issues,
            "emission_category": inp.get("activity_name") or (inp.get("emission_category") if isinstance(inp, dict) else None),
            "activity_value": inp.get("activity_value"),
            "activity_data_unit": inp.get("activity_unit") or inp.get("activity_data_unit"),
            "global_category_name": (result.get("activity") or {}).get("factor_name"),
            "factor_value": (result.get("factor") or {}).get("value"),
            "factor_year": (result.get("factor") or {}).get("year"),
            "denominator_unit": (result.get("factor") or {}).get("unit"),
            "total_emission": result.get("total_tco2e"),
            "fera_total_emission": (result.get("fera") or {}).get("total_tco2e"),
            "unit": "tCO2e",
            "date_of_reporting": result.get("reporting_date") or (inp.get("period") or {}).get("date"),
        }
    )
    if errors:
        out["row_error"] = "; ".join(str(i.get("message") or i.get("code")) for i in errors)
    if warnings:
        out["row_warning"] = "; ".join(str(i.get("message") or i.get("code")) for i in warnings)
    return out


def _with_local_counts(summary: dict, local: list[LocalIssue]) -> dict:
    """Rows this service refused count as issues on the summary, as the plans API would count them."""
    if not local:
        return summary
    counts = dict(summary.get("counts") or {})
    counts["rows"] = int(counts.get("rows") or 0) + len(local)
    counts["issue"] = int(counts.get("issue") or 0) + len(local)
    summary = {**summary, "counts": counts}
    by_code: dict[str, list[str]] = {}
    for item in local:
        by_code.setdefault(item.code, []).append(item.row_ref)
    extra = [{"code": code, "count": len(refs), "example_row_refs": refs[:5], "local": True} for code, refs in by_code.items()]
    summary["issues_summary"] = list(summary.get("issues_summary") or []) + extra
    return summary


def plan_sheet(
    client: PlanClient,
    *,
    sheet: SheetRows,
    source_name: str,
    company_id: Optional[int],
    page: int = 1,
    page_size: int = 100,
    state: Optional[str] = None,
    cancel_plan_id: Optional[str] = None,
) -> dict:
    """Plan the sheet's rows and answer with the plan and a page of its rows.

    ``cancel_plan_id`` is the plan of an earlier preview of the same upload;
    it is cancelled first so re-mapping a sheet doesn't pile up draft plans
    (ESG-lite allows ten per user).
    """
    if cancel_plan_id:
        try:
            client.cancel_plan(cancel_plan_id)
        except EsgLiteError as e:
            # Already committed, expired or someone else's: nothing to free.
            logger.info(f"Earlier plan not cancelled ({e.code}); carrying on")
    if not sheet.rows:
        if sheet.local_issues:
            rows = [preview_row(i.as_planned_row()) for i in sheet.local_issues]
            summary = _with_local_counts({"plan_id": None, "version": 0, "status": "draft", "counts": {"rows": 0, "ready": 0, "issue": 0, "skipped": 0, "warnings": 0}, "totals": {"tco2e": 0, "by_category": [], "by_month": []}, "issues_summary": []}, sheet.local_issues)
            return {"plan": summary, "rows": rows, "total_rows": len(rows), "page": 1, "page_size": page_size, "not_selected": sheet.not_selected, "sheet_rows": sheet.total_rows}
        raise ValueError("No rows to import. Check the category and unit columns and the ticked categories.")

    plan = create_plan_from_rows(client, source_name, sheet.rows, company_id)
    return plan_page(client, plan["plan_id"], page=page, page_size=page_size, state=state, local_issues=sheet.local_issues, not_selected=sheet.not_selected, sheet_rows=sheet.total_rows)


def plan_page(
    client: PlanClient,
    plan_id: str,
    *,
    page: int = 1,
    page_size: int = 100,
    state: Optional[str] = None,
    local_issues: Optional[list[LocalIssue]] = None,
    not_selected: int = 0,
    sheet_rows: Optional[int] = None,
) -> dict:
    """One page of an existing plan, in the shape ``/v1/excel/preview`` answers."""
    res = client.get_plan(plan_id, page=page, page_size=page_size, state=state)
    rows = [preview_row(r) for r in (res.get("rows") or [])]
    local = local_issues or []
    if local and page == 1 and state in (None, "", "issue"):
        rows = [preview_row(i.as_planned_row()) for i in local] + rows
    summary = _with_local_counts(plan_summary(res), local)
    return {
        "plan": summary,
        "rows": rows,
        "total_rows": int(res.get("total_rows") or 0) + (len(local) if state in (None, "", "issue") else 0),
        "page": int(res.get("page") or page),
        "page_size": int(res.get("page_size") or page_size),
        "not_selected": not_selected,
        "sheet_rows": sheet_rows,
    }


def check_plan_is_this_sheet(client: PlanClient, plan_id: str, *, source_name: str, row_count: int) -> dict:
    """Refuse a ``plan_id`` that was not planned from this upload.

    The commit consumes the document and reports its sheet's counts, so the plan
    must be the one ``/preview`` made from it: a ``bulk_upload`` plan named after
    this file, holding as many rows as the sheet sends. A Claude (``mcp``) draft
    or another file's plan answers 422 before anything is committed.
    """
    plan = client.get_plan(plan_id, page=1, page_size=1)
    counts = plan.get("counts") or {}
    same = (
        plan.get("channel") == CHANNEL
        and str(plan.get("source_name") or "").strip() == source_name.strip()
        and int(counts.get("rows") or 0) == int(row_count)
    )
    if not same:
        logger.warning(f"import refused: plan {plan_id} was not previewed from this file")
        raise ValueError("This plan was not made from this file. Go back to the preview and import again.")
    return plan


def idempotency_key(document_id: int, plan_id: str) -> str:
    """One key per file and plan, so a retried commit returns the stored batch instead of a second one."""
    return f"bulk-upload:doc-{int(document_id)}:plan-{plan_id}"


def commit_plan(client: PlanClient, *, document_id: int, plan_id: str, version: int, local_issues: Optional[list[LocalIssue]] = None, not_selected: int = 0, total_rows: Optional[int] = None) -> dict:
    """Commit the plan's ready rows as one pending batch and answer as ``/v1/excel/import`` always has."""
    plan = client.commit_plan(plan_id, int(version), idempotency_key(document_id, plan_id), skip_rows_with_issues=True)
    committed = plan.get("committed") or {}
    counts = plan.get("counts") or {}
    local = local_issues or []

    skipped_rows: list[dict] = [{"row": i.row, "emission_category": i.emission_category, "reason": i.reason} for i in local]
    issue_count = int(counts.get("issue") or 0) + int(counts.get("skipped") or 0)
    if issue_count and len(skipped_rows) < SKIPPED_ROWS_LIMIT:
        for r in client.issue_rows(plan_id, SKIPPED_ROWS_LIMIT - len(skipped_rows)):
            inp = r.get("input") or {}
            errors = [i for i in (r.get("issues") or []) if i.get("severity") == "error"] or (r.get("issues") or [])
            skipped_rows.append(
                {
                    "row": row_number(r.get("row_ref")),
                    "emission_category": inp.get("activity_name"),
                    "reason": "; ".join(str(i.get("message") or i.get("code")) for i in errors) or "Skipped",
                }
            )
    skipped_rows.sort(key=lambda r: (r["row"] is None, r["row"] or 0))

    inserted = int(committed.get("inserted") or 0)
    skipped = issue_count + len(local)
    return {
        "inserted": inserted,
        "fera_inserted": int(committed.get("fera_inserted") or 0),
        "skipped": skipped,
        "skipped_rows": skipped_rows[:SKIPPED_ROWS_LIMIT],
        "not_selected": not_selected,
        "total_rows": total_rows if total_rows is not None else inserted + skipped + not_selected,
        "upload_batch_id": committed.get("upload_batch_id") if inserted > 0 else None,
        "plan_id": plan_id,
        "plan": plan_summary(plan),
    }
