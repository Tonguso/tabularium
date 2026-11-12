"""Task-specific local QA of a delivered file, run by the review_output tool in a bounded subprocess
(python -m tabularium.records.review_output <request.json>); never overwrites the deliverable. One module of the
package since 2026-11-12: before, each agent folder carried its own copy and an installation without one had no QA."""
import argparse
from contextlib import redirect_stdout
from datetime import datetime
import hashlib
import io
import json
import math
from pathlib import Path
import shutil
import sys
import zipfile
from tabularium.records.action_evidence import verify_artifact
from tabularium.core.agent_runtime import atomic_json

def recalculate_copy(source, directory):
    import pythoncom
    import win32com.client
    target = directory / ("recalculated" + source.suffix)
    shutil.copy2(source,target)
    pythoncom.CoInitialize()
    app = book = None
    try:
        app = win32com.client.DispatchEx("Excel.Application")
        app.Visible = False
        app.DisplayAlerts = False
        app.AskToUpdateLinks = False
        app.AutomationSecurity = 3  # Never execute workbook macros.
        book = app.Workbooks.Open(str(target), UpdateLinks=0, ReadOnly=False, IgnoreReadOnlyRecommended=True)
        app.CalculateFullRebuild()
        book.Save()
        return target
    finally:
        if book is not None:
            book.Close(SaveChanges=False)
        if app is not None:
            app.Quit()
        pythoncom.CoUninitialize()

def review_sheet(path, directory, checks, recalculate):
    from openpyxl import load_workbook
    issues, unchecked = [], []
    with zipfile.ZipFile(path) as z:
        linked = any(n.startswith("xl/externalLinks/") or n == "xl/connections.xml" for n in z.namelist())
    values_path = path
    calculation = "not requested"
    if recalculate and not linked:
        try:
            values_path = recalculate_copy(path,directory)
            calculation = "Recalculated an isolated copy in Excel; original unchanged."
        except Exception as exc:
            calculation = "Unavailable: " + type(exc).__name__
            unchecked.append("Fresh calculation could not be confirmed.")
    elif linked:
        unchecked.append("External links/data connections present; no automatic refresh or recalculation performed.")
    formulas = load_workbook(path,read_only=True,data_only=False)
    values = load_workbook(values_path,read_only=False,data_only=True)
    missing = []
    outcomes = []
    try:
        for sheet in formulas:
            cached = values[sheet.title]
            for row in sheet:
                for cell in row:
                    if getattr(cell, "coordinate", None) is None:
                        continue  # read-only EmptyCell (blank gap in a row): nothing to check
                    ref = f"{sheet.title}!{cell.coordinate}"
                    if cell.data_type == "f":
                        if "#REF!" in str(cell.value):
                            issues.append(f"Broken reference: {ref}")
                        if cached[cell.coordinate].value is None:
                            missing.append(ref)
                    if cached[cell.coordinate].data_type == "e":
                        issues.append(f"Calculation error {ref}: {cached[cell.coordinate].value}")
        for check in checks:
            try:
                sheet = values[check["sheet"]]
                actual = sheet[check["cell"]].value
                if "components" in check:
                    parts = [sheet[c].value for c in check["components"]]
                    if not parts or not all(isinstance(v,(int,float)) and not isinstance(v,bool) for v in parts):
                        raise ValueError("Components are missing or non-numeric.")
                    expected = sum(parts)
                elif "expected" in check:
                    expected = check["expected"]
                else:
                    raise ValueError("Supply expected or components.")
                if isinstance(actual,(int,float)) and isinstance(expected,(int,float)):
                    passed = math.isclose(actual,expected,rel_tol=0,abs_tol=max(0,float(check.get("tolerance",1e-6))))
                else:
                    passed = actual == expected
                if "expected_unit" in check:
                    unit = sheet[check["unit_label_cell"]].value
                    passed = passed and str(unit).strip().casefold() == str(check["expected_unit"]).strip().casefold()
                outcomes.append({"check":check,"actual":actual,"expected":expected,"passed":passed})
                if not passed:
                    issues.append("Reconciliation failed: " + check["sheet"]+"!"+check["cell"])
            except Exception as exc:
                outcomes.append({"check":check,"passed":False,"error":str(exc)})
                issues.append("A requested check could not be completed.")
    finally:
        formulas.close(); values.close()
    if missing:
        unchecked.append(f"{len(missing)} formula cells have no usable cached value.")
    if not checks:
        unchecked.append("No task-specific totals, expected values or unit checks supplied.")
    return {"issues":issues,"unchecked":unchecked,"calculation":calculation,
            "calculated_copy":str(values_path) if values_path != path else None,
            "missing_formula_values":missing,"reconciliations":outcomes,
            "status":"needs_attention" if issues else "checks_incomplete" if unchecked else "specified_checks_passed",
            "limit":"Passing supplied checks does not establish all formulas or assumptions are correct."}

def review_deck(path,directory,render):
    import pythoncom
    import deck
    pythoncom.CoInitialize()
    capture = io.StringIO()
    try:
        with redirect_stdout(capture):
            deck.cmd_check(path)
            if render:
                deck.cmd_render(path,str(directory/"slides"))
    finally:
        pythoncom.CoUninitialize()
    output = capture.getvalue()
    issues = [line.strip() for line in output.splitlines()
              if line.strip().startswith(("OVERFLOW","OFF-SLIDE","PLACEHOLDER"))]
    return {"status":"needs_attention" if issues else "visual_review_required",
            "issues":issues,"check_output":output,
            "rendered_slides":[str(p) for p in sorted((directory/"slides").glob("slide*.png"))],
            "next":"Use view_image to inspect rendered slides for overlap, readability, chart units and labels; inspect every slide before claiming visual review."}

def review_research(claims,date_from="",date_to=""):
    for value in (date_from,date_to):
        if value:
            datetime.strptime(value,"%Y-%m-%d")
    if date_from and date_to and date_from > date_to:
        raise ValueError("Invalid date range.")
    findings = []
    for claim in claims:
        issues = []
        if not claim.get("claim"):
            issues.append("Missing claim.")
        quote = claim.get("evidence_quote","").strip()
        source = Path(claim.get("source_path",""))
        digest = None
        try:
            if source.suffix.lower() not in (".txt",".md",".json",".html"):
                raise ValueError("Provide a retained text source, not a PDF path.")
            text = source.read_text(encoding="utf-8")
            digest = hashlib.sha256(text.encode()).hexdigest()
            if not quote or " ".join(quote.split()) not in " ".join(text.split()):
                issues.append("Supporting quote missing or absent from retained source.")
        except (OSError,ValueError) as exc:
            issues.append("Source unavailable: " + str(exc))
        published = claim.get("published_at","")
        try:
            datetime.strptime(published,"%Y-%m-%d")
            if date_from and published < date_from or date_to and published > date_to:
                issues.append("Source publication date is outside the requested period; distinguish background context from this week's event.")
        except ValueError:
            issues.append("Publication date missing or invalid.")
        if claim.get("kind") not in ("fact","inference"):
            issues.append("Label the claim as fact or inference.")
        if not claim.get("source_url"):
            issues.append("Missing source URL or publication reference.")
        findings.append({"claim":claim,"issues":issues,"source_sha256":digest,
                         "quote_present":not any("quote" in i or "Source unavailable" in i for i in issues)})
    return {"status":"evidence_gaps" if not findings or any(f["issues"] for f in findings) else "human_or_model_reasoning_review_required",
            "claims":findings, "requested_period":{"from":date_from,"to":date_to},
            "limit":"Checks dates/retained quotations only. Does not prove the publisher's date, truth, causation or that an excerpt supports the conclusion. Read surrounding text and competing evidence."}

def review(path="",checks=None,claims=None,recalculate=True,render=True,date_from="",date_to=""):
    root = Path(__file__).resolve().parents[1]
    claims = claims or None   # an empty list is 'no claims', not claims mode (task 82 sent claims=[] with a path ten times)
    if claims is not None and path:
        raise ValueError("Review either a file (path) or research claims (claims) in one call, not both: drop one of the two arguments and call again.")
    if claims is not None:
        result = review_research(claims,date_from,date_to)
        fingerprint = hashlib.sha256(json.dumps(claims,sort_keys=True).encode()).hexdigest()
    else:
        source = Path(path).resolve()
        result = verify_artifact(source)
        fingerprint = result.get("sha256","missing")
        directory = root/"reports"/"quality_checks"/fingerprint
        directory.mkdir(parents=True,exist_ok=True)
        if result["status"] in ("missing","validation_failed"):
            return result
        if source.suffix.lower() == ".xlsx":
            result["quality"] = review_sheet(source,directory,checks or [],recalculate)
        elif source.suffix.lower() == ".pptx":
            result["quality"] = review_deck(source,directory,render)
        else:
            result["quality"] = {"status":"task_specific_review_required"}
        # Detect concurrent edits: the receipt is valid only for the inspected bytes.
        with source.open("rb") as stream:
            if hashlib.file_digest(stream,"sha256").hexdigest() != fingerprint:
                result["quality"]["status"] = "file_changed_during_review"
        result["status"] = result["quality"]["status"]
    result = json.loads(json.dumps(result, ensure_ascii=False, default=str))
    output = root/"reports"/"quality_checks"/fingerprint/"review.json"
    atomic_json(output,result)
    return dict(result, report_path=str(output))

if __name__ == "__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("request")
    args=parser.parse_args()
    try:
        result=review(**json.loads(Path(args.request).read_text(encoding="utf-8")))
        print(json.dumps(result,ensure_ascii=False,default=str))
    except Exception as exc:
        print(json.dumps({"status":"review_failed","error":str(exc)},ensure_ascii=False))
        sys.exit(1)
