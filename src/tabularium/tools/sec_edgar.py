"""SEC EDGAR as a tool for the research agents (2026-10-17).

Primary-source filings for US-listed names, through the edgartools library: recent filings with their URLs, a section
of the latest 10-K or 10-Q (risk factors, business, MD&A, or any Item), the financial statements as parsed from
XBRL, and recent insider transactions (Form 4). No API key; the SEC requires a contact identity on every request
(EDGAR_IDENTITY in .env) and allows ten requests a second, which edgartools keeps to. US filers only: HKEX and
TSE disclosures are not here. Every result names the filing and its URL so the agent can cite the document, not a
summary of it."""
import os

from tabularium.core.outcome import Failed

SEC_FILING_TOOL = {
    "name": "sec_filing",
    "description": ("SEC EDGAR, primary source for US-listed companies. action=filings: recent filings (form, date, URL). "
                    "action=section: one part of the latest 10-K or 10-Q as text (item: risk_factors, business, mdna, or an "
                    "Item number like 'Item 1C'); long sections come back paged. action=financials: income, balance or cash "
                    "statement from XBRL, annual or quarterly. action=insiders: recent Form 4 transactions. Cite the filing URL "
                    "the result gives. US filers only."),
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["filings", "section", "financials", "insiders"]},
            "ticker": {"type": "string", "description": "US ticker, e.g. AMT, or a CIK number."},
            "form": {"type": "string", "description": "filings: comma-separated forms to list (default 10-K,10-Q,8-K). section: 10-K (default) or 10-Q."},
            "item": {"type": "string", "description": "section: risk_factors | business | mdna | 'Item 7' etc. Default risk_factors."},
            "statement": {"type": "string", "enum": ["income", "balance", "cash"], "description": "financials: default income."},
            "period": {"type": "string", "enum": ["annual", "quarterly"], "description": "financials: default annual."},
            "periods": {"type": "integer", "description": "financials: how many periods (default 4). filings/insiders: how many rows (default 10)."},
        },
        "required": ["action", "ticker"],
    },
}

ITEMS = {"10-K": {"risk_factors": "Item 1A", "business": "Item 1", "mdna": "Item 7"},
         "10-Q": {"risk_factors": "Item 1A", "business": "Item 2", "mdna": "Item 2"}}
STATEMENTS = {"income": "income_statement", "balance": "balance_sheet", "cash": "cash_flow_statement"}


def _company(ticker):
    from edgar import Company, set_identity
    identity = os.environ.get("EDGAR_IDENTITY", "").strip()
    if not identity:
        raise RuntimeError("EDGAR_IDENTITY is not set (name and email the SEC asks for on every request).")
    set_identity(identity)
    return Company(str(ticker).strip().upper())


def _cite(company, filing):
    return f"Source: SEC EDGAR, {company.name} (CIK {company.cik}), {filing.form} filed {filing.filing_date}, {filing.url}"


def _table(frame):
    """A statement frame as plain text: the label and the period columns, nothing structural."""
    keep = [c for c in frame.columns if c not in ("depth", "is_abstract", "is_total", "section", "confidence")]
    out = frame[keep]
    if "label" in out.columns:
        out = out.set_index("label")
    return out.to_string(max_colwidth=48, float_format=_money)


def _money(value):
    """Compact dollars: 715,274,000.0 -> $715.3M. Strings from the library (already '$10.6B') pass through."""
    try:
        value = float(value)
    except (TypeError, ValueError):
        return str(value)
    sign = "-" if value < 0 else ""
    value = abs(value)
    for unit, size in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if value >= size:
            return f"{sign}${value / size:,.1f}{unit}"
    return f"{sign}${value:,.0f}"


def sec_filing(action, ticker, form="", item="", statement="income", period="annual", periods=0):
    try:
        company = _company(ticker)
        if action == "filings":
            forms = [f.strip() for f in (form or "10-K,10-Q,8-K").split(",") if f.strip()]
            rows = company.get_filings(form=forms).head(int(periods) or 10)
            lines = [f"{company.name} (CIK {company.cik}), latest {', '.join(forms)} filings:"]
            lines += [f"- {f.form}  {f.filing_date}  {f.accession_no}  {f.url}" for f in rows]
            return "\n".join(lines) if len(lines) > 1 else f"{company.name}: no {', '.join(forms)} filings found."
        if action == "section":
            kind = (form or "10-K").upper()
            if kind not in ITEMS:
                return Failed(f"sec_filing: section needs form 10-K or 10-Q, not {kind}.")
            filing = company.latest(kind)
            if filing is None:
                return Failed(f"sec_filing: {company.name} has no {kind} on EDGAR.")
            key = ITEMS[kind].get((item or "risk_factors").lower(), item)
            text = filing.obj()[key]
            if not text:
                return Failed(f"sec_filing: {key} not found in that {kind}.")
            return f"{_cite(company, filing)}\n{key} ({len(text):,} characters)\n\n{text}"
        if action == "financials":
            method = getattr(company, STATEMENTS.get(statement or "income", ""), None)
            if method is None:
                return Failed(f"sec_filing: statement must be income, balance or cash, not {statement}.")
            kwargs = {"periods": int(periods) or 4}
            if period == "quarterly":
                kwargs["period"] = "quarterly"
            frame = method(**kwargs).to_dataframe()
            return (f"Source: SEC EDGAR XBRL, {company.name} (CIK {company.cik}), {statement} statement, "
                    f"{period or 'annual'}. Values are the library's reading of the XBRL; a figure that looks out of line "
                    f"with its neighbours (a fourth quarter derived from the annual, a restated period) is checked against "
                    f"the filing text before it is quoted.\n{_table(frame)}")
        if action == "insiders":
            rows = company.get_filings(form="4").head(int(periods) or 10)
            lines = [f"{company.name} (CIK {company.cik}), recent Form 4 insider transactions:"]
            for f in rows:
                try:
                    frame = f.obj().to_dataframe()
                except Exception as exc:
                    lines.append(f"- {f.filing_date}: unreadable Form 4 ({type(exc).__name__}) {f.url}")
                    continue
                cols = [c for c in ("Date", "Insider", "Position", "Transaction Type", "Code", "Shares", "Price", "Value", "Remaining Shares") if c in frame.columns]
                lines.append(f"- filed {f.filing_date}, {f.url}\n" + frame[cols].to_string(index=False))
            return "\n".join(lines)
        return Failed(f"sec_filing: unknown action {action}.")
    except Exception as exc:
        return Failed(f"sec_filing error: {type(exc).__name__}: {str(exc)[:300]}")
