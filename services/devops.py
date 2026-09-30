"""Explicitly approved code-change publication to a scoped Azure DevOps repository."""

from dataclasses import dataclass
from pathlib import PurePosixPath
from urllib.parse import quote
from uuid import UUID

import requests

from services.access import AccessDenied, Action, Resource, authorize


@dataclass(frozen=True)
class Repository:
    tenant_id: str
    organization: str
    project: str
    repository_id: str
    target_branch: str = "main"


def publish_pull_request(principal, resource, repository, credential, changes, title, approved=False, enabled=False):
    authorize(principal, Action.PULL_REQUEST, resource)
    if (not enabled or not approved or principal.demo or repository.tenant_id != principal.tenant_id
            or repository.repository_id != resource.repository_id):
        raise AccessDenied("PR publication requires an approved live change in an authorized tenant repository.")
    if not changes or len(changes) > 10:
        raise ValueError("Provide between one and ten reviewed file changes.")
    for path, content in changes.items():
        parsed = PurePosixPath(path)
        if not path.startswith("/") or ".." in parsed.parts or "\\" in path or parsed.suffix not in {".py", ".tmdl", ".json", ".sql"}:
            raise ValueError("Only scoped source-code files can be published.")
        if len(content.encode()) > 200_000:
            raise ValueError("Change exceeds the review size limit.")
    repository_id = str(UUID(repository.repository_id))
    base = f"https://dev.azure.com/{quote(repository.organization, safe='')}/{quote(repository.project, safe='')}/_apis/git/repositories/{repository_id}"
    token = credential.get_token("499b84ac-1321-427f-aa17-267ca6975798/.default").token
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    response = requests.get(base + "/refs", headers=headers, params={"filter": "heads/" + repository.target_branch, "api-version": "7.1"}, timeout=20, allow_redirects=False)
    response.raise_for_status()
    target = next((branch for branch in response.json().get("value", []) if branch["name"] == "refs/heads/" + repository.target_branch), None)
    if target is None:
        raise ValueError("Target branch does not exist.")
    from uuid import uuid4
    branch = "refs/heads/fabric-pilot/" + str(uuid4())
    file_changes = []
    for path, content in changes.items():
        response = requests.get(base + "/items", headers=headers, params={"path": path, "versionDescriptor.version": repository.target_branch, "api-version": "7.1"}, timeout=20, allow_redirects=False)
        if response.status_code not in (200, 404):
            response.raise_for_status()
        file_changes.append({"changeType": "edit" if response.status_code == 200 else "add", "item": {"path": path},
                             "newContent": {"content": content, "contentType": "rawtext"}})
    response = requests.post(base + "/pushes", headers=headers, params={"api-version": "7.1"}, timeout=30, allow_redirects=False,
                             json={"refUpdates": [{"name": branch, "oldObjectId": "0" * 40}],
                                   "commits": [{"comment": title[:200], "parents": [target["objectId"]], "changes": file_changes}]})
    response.raise_for_status()
    try:
        response = requests.post(base + "/pullrequests", headers=headers, params={"api-version": "7.1"}, timeout=30, allow_redirects=False,
                                 json={"sourceRefName": branch, "targetRefName": target["name"], "title": title[:200],
                                       "description": "Reviewed Fabric Pilot remediation draft. Required CI and branch policies must pass before merge.", "isDraft": True})
        response.raise_for_status()
    except requests.RequestException as exc:
        raise RuntimeError(f"Branch {branch} was created; reconcile PR status before retrying.") from exc
    body = response.json()
    return {"pull_request_id": body["pullRequestId"], "url": body.get("_links", {}).get("web", {}).get("href", body.get("url", "")), "branch": branch}