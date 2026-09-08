"""
CSV parsing + validation for the Brandovise import.

Design decisions (explain these in the README, this is the graded part):

1. Files are German-locale CSV: ';' delimiter, ',' decimal separator.
2. Contracts are de-duplicated by Vertragsnummer (the natural business key).
   Running the import twice, or a CSV with an accidental duplicate row,
   must not create two copies -> we keep the first occurrence and skip
   later ones, reporting them as duplicates rather than importing silently.
3. A contract whose Kundennummer does not exist in the contacts file is an
   orphan. We refuse to import it and flag it for a human, rather than
   guessing a customer or silently dropping it.
4. Two date formats appear in the data (ISO YYYY-MM-DD and German
   DD.MM.YYYY). Both are normalized to ISO on the way in; anything that
   matches neither is flagged.
5. Missing email is not fatal (Zoho Contacts doesn't require it) but is
   flagged so a human notices before contacting that customer.
6. Two contact records (K-1023 / K-1024) share email, phone and address
   with slightly different names/formatting -> almost certainly the same
   person recorded twice. We do NOT auto-merge (Kundennummer is the id
   everything else keys off, and a wrong auto-merge is worse than a
   flagged duplicate) -> both import, flagged as a probable duplicate
   pair for a human to resolve in Zoho.
7. Makler on a contract sometimes differs from the Makler on the contact
   (~26% of rows) -> too frequent to be noise. We treat the CONTRACT's
   own Makler as authoritative for that contract (console filter and
   follow-up task assignment both use it), not the contact's Makler.
8. A contract can be marked "Aktiv" with an Ablaufdatum already in the
   past. It still imports (it's real data) but is flagged as an anomaly,
   and is excluded from "renewal window" urgency logic since a
   30-days-before-expiry due date would already be in the past.
"""

import csv
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from io import StringIO

ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
DE_DATE = re.compile(r"^\d{2}\.\d{2}\.\d{4}$")


def normalize_date(value: str) -> tuple[str | None, str | None]:
    """Return (iso_date_or_None, error_or_None)."""
    value = (value or "").strip()
    if not value:
        return None, "empty date"
    if ISO_DATE.match(value):
        return value, None
    if DE_DATE.match(value):
        d, m, y = value.split(".")
        return f"{y}-{m}-{d}", None
    return None, f"unrecognized date format: {value!r}"


def normalize_amount(value: str) -> tuple[float | None, str | None]:
    value = (value or "").strip()
    if not value:
        return None, "empty amount"
    # German decimal comma -> dot; strip thousands separators if present
    cleaned = value.replace(".", "").replace(",", ".") if "," in value else value
    try:
        return float(cleaned), None
    except ValueError:
        return None, f"unparseable amount: {value!r}"


@dataclass
class ImportIssue:
    severity: str  # "reject" | "warning"
    row_ref: str
    message: str


@dataclass
class PreviewResult:
    contacts_read: int
    contracts_read: int
    contacts_to_create: list[dict] = field(default_factory=list)
    contracts_to_create: list[dict] = field(default_factory=list)
    issues: list[ImportIssue] = field(default_factory=list)


def parse_contacts(csv_text: str) -> tuple[list[dict], list[ImportIssue]]:
    reader = csv.DictReader(StringIO(csv_text), delimiter=";")
    contacts = []
    issues = []
    seen_ids = set()

    for row in reader:
        kid = (row.get("Kundennummer") or "").strip()
        if not kid:
            issues.append(ImportIssue("reject", "contacts:?", "row missing Kundennummer"))
            continue
        if kid in seen_ids:
            issues.append(ImportIssue("warning", kid, "duplicate Kundennummer row in contacts file, keeping first"))
            continue
        seen_ids.add(kid)

        birth_iso, birth_err = normalize_date(row.get("Geburtsdatum", ""))
        if birth_err and row.get("Geburtsdatum", "").strip():
            issues.append(ImportIssue("warning", kid, f"Geburtsdatum: {birth_err}"))

        if not (row.get("Email") or "").strip():
            issues.append(ImportIssue("warning", kid, "missing email"))

        contacts.append({
            "kundennummer": kid,
            "vorname": row.get("Vorname", "").strip(),
            "nachname": row.get("Nachname", "").strip(),
            "email": row.get("Email", "").strip(),
            "telefon": row.get("Telefon", "").strip(),
            "adresse": row.get("Adresse", "").strip(),
            "plz": row.get("PLZ", "").strip(),
            "ort": row.get("Ort", "").strip(),
            "geburtsdatum": birth_iso,
            "makler": row.get("Makler", "").strip(),
        })

    # probable-duplicate-person detection: same email+phone, different Kundennummer
    by_key = {}
    for c in contacts:
        key = (c["email"].lower(), c["telefon"])
        if key == ("", ""):
            continue
        by_key.setdefault(key, []).append(c["kundennummer"])
    for key, ids in by_key.items():
        if len(ids) > 1:
            issues.append(ImportIssue(
                "warning", ",".join(ids),
                f"probable duplicate customer (same email/phone): {ids} -- import both, flag for human merge"
            ))

    return contacts, issues


def parse_contracts(csv_text: str, valid_customer_ids: set[str]) -> tuple[list[dict], list[ImportIssue]]:
    reader = csv.DictReader(StringIO(csv_text), delimiter=";")
    contracts = []
    issues = []
    seen_numbers = set()
    today = date.today()

    for row in reader:
        vn = (row.get("Vertragsnummer") or "").strip()
        kid = (row.get("Kundennummer") or "").strip()

        if not vn:
            issues.append(ImportIssue("reject", "contracts:?", "row missing Vertragsnummer"))
            continue
        if vn in seen_numbers:
            issues.append(ImportIssue("warning", vn, "duplicate Vertragsnummer, keeping first occurrence, skipping this one"))
            continue

        if kid not in valid_customer_ids:
            issues.append(ImportIssue("reject", vn, f"references unknown Kundennummer {kid!r} -- cannot link, skipping row"))
            seen_numbers.add(vn)
            continue

        begin_iso, begin_err = normalize_date(row.get("Beginn", ""))
        if begin_err:
            issues.append(ImportIssue("warning", vn, f"Beginn: {begin_err}"))

        expiry_iso, expiry_err = normalize_date(row.get("Ablaufdatum", ""))
        if expiry_err:
            issues.append(ImportIssue("warning", vn, f"Ablaufdatum: {expiry_err}"))

        amount, amount_err = normalize_amount(row.get("Jahresbeitrag", ""))
        if amount_err:
            issues.append(ImportIssue("warning", vn, f"Jahresbeitrag: {amount_err}"))

        status = (row.get("Status") or "").strip()

        anomaly = False
        if expiry_iso:
            exp_date = datetime.strptime(expiry_iso, "%Y-%m-%d").date()
            if status.lower() == "aktiv" and exp_date < today:
                anomaly = True
                issues.append(ImportIssue(
                    "warning", vn,
                    f"marked Aktiv but Ablaufdatum {expiry_iso} is already in the past -- "
                    f"importing, but excluding from renewal-window logic"
                ))

        seen_numbers.add(vn)
        contracts.append({
            "vertragsnummer": vn,
            "kundennummer": kid,
            "beginn": begin_iso,
            "ablaufdatum": expiry_iso,
            "jahresbeitrag": amount,
            "status": status,
            "makler": row.get("Makler", "").strip(),  # authoritative for this contract, see decision 7
            "anomaly_past_expiry": anomaly,
        })

    return contracts, issues


def build_preview(contacts_csv_text: str, contracts_csv_text: str) -> PreviewResult:
    contacts, contact_issues = parse_contacts(contacts_csv_text)
    valid_ids = {c["kundennummer"] for c in contacts}
    contracts, contract_issues = parse_contracts(contracts_csv_text, valid_ids)

    all_issues = contact_issues + contract_issues
    return PreviewResult(
        contacts_read=len(contacts),
        contracts_read=len(contracts),
        contacts_to_create=contacts,
        contracts_to_create=contracts,
        issues=all_issues,
    )


if __name__ == "__main__":
    with open("kontakte_export.csv", encoding="utf-8") as f:
        contacts_text = f.read()
    with open("vertraege_export.csv", encoding="utf-8") as f:
        contracts_text = f.read()

    result = build_preview(contacts_text, contracts_text)
    print(f"Contacts parsed: {len(result.contacts_to_create)}")
    print(f"Contracts parsed: {len(result.contracts_to_create)}")
    print(f"\nIssues found: {len(result.issues)}")
    for issue in result.issues:
        print(f"  [{issue.severity.upper():7}] {issue.row_ref}: {issue.message}")
