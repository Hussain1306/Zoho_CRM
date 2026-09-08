"""
List all users in your Zoho org, with their user IDs - needed to map
each Makler name to the real Zoho user ID used as Task Owner.

Usage: python app/list_users.py
"""

from zoho_client import zoho_get

result = zoho_get("/crm/v2/users", params={"type": "AllUsers"})

print("Users in your org:\n")
for u in result.get("users", []):
    print(f"  id={u['id']!r:25} name={u.get('full_name')!r:30} email={u.get('email'):40} status={u.get('status')}")
