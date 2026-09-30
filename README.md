# FABRIC PILOT

Unified, read-only Streamlit app for monitoring **Azure Data Factory** and
**Microsoft Fabric** with a **single Microsoft Entra ID sign-in**.

Fabric Pilot now includes an access-aware operational dashboard, a tenant-scoped
FastAPI backend, hierarchical assessments and guarded remediation adapters.
The original ADF/Fabric monitoring tools remain available in Command Center.
See [PLATFORM.md](PLATFORM.md) for startup, configuration, security boundaries,
implemented capabilities and the remaining production integrations.

## Screens

The **FABRIC PILOT** shell provides a fixed header with notifications and an
account menu, plus eight icon-based Operations destinations. Command Center
retains the original monitoring views and Microsoft Entra ID sign-in.

**Capacity & Cost Optimization** contains illustrative CU consumption charts,
background smoothing, F-SKU recommendations, and throttling alerts. Chart
selection, metric buttons, and alerts open a right-hand details drawer with
sample logs and advisory remediation. All capacity data and notifications are
explicitly demo data; no billing connector or self-healing execution is enabled.
Other Operations destinations now render scoped architectural assessments,
runtime incidents, Direct Lake findings, governance checks, a remediation
backlog and session-local playbook simulations. Local anonymous sessions use
synthetic data; live integrations require verified tenant permissions.

The layout uses Streamlit 1.64+ native controls, Material icons, and a scoped
theme. The IBM Plex Sans font loads from Google Fonts with a local fallback.

Run the UI regression tests from the project directory:

```bash
python -m unittest discover -s tests -v
```

| Tab | Source | Needs sign-in |
|---|---|---|
| 🏠 Home | — | No |
| 📊 Dashboard | Log Analytics (`ADFActivityRun`) or Demo Data | No (Demo) |
| 🧩 Pipeline Activities | ADF REST API (`2018-06-01`) | Yes |
| 🏭 Fabric Monitoring | Fabric REST API (`/v1`) | Yes |
| 🔐 Authentication | Entra ID interactive browser | — |

## Fabric Monitoring sub-tabs

- **📈 Overview** — workspace item inventory and mix
- **⚙️ Pipeline Runs** — job-instance history for Data Pipelines
- **📓 Notebook Runs** — job-instance history for Notebooks / Spark Job Definitions
- **🏋️ Capacities** — capacity SKU / state / region
- **🧯 Error Analytics** — Fabric failures normalised into the ADF schema, then run
  through the same root-cause, recurrence, remediation and escalation engine

## One login, four scopes

The single `InteractiveBrowserCredential` silently acquires each scope as needed:

| Data plane | Scope |
|---|---|
| ARM / ADF | `https://management.azure.com/.default` |
| Log Analytics | `https://api.loganalytics.io/.default` |
| Fabric | `https://api.fabric.microsoft.com/.default` |
| Graph (email alerts) | `https://graph.microsoft.com/.default` |

## Run

```bash
pip install -r requirements.txt
streamlit run app.py --server.address=127.0.0.1
```

## Permissions needed

- **Azure**: Reader on the subscriptions / factories, Log Analytics Reader on the workspace
- **Fabric**: workspace Viewer (or above); delegated `Workspace.Read.All`, `Item.Read.All`
- **Email alerts (optional)**: `Mail.Send` delegated consent on the client app

> The default client ID is the public Azure CLI client. For shared or long-term use,
> register your own public client app (redirect URI `http://localhost`) and grant it
> the Fabric delegated permissions above.

## Safety

Read-only calls only · bound to `127.0.0.1` · telemetry off · SAS tokens,
connection strings and auth headers redacted unless raw mode is toggled on.
