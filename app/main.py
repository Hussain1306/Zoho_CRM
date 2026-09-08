"""
Brandovise Contract Console - main FastAPI app.

Routes:
  GET  /                         -> the single-page UI (import + console)
  POST /import/preview           -> parse+validate uploaded CSVs, no writes
  POST /import/confirm           -> actually create records in Zoho
  GET  /contracts                -> list contracts (joined with contact info) for the console
  POST /contracts/{id}/followup  -> create renewal Task, mark Follow_Up_Created
"""

from datetime import datetime, date

from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from fastapi import Request

from . import importer
from . import zoho_crm
from .zoho_client import zoho_get

app = FastAPI(title="Brandovise Contract Console")
templates = Jinja2Templates(directory="app/templates")
app.mount("/static", StaticFiles(directory="app/static"), name="static")


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(request, "index.html", {})


@app.post("/import/preview")
async def import_preview(contacts_file: UploadFile = File(...), contracts_file: UploadFile = File(...)):
    contacts_text = (await contacts_file.read()).decode("utf-8")
    contracts_text = (await contracts_file.read()).decode("utf-8")

    result = importer.build_preview(contacts_text, contracts_text)

    # Stash the raw text in-memory keyed by nothing durable - for a real app we'd
    # use a session/token, but for a single-operator console we just ask the
    # browser to re-send the same files on /confirm (simplest thing that works).
    return {
        "contacts_read": result.contacts_read,
        "contracts_read": result.contracts_read,
        "issues": [
            {"severity": i.severity, "row_ref": i.row_ref, "message": i.message}
            for i in result.issues
        ],
    }


@app.post("/import/confirm")
async def import_confirm(contacts_file: UploadFile = File(...), contracts_file: UploadFile = File(...)):
    contacts_text = (await contacts_file.read()).decode("utf-8")
    contracts_text = (await contracts_file.read()).decode("utf-8")

    result = importer.build_preview(contacts_text, contracts_text)

    created_contact_count = 0
    reused_contact_count = 0
    created_contract_count = 0
    reused_contract_count = 0
    failed = []

    contact_zoho_ids = {}  # kundennummer -> zoho id

    for contact in result.contacts_to_create:
        try:
            zoho_id, was_created = zoho_crm.create_contact(contact)
            contact_zoho_ids[contact["kundennummer"]] = zoho_id
            if was_created:
                created_contact_count += 1
            else:
                reused_contact_count += 1
        except Exception as e:
            failed.append({"ref": contact["kundennummer"], "stage": "contact", "error": str(e)})

    for contract in result.contracts_to_create:
        contact_id = contact_zoho_ids.get(contract["kundennummer"])
        if not contact_id:
            failed.append({"ref": contract["vertragsnummer"], "stage": "contract", "error": "linked contact was not created"})
            continue
        try:
            zoho_id, was_created = zoho_crm.create_contract(contract, contact_id)
            if was_created:
                created_contract_count += 1
            else:
                reused_contract_count += 1
        except Exception as e:
            failed.append({"ref": contract["vertragsnummer"], "stage": "contract", "error": str(e)})

    return {
        "created_contacts": created_contact_count,
        "reused_contacts": reused_contact_count,
        "created_contracts": created_contract_count,
        "reused_contracts": reused_contract_count,
        "failed": failed,
        "issues": [
            {"severity": i.severity, "row_ref": i.row_ref, "message": i.message}
            for i in result.issues
        ],
    }


def _days_until(iso_date: str | None) -> int | None:
    if not iso_date:
        return None
    d = datetime.strptime(iso_date, "%Y-%m-%d").date()
    return (d - date.today()).days


@app.get("/contracts")
def list_contracts(makler: str | None = None, expiry_window: int | None = None):
    """Fetch all contracts from Zoho, joined with their linked contact's info."""
    params = {"fields": "Name,Beginn,Ablaufdatum,Jahresbeitrag,Status,Makler,Kunde,Follow_Up_Created", "per_page": 200}
    result = zoho_get("/crm/v2/Contracts", params=params)
    records = result.get("data") or []

    # Batch-fetch contact details (email/phone) in ONE call, not one per contract -
    # the brief requires contact details visible in the console, and this keeps
    # the console load to 2 API calls total regardless of contract count.
    contacts_result = zoho_get("/crm/v2/Contacts", params={"fields": "Email,Phone", "per_page": 200})
    contact_details = {
        c["id"]: {"email": c.get("Email"), "phone": c.get("Phone")}
        for c in (contacts_result.get("data") or [])
    }

    out = []
    for r in records:
        contract_makler = r.get("Makler")
        if makler and contract_makler != makler:
            continue

        expiry = r.get("Ablaufdatum")
        days_left = _days_until(expiry)

        if expiry_window is not None:
            if days_left is None or days_left < 0 or days_left > expiry_window:
                continue

        kunde = r.get("Kunde") or {}
        details = contact_details.get(kunde.get("id"), {})
        eligible_for_followup = (
            (contract_makler is not None)
            and (r.get("Status") or "").strip().lower() == "aktiv"
            and days_left is not None
            and days_left >= 0
        )
        out.append({
            "id": r["id"],
            "vertragsnummer": r.get("Name"),
            "beginn": r.get("Beginn"),
            "ablaufdatum": expiry,
            "days_left": days_left,
            "jahresbeitrag": r.get("Jahresbeitrag"),
            "status": r.get("Status"),
            "makler": contract_makler,
            "follow_up_created": r.get("Follow_Up_Created", False),
            "eligible_for_followup": eligible_for_followup,
            "kunde_id": kunde.get("id"),
            "kunde_name": kunde.get("name"),
            "kunde_email": details.get("email"),
            "kunde_phone": details.get("phone"),
        })

    out.sort(key=lambda c: (c["days_left"] if c["days_left"] is not None else 999999))
    return {"contracts": out}


@app.post("/contracts/{contract_id}/followup")
def create_followup(contract_id: str):
    # fetch the contract + linked contact fresh, to avoid trusting stale client state
    result = zoho_get(f"/crm/v2/Contracts/{contract_id}")
    records = result.get("data") or []
    if not records:
        raise HTTPException(404, "contract not found")
    r = records[0]

    kunde = r.get("Kunde") or {}
    contact_id = kunde.get("id")
    if not contact_id:
        raise HTTPException(400, "contract has no linked customer")

    contract = {
        "vertragsnummer": r.get("Name"),
        "ablaufdatum": r.get("Ablaufdatum"),
        "makler": r.get("Makler"),
    }
    if not contract["ablaufdatum"]:
        raise HTTPException(400, "contract has no Ablaufdatum, cannot compute due date")

    status = (r.get("Status") or "").strip().lower()
    if status != "aktiv":
        raise HTTPException(400, f"contract status is {r.get('Status')!r}, not Aktiv - refusing to create a renewal follow-up")

    days_left = _days_until(contract["ablaufdatum"])
    if days_left is not None and days_left < 0:
        raise HTTPException(400, "contract already expired - refusing to create a follow-up with a due date in the past")

    customer_display_name = kunde.get("name") or "(unknown customer)"

    try:
        zoho_crm.create_renewal_task(contract_id, contact_id, contract, customer_display_name)
        zoho_crm.mark_followup_created(contract_id)
    except Exception as e:
        raise HTTPException(500, str(e))

    return {"ok": True}
