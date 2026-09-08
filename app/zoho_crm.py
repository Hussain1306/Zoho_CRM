"""
Zoho CRM write operations for the import + follow-up flow.

Idempotency strategy (why running the import twice is safe):
  - Before creating a Contact, we search Zoho for an existing record
    with the same Kundennummer. If found, we reuse it (and can update
    it) instead of creating a duplicate.
  - Same pattern for Contracts, keyed on Vertragsnummer (stored in the
    module's primary "Name" field).
  - This is IN ADDITION to the CSV-level dedup in importer.py, which
    only catches duplicates within a single CSV file. This module's
    dedup catches duplicates across separate import runs.

Makler -> Zoho user ID mapping:
  Fill this in after running `python app/list_users.py` and matching
  each Makler name to their real Zoho user id.
"""

from datetime import datetime, timedelta
from .zoho_client import zoho_get, zoho_post, zoho_put

CONTACTS_MODULE = "Contacts"
CONTRACTS_MODULE = "Contracts"
TASKS_MODULE = "Tasks"

MAKLER_USER_IDS = {
    "Sabine Weiß": "7597827000000725001",
    "Thomas Krüger": "7597827000000724001",
    "Aylin Öztürk": "7597827000000723001",
    "Michael Brandt": "7597827000000722001",
}


def find_contact_by_kundennummer(kundennummer: str) -> dict | None:
    result = zoho_get(
        "/crm/v2/Contacts/search",
        params={"criteria": f"(Kundennummer:equals:{kundennummer})"},
    )
    records = result.get("data") or []
    return records[0] if records else None


def find_contract_by_vertragsnummer(vertragsnummer: str) -> dict | None:
    # Vertragsnummer is stored in the module's primary field, api_name "Name"
    result = zoho_get(
        f"/crm/v2/{CONTRACTS_MODULE}/search",
        params={"criteria": f"(Name:equals:{vertragsnummer})"},
    )
    records = result.get("data") or []
    return records[0] if records else None


def create_contact(contact: dict) -> tuple[str, bool]:
    """Returns (zoho_record_id, was_created). If it already exists, was_created=False."""
    existing = find_contact_by_kundennummer(contact["kundennummer"])
    if existing:
        return existing["id"], False

    payload = {
        "data": [{
            "First_Name": contact["vorname"],
            "Last_Name": contact["nachname"] or "(unknown)",  # Last_Name is required by Zoho
            "Email": contact["email"] or None,
            "Phone": contact["telefon"] or None,
            "Mailing_Street": contact["adresse"] or None,
            "Mailing_Zip": contact["plz"] or None,
            "Mailing_City": contact["ort"] or None,
            "Date_of_Birth": contact["geburtsdatum"] or None,
            "Kundennummer": contact["kundennummer"],
            "Makler": contact["makler"] or None,
        }]
    }
    result = zoho_post(f"/crm/v2/{CONTACTS_MODULE}", payload)
    record = result["data"][0]
    if record.get("status") != "success":
        # Zoho's own duplicate-detection (e.g. on Email) can reject a create
        # even though OUR Kundennummer-based search above found nothing -
        # this happens when two different Kundennummer rows are actually the
        # same real person (see importer.py decision #6: K-1023/K-1024).
        # Zoho's error tells us which existing record it conflicts with -
        # reuse that one rather than losing the row entirely.
        if record.get("code") == "DUPLICATE_DATA":
            existing_id = record.get("details", {}).get("id")
            if existing_id:
                return existing_id, False
        raise RuntimeError(f"Failed to create contact {contact['kundennummer']}: {record}")
    return record["details"]["id"], True


def create_contract(contract: dict, contact_zoho_id: str) -> tuple[str, bool]:
    existing = find_contract_by_vertragsnummer(contract["vertragsnummer"])
    if existing:
        return existing["id"], False

    payload = {
        "data": [{
            "Name": contract["vertragsnummer"],  # primary field
            "Beginn": contract["beginn"],
            "Ablaufdatum": contract["ablaufdatum"],
            "Jahresbeitrag": contract["jahresbeitrag"],
            "Status": contract["status"] or None,
            "Makler": contract["makler"] or None,
            "Kunde": {"id": contact_zoho_id},  # lookup relationship
            "Follow_Up_Created": False,
        }]
    }
    result = zoho_post(f"/crm/v2/{CONTRACTS_MODULE}", payload)
    record = result["data"][0]
    if record.get("status") != "success":
        raise RuntimeError(f"Failed to create contract {contract['vertragsnummer']}: {record}")
    return record["details"]["id"], True


def create_renewal_task(contract_zoho_id: str, contact_zoho_id: str, contract: dict, customer_display_name: str) -> str:
    """Creates the renewal Task in Zoho, due 30 days before Ablaufdatum, assigned to the
    contract's own Makler (not the contact's - see importer.py decision #7)."""
    expiry = datetime.strptime(contract["ablaufdatum"], "%Y-%m-%d")
    due_date = (expiry - timedelta(days=30)).strftime("%Y-%m-%d")

    owner_id = MAKLER_USER_IDS.get(contract["makler"])
    if not owner_id or owner_id == "REPLACE_ME":
        raise RuntimeError(f"No Zoho user mapped for Makler {contract['makler']!r} - update MAKLER_USER_IDS")

    subject = f"Renewal call — {customer_display_name} ({contract['vertragsnummer']})"

    payload = {
        "data": [{
            "Subject": subject,
            "Due_Date": due_date,
            "Who_Id": {"id": contact_zoho_id},
            "What_Id": {"id": contract_zoho_id},
            "$se_module": CONTRACTS_MODULE,  # tells Zoho which module What_Id belongs to
            "Owner": {"id": owner_id},
            "Status": "Not Started",
        }]
    }
    result = zoho_post(f"/crm/v2/{TASKS_MODULE}", payload)
    record = result["data"][0]
    if record.get("status") != "success":
        raise RuntimeError(f"Failed to create task for {contract['vertragsnummer']}: {record}")
    return record["details"]["id"]


def mark_followup_created(contract_zoho_id: str) -> None:
    payload = {"data": [{"id": contract_zoho_id, "Follow_Up_Created": True}]}
    result = zoho_put(f"/crm/v2/{CONTRACTS_MODULE}", payload)
    record = result["data"][0]
    if record.get("status") != "success":
        raise RuntimeError(f"Failed to update Follow_Up_Created for {contract_zoho_id}: {record}")
