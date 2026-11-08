"""Local evidence receipts. Submitted is not delivery; readable is not correct."""
import hashlib
import json
from pathlib import Path
import time
import uuid
import zipfile
from tabularium.core.agent_runtime import atomic_json
from tabularium.memory.memory_store import memory_lock

def verify_artifact(path):
    p = Path(path).expanduser().resolve()
    if not p.is_file():
        return {"status": "missing", "path": str(p)}
    with p.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    result = {"status": "saved", "path": str(p), "bytes": p.stat().st_size, "sha256": digest}
    try:
        if p.suffix.lower() in (".xlsx", ".pptx", ".docx", ".zip"):
            with zipfile.ZipFile(p) as z:
                bad = z.testzip()
                if bad:
                    raise ValueError("Corrupt archive member: " + bad)
            result["container_check"] = "passed"
        if p.suffix.lower() == ".xlsx":
            from openpyxl import load_workbook
            book = load_workbook(p, read_only=True, data_only=False)
            errors, formulas = [], 0
            try:
                for sheet in book:
                    for row in sheet:
                        for cell in row:
                            if cell.data_type == "e":
                                errors.append(f"{sheet.title}!{cell.coordinate}: {cell.value}")
                            formulas += cell.data_type == "f"
                result.update(sheets=book.sheetnames, error_cells=errors, formulas=formulas,
                              calculation_check="not recalculated; formula correctness is unverified")
            finally:
                book.close()
        elif p.suffix.lower() == ".pdf":
            import fitz
            with fitz.open(p) as document:
                result["pages"] = len(document)
        elif p.suffix.lower() == ".pptx":
            from pptx import Presentation
            result["slides"] = len(Presentation(p).slides)
            result["visual_check"] = "not rendered; visual layout remains unverified"
        result["status"] = "structure_checked"
        result["meaning_check"] = "Contents have not been checked against the user's requirements."
    except Exception as exc:
        result.update(status="validation_failed", error=str(exc))
    return result

class EmailReceipts:
    def __init__(self, directory, agent_name):
        """agent_name tags each sent mail ("<Name>Receipt") so it can be found again in Sent Items."""
        self.directory = Path(directory)
        self.tag = agent_name + "Receipt"
        self.directory.mkdir(parents=True, exist_ok=True)

    def claim(self, payload, resend=False):
        fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        index = self.directory / (fingerprint + ".json")
        with memory_lock(index):
            if index.exists() and not resend:
                return json.loads(index.read_text(encoding="utf-8")), False
            receipt = {"id": uuid.uuid4().hex, "status": "prepared", "created": time.time(),
                       "fingerprint": fingerprint, "to": payload["to"], "subject": payload["subject"],
                       "from": payload["from_address"]}
            atomic_json(index, receipt)
            atomic_json(self.directory / (receipt["id"] + ".json"), receipt)
            return receipt, True

    def save(self, receipt, **updates):
        index = self.directory / (receipt["fingerprint"] + ".json")
        path = self.directory / (receipt["id"] + ".json")
        with memory_lock(index):
            current = json.loads(path.read_text(encoding="utf-8")) if path.exists() else dict(receipt)
            ranks = {"prepared": 0, "draft_saved": 1, "send_outcome_unknown": 2, "submitted": 3, "confirmed_sent": 4}
            if ranks.get(updates.get("status"), -1) < ranks.get(current.get("status"), -1):
                updates.pop("status", None)
                updates.pop("evidence", None)
            current.update(updates, updated=time.time())
            receipt.update(current)
            atomic_json(path, receipt)
            indexed = json.loads(index.read_text(encoding="utf-8")) if index.exists() else receipt
            if indexed["id"] == receipt["id"]:
                atomic_json(index, receipt)
        return receipt

    def get(self, ident):
        if not ident or any(c not in "0123456789abcdef" for c in ident):
            raise ValueError("Invalid receipt ID.")
        return json.loads((self.directory / (ident + ".json")).read_text(encoding="utf-8"))

    def confirm(self, outlook, receipt):
        """Find correlation property in Sent Items; no assumptions about delivery."""
        if receipt["status"] == "confirmed_sent":
            return receipt
        try:
            accounts = outlook.Session.Accounts
            account = next(accounts.Item(i) for i in range(1, accounts.Count + 1)
                           if accounts.Item(i).SmtpAddress.lower() == receipt["from"].lower())
            items = account.DeliveryStore.GetDefaultFolder(5).Items
            items.Sort("[SentOn]", True)
            for i in range(1, min(items.Count, 100) + 1):
                item = items.Item(i)
                prop = item.UserProperties.Find(self.tag)
                if prop and prop.Value == receipt["id"]:
                    return self.save(receipt, status="confirmed_sent", sent_entry_id=item.EntryID,
                                     evidence=f"Matched {self.tag} in the sender's Sent Items. Recipient delivery unverified.")
            return self.save(receipt, sent_check="No matching item in the latest 100 Sent Items; delivery remains unverified.")
        except Exception as exc:
            return self.save(receipt, sent_check="Sent Items check unavailable: " + type(exc).__name__)

    def send(self, outlook, payload, resend=False):
        # Validate sender and attachment paths BEFORE claiming or submitting anything.
        accounts = outlook.Session.Accounts
        matched = next((accounts.Item(i) for i in range(1, accounts.Count + 1)
                        if accounts.Item(i).SmtpAddress.lower() == payload["from_address"].lower()), None)
        if matched is None:
            return {"status": "not_sent", "error": "Requested sending account is not connected."}
        paths = [Path(p).resolve() for p in payload["attachments"]]
        if any(not p.is_file() for p in paths):
            return {"status": "not_sent", "error": "An attachment is missing."}
        # Different attachment contents are a different action.
        payload = dict(payload, attachment_hashes=[hashlib.sha256(p.read_bytes()).hexdigest() for p in paths])
        receipt, fresh = self.claim(payload, resend)
        if not fresh:
            self.confirm(outlook, receipt)
            return dict(receipt, replay="Not sent again. An intentional duplicate requires resend=true and Alex's explicit request.")
        try:
            mail = outlook.CreateItem(0)
            mail.To, mail.Subject, mail.Body, mail.CC = payload["to"], payload["subject"], payload["body"], payload["cc"]
            mail._oleobj_.Invoke(*(64209, 0, 8, 0, matched))
            mail.UserProperties.Add(self.tag, 1).Value = receipt["id"]
            for p in paths:
                mail.Attachments.Add(str(p))
            mail.Save()
            self.save(receipt, status="draft_saved", draft_entry_id=mail.EntryID)
            # Persist uncertainty before crossing the external-effect boundary.
            self.save(receipt, status="send_outcome_unknown")
            mail.Send()
            self.save(receipt, status="submitted", evidence="Outlook accepted Send(); Sent Items not yet confirmed.")
            return self.confirm(outlook, receipt)
        except Exception as exc:
            # Never label a timeout/COM exception as proof that nothing was sent.
            return self.save(receipt, error=type(exc).__name__,
                             warning="Inspect the draft, Outbox and Sent Items before any retry.")
