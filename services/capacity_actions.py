"""Budget-bounded Azure ARM capacity scale adapter; no implicit workspace escalation."""

import re

import requests

from services.access import AccessDenied, Action, authorize


def scale_capacity(principal, resource, credential, bindings, target_sku, approved=False, enabled=False):
    authorize(principal, Action.SCALE, resource)
    if principal.demo or not enabled or not approved:
        raise AccessDenied("Capacity scaling requires an approved live execution policy.")
    binding = bindings.get((principal.tenant_id, resource.capacity_id))
    if not binding or target_sku not in binding.get("allowed_skus", []):
        raise AccessDenied("The requested SKU is outside the tenant's approved capacity budget.")
    arm_id = binding["arm_resource_id"]
    if not re.fullmatch(r"/subscriptions/[0-9a-fA-F-]{36}/resourceGroups/[\w.()-]+/providers/Microsoft\.Fabric/capacities/[a-z][a-z0-9]{2,62}", arm_id):
        raise ValueError("Invalid configured Fabric ARM resource ID.")
    token = credential.get_token("https://management.azure.com/.default").token
    response = requests.patch("https://management.azure.com" + arm_id,
                              params={"api-version": "2023-11-01"},
                              headers={"Authorization": f"Bearer {token}"},
                              json={"sku": {"name": target_sku, "tier": "Fabric"}},
                              timeout=30, allow_redirects=False)
    if response.status_code not in (200, 202):
        response.raise_for_status()
        raise RuntimeError("Unexpected capacity update response.")
    return {"status": "Accepted; verify provisioning state", "target_sku": target_sku}