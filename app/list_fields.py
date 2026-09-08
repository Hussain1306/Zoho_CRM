"""
Run this once to see the EXACT API names of every field in your Contracts
module, straight from Zoho - no UI hunting needed.

Usage (from the brandovise/ folder, same place you ran zoho_client.py):
    python app/list_fields.py
"""

from zoho_client import zoho_get

import sys

MODULE_API_NAME = sys.argv[1] if len(sys.argv) > 1 else "Contracts"

result = zoho_get("/crm/v2/settings/fields", params={"module": MODULE_API_NAME})

print(f"Fields in module '{MODULE_API_NAME}':\n")
for f in result.get("fields", []):
    label = f.get("field_label")
    api_name = f.get("api_name")
    data_type = f.get("data_type")
    print(f"  label={label!r:35} api_name={api_name!r:35} type={data_type}")
