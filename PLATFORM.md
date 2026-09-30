# Fabric Pilot Platform

## Delivery Status

This is a runnable local platform foundation, not a production-certified SaaS
deployment. No Azure resources, billing changes, job cancellations, repository
commits or pull requests were created during development.

| Area | Implemented | Remaining Integration |
|---|---|---|
| Identity | RS256 Entra API token validation, issuer/audience/expiry checks, tenant allowlist, OBO credential exchange, explicit application credential factory | Register and consent Entra applications; deploy an access-token-capable frontend/BFF for the API |
| Hosted UI | Native Streamlit OIDC gate when `PILOT_HOSTED=true`; tenant allowlist and identity-session reset | Configure OIDC secrets and HTTPS; Streamlit OIDC does not supply the delegated access token needed for OBO |
| Authorization | Deny-by-default tenant/workspace/capacity/repository guards and short-lived grants | Capacity-admin/ARM/repository grant resolvers; group-overage resolution; tenant-admin inventory collection |
| Collection | Delegated workspace/item/latest-job inventory; normalized KQL query adapter | Eventstream routing, scheduled ingestion workers, source retention and telemetry exporters |
| Assessments | 26 evidence-driven checks across tenant, capacity, workspace and item levels; coverage and findings | Raw Delta/TMDL/BPA/SparkLens/Purview/SQL collectors and validated policy benchmarks |
| Runtime | Six playbook trigger categories; 30-day baseline guards for duration signals | Continuous evaluation, dependency graph, throughput-based SLA forecasting, alert delivery |
| Healing | Approval queue, scoped durable audit, idempotent action creation, bounded backoff, opt-in job retry/cancel adapters | Durable worker/reconciliation, idempotency/dependency-owner approvals, tuned notebook deployment, cache reload adapters |
| Capacity | Demo trend/smoothing/sizing UI; separately guarded, SKU-budget-bound ARM scaling adapter | Verified capacity-to-ARM mapping, Fabric/ARM grants, live CU and billing exporters, automatic scaling policy |
| Agent | Tenant-opted-in GPT-4o-compatible LangChain structured diagnostics; redacted inputs, no executable tools | Tenant-specific model hosting policy, evaluation dataset, source-grounded code-fix generation and CI validation |
| Pull requests | Explicitly approved Azure DevOps draft PR adapter, source-file restrictions and repo guards | Verified repository grants, source definition retrieval, reviewed code changes, durable publishing workflow and PR status UI |
| UI | Eight operational views, scoped filters, role badges, incident notifications, context drawers and simulation policies | Live UI-to-OBO API session bridge; full automation management |
| Persistence | Tenant-keyed SQLite records, action state transitions, per-actor audit | Production PostgreSQL/RLS, encrypted storage/backups, migrations, retention, tamper-resistant audit export |

## Run Locally

From this project directory, using the workspace virtual environment:

```powershell
..\.venv\Scripts\python.exe -m streamlit run app.py --server.address=127.0.0.1 --server.port=8501
..\.venv\Scripts\python.exe -m uvicorn api:app --host 127.0.0.1 --port 8000
..\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Anonymous localhost sessions use a clearly labeled synthetic tenant. Signed-in
local sessions do not inherit demo roles or telemetry. Use Authentication's
permission-refresh action to verify the directory identity and load delegated
Fabric inventory. Graph `/me` and `/organization` plus Fabric read consent are
required. When discovery is denied or incomplete, privileged access stays off.

The API's `/healthz` is public. All `/v1/*` routes require an API-audience bearer
token, an onboarded tenant and verified delegated workspace permissions. There
is no endpoint accepting arbitrary user-supplied roles or execution telemetry.

## Entra Configuration

Set server environment variables through a secret manager; do not put access
tokens or client secrets into source files or chat:

| Variable | Purpose |
|---|---|
| `PILOT_ALLOWED_TENANTS` | Comma-separated tenant UUID allowlist; no wildcard onboarding |
| `PILOT_API_AUDIENCE` | Audience of this API's v2 access tokens |
| `PILOT_CLIENT_ID` | Confidential API application registration |
| `PILOT_CLIENT_SECRET` | Server-side API application credential |
| `PILOT_DATABASE` | Local SQLite path; default `.pilot/pilot.db` |
| `PILOT_ENABLE_EXECUTION` | Exact `true` enables the guarded executor; default disabled |
| `PILOT_HOSTED` | Exact `true` enables the UI's OIDC gate; required for any hosted UI |

Expose the delegated `access_as_user` scope on the API registration. Configure
the frontend to request that scope, then send the access token as a Bearer token
to the API. The backend validates it and performs OBO for Fabric. Do not send an
ID token as an API access token. Configure `requestedAccessTokenVersion=2`.

The OIDC example is `.streamlit/secrets.example.toml`; use real tenant metadata
and locally managed secrets in the ignored `.streamlit/secrets.toml`. Register
the exact `/oauth2callback` URI and use HTTPS for hosted access. UI OIDC and API
OBO are separate integrations: the current Streamlit shell does not implement a
frontend authorization-code/PKCE access-token bridge to the API.

Application credentials never silently replace a denied delegated user. The
`application_credential` factory is for explicitly configured tenant background
workers only; those workers need independently verified resource grants. No
background service-principal worker is launched by this application.

Tenant Administrator does not imply workspace mutation permission. Scaling
requires both the assigned capacity administration scope and Azure write
permission. Contributor/Admin plus repository write permission is required for
PR publication. Group claims with overage are not guessed or elevated.

## Evidence and KQL

Configure `PILOT_KQL_CLUSTER`, `PILOT_KQL_DATABASE` and
`PILOT_KQL_ALLOWED_TENANTS` only after provisioning the source. The collector
uses parameterized tenant/workspace/capacity constraints and filters the
response again in the service layer. No arbitrary cluster URLs are accepted.

Expected normalized table:

```kusto
.create table FabricPilotTelemetry (
    TenantId:string, RecordId:string, Name:string, Kind:string,
    Level:string, WorkspaceId:string, CapacityId:string,
    Evidence:dynamic, ObservedAt:datetime
)
```

`Level` is `tenant`, `capacity`, `workspace` or `item`. `Kind` is `Tenant`,
`Capacity`, `Workspace`, `Lakehouse`, `SemanticModel`, `DataPipeline`, `Notebook`
or `Warehouse`. Evidence field contracts are declared alongside each rule in
`services/assessment.py`. The synthetic examples in `services/demo_estate.py`
show their shapes. Missing evidence is **Not assessed**, never a passing check.
The displayed grade is a simple pass percentage of assessed checks, not a
Microsoft certification or an industry-standard risk score.

The KQL adapter reads the latest record per resource from a 30-day window; it
does not provision Eventstreams, compute historical baselines, or continuously
consume events. Configure authenticated upstream exporters to compute trusted
30-day baselines, CU smoothing, BPA findings, Purview policy evidence, table
maintenance history and SQL/Spark execution summaries. Record truncation and
source freshness must be monitored by the deployment's ingestion worker.

There is no assumed generic Capacity Metrics REST endpoint. Use an approved
Capacity Metrics semantic-model export or other supported telemetry source.
V-Order is policy-dependent and should be benchmarked for the workload. Direct
Lake fallback is not automatically attributable to small files; schema and
memory causes require different fixes. VACUUM retention must preserve safety
and time-travel requirements.

## Execution Lifecycle

1. Collect authorized evidence through `/v1/collect`.
2. Read `/v1/assessments` and `/v1/incidents`.
3. Submit an existing incident ID to `/v1/plans`.
4. Explicitly approve `/v1/plans/{id}/approve`.
5. Execute through `/v1/plans/{id}/execute` only after deployment-level enablement.
6. Reconcile remote status; an HTTP 202 means accepted, not healed.

Plans expire after ten minutes. Each execution rechecks identity permissions
and remote job status. Demo or arbitrary KQL records cannot dispatch live job
mutations. Delta retries additionally require server-verified idempotent-write
approval; reaping requires dependency-owner cancellation approval. The current
collector deliberately does not manufacture these approval flags, so normal
collection alone cannot unlock destructive operations.

Retry delays are 30/60/120 seconds with three supported attempts. There is no
resident retry scheduler yet. Request timeouts enter an unknown-outcome state,
not an automatic retry loop. Live execution remains disabled in this delivery.
The UI's playbook switches and execute control only create session-local
simulation audit entries; they do not dispatch API action plans.

## AI and Repositories

For diagnostics, set `PILOT_AI_ALLOWED_TENANTS`, `AZURE_OPENAI_ENDPOINT`,
`AZURE_OPENAI_DEPLOYMENT`, `AZURE_OPENAI_API_VERSION`, and locally managed
`AZURE_OPENAI_API_KEY`. Use a GPT-4o deployment or approved equivalent. The
structured LangChain path never caches results across users and never executes
generated instructions. Enabling it authorizes sending redacted evidence to
the configured Azure OpenAI resource; determine residency and consent first.

`services/devops.py` accepts reviewed source changes, explicit publication
approval, an independently verified repository grant and a delegated DevOps
credential. It creates a unique branch and a **draft** PR. It does not generate
production-ready code by itself, bypass CI, merge changes, or infer repository
rights from a Fabric workspace role. PR creation failure after a push reports
the branch for reconciliation rather than deleting or blindly repeating it.

## Production Gates

Before external exposure: complete the frontend/API identity bridge; provision
tenant-isolated database and encryption; resolve capacity/ARM/repository grants;
add ingestion, scheduling and operation-reconciliation workers; implement
cooldowns and per-resource concurrency across workers; add rate limits, audit
retention and alert delivery; test revocation and cross-tenant boundaries; run
end-to-end integration tests in a nonproduction Fabric tenant. SQLite and
session-local policies are development infrastructure, not a multiregion SaaS
deployment. Do not enable unattended production writes from this foundation.