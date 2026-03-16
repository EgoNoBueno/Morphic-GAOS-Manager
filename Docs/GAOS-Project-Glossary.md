# GAOS Project Glossary

This glossary defines every abbreviation, acronym, and technical term used in Morphic-G AOS documentation. Written for a non-technical business owner — no programming background assumed.

---

## A

- **A2A (Agent-to-Agent)**: The standardized message format every agent uses when sending status updates, task handoffs, alerts, and approval requests to other agents via Cloud Pub/Sub.
- **ADC (Application Default Credentials)**: Google's built-in method for software running on your computer or in the cloud to prove its identity automatically — no password file required — by picking up credentials from the local environment.
- **ADK (Agent Development Kit)**: Google's software framework used to build every AI agent in the system; it provides the base structure that each orchestrator and sub-agent is built on.
- **AGPL (Affero General Public License)**: A restrictive open-source software license the project explicitly blocks; any third-party code licensed under AGPL cannot be imported because it imposes viral legal obligations on commercial products.
- **AOS (Agent Operating System)**: The intelligent workforce system built on Google's cloud ecosystem. It coordinates specialized AI agents to handle business operations autonomously.
- **AP (Accounts Payable)**: Money the business owes to vendors; the Ledger agent monitors and proposes payments for all AP items.
- **API (Application Programming Interface)**: A standardized "plug" that lets two pieces of software talk to each other — for example, every time an agent reads from or writes to your dashboard, it calls Google's Sheets API.
- **AR (Accounts Receivable)**: Money customers owe the business; Ledger opens an AR entry when a deal closes and marks it fulfilled once Foreman confirms the shipment has been sent.
- **AST (Abstract Syntax Tree)**: A structural map of a piece of Python code that the system walks automatically to detect dangerous commands before any agent-written code is submitted to the Approval Gate.

## B

- **Base64**: A method of encoding binary data as plain text so it can travel safely inside a message; every Pub/Sub push delivery wraps its payload in Base64, which the system decodes and validates before processing.
- **BigQuery**: A Google Cloud service used for analyzing large datasets. In the GAOS system it stores agent logs, task outcomes, and approval history for pattern recognition and reporting.

## C

- **CI/CD (Continuous Integration / Continuous Deployment)**: An automated pipeline that tests and delivers software changes; the GAOS-Doctor health check is designed to plug into a CI/CD pipeline so every deployment is validated automatically.
- **CLI (Command-Line Interface)**: A text-based terminal where you type commands; used during setup to create GCP projects, service accounts, Pub/Sub topics, and Cloud Run services.
- **CMEK (Customer-Managed Encryption Keys)**: An optional configuration where you supply your own encryption key for data stored in BigQuery and other GCP services, so Google cannot access your data without your explicit key.
- **Cloud Pub/Sub**: A Google Cloud messaging service used for communication between agents. Ensures reliable message delivery even during restarts or crashes.
- **Cloud Run**: A Google Cloud service for running containerized applications. Used in the GAOS system for scalable, serverless deployment — pay only when agents are actively handling requests.
- **Cold start**: The brief startup delay that occurs when a Cloud Run agent wakes up from zero — relevant because the system is designed around scale-to-zero (pay-per-use) rather than an always-on server.
- **CPL (Cost Per Lead)**: A marketing metric expressing how much is spent to acquire one potential customer; the Beacon agent tracks CPL per campaign and uses it in weekly performance reports.
- **CRM (Customer Relationship Management)**: Software (or a process) for tracking leads, deals, and customer interactions; the Pursuit agent manages all CRM data in the Google Sheet.
- **CVE (Common Vulnerabilities and Exposures)**: A public database of known security flaws in software packages; every third-party library the project imports is checked against CVE records before it is allowed into the codebase.

## D

- **DPA (Data Processing Addendum)**: A legal contract Google provides to GCP customers that commits them not to use your data to train AI models — distinct from the consumer privacy policy that applies to free Google tools.

## E

- **EDI (Electronic Data Interchange)**: A standardized electronic format for exchanging business documents such as purchase orders between companies; the Foreman agent is blocked from writing directly to supplier EDI systems without Approval Gate sign-off.
- **EOL (End of Life)**: A software product that is no longer supported; used to describe a deprecated Google library the project avoids because its API endpoint no longer serves current models.

## F

- **FastAPI**: A Python web framework used to run each agent as an HTTP service on Cloud Run, allowing it to receive incoming Pub/Sub push messages and respond to scheduled trigger calls.

## G

- **GCE (Google Compute Engine)**: Google's virtual machine service; agents deployed on Cloud Run automatically receive their service-account credentials from GCE's metadata server without needing a key file on disk.
- **GCP (Google Cloud Platform)**: Google's suite of paid cloud infrastructure services — the system uses BigQuery, Cloud Run, Pub/Sub, Secret Manager, Cloud Scheduler, and Logging, all within a single GCP billing project.
- **GDPR (General Data Protection Regulation)**: European Union privacy law that requires specific protections for data belonging to EU residents; the system's optional region-locking configuration directly addresses GDPR data-residency requirements.
- **Gemma**: An open-source AI language model family from Google; listed as an alternative to Llama for running on a local GPU when higher-quality local inference is needed.
- **GMEK (Google-Managed Encryption Keys)**: The default encryption Google uses to protect data at rest — Google holds the key, which is convenient but means Google could theoretically access data under legal compulsion.
- **Google Apps Script**: A scripting platform for automating tasks across Google Workspace. Used in the GAOS system for the Approval Gate trigger and webhook workflows.
- **Google Drive**: A cloud storage service used for storing procedural knowledge in the GAOS system.
- **Google Sheets**: A spreadsheet application used as the operational dashboard. It serves as the control plane for live agent status, approval queues, task logs, and business data.
- **Google Workspace**: A suite of productivity tools including Gmail, Drive, and Sheets. The GAOS system leverages free-tier services from Google Workspace.
- **GPU (Graphics Processing Unit)**: A specialized chip that dramatically accelerates AI model computation; the local hardware requirement for running large models via Ollama — a 70B-parameter model needs approximately 48 GB of GPU memory.
- **Grafana**: An open-source data visualization platform planned for Phase 5; it will provide a CEO-facing dashboard that reads from BigQuery and Google Sheets as data sources.
- **gspread**: The Python library the system uses to communicate with Google Sheets; every agent that reads from or writes to the dashboard uses gspread.

## H

- **HMAC (Hash-based Message Authentication Code)**: A cryptographic signature attached to every webhook request from the system; the receiving end recomputes the signature and rejects the request if it doesn't match, preventing fake approval proposals.
- **HRIS (Human Resources Information System)**: Software used to formally manage employee records — hiring, termination, performance reviews; the Steward agent is blocked from writing to any HRIS without Priority-4 Approval Gate sign-off.
- **HSM (Hardware Security Module)**: A tamper-resistant physical device that stores and operates on encryption keys; referenced as an upgrade option for the highest level of key protection when CMEK is enabled.

## I

- **IAM (Identity and Access Management)**: Google Cloud's permission system that controls which service account is allowed to use which Google service; each of the eight agent service accounts has the minimum roles needed for its specific function.

## J

- **JSON (JavaScript Object Notation)**: A lightweight, human-readable text format for structured data; used to encode all agent-to-agent messages, API responses, and the webhook payload that lands in the Approval Gate sheet.

## K

- **KMS (Key Management Service)**: Google Cloud's managed service for creating, storing, and rotating the encryption keys used to protect data at rest.
- **KPI (Key Performance Indicator)**: A measurable target used to gauge success; Nexus-Prime's primary KPI is defined as the business owner's success, which governs how agents prioritize their work.

## L

- **LangGraph**: A software framework that manages each orchestrator's multi-step workflow as a named state machine — handling task parking for human approval, conditional routing between work stages, and resumption after a Pub/Sub event.
- **Llama (e.g., Llama 3.1)**: A family of open-source AI language models from Meta; the default local model, which runs on your machine via Ollama at zero cost per inference call.
- **LLM (Large Language Model)**: An AI model trained on vast amounts of text that can reason, summarize, draft, and classify — Gemini (cloud) and Llama (local) are the two LLMs this system routes work between based on cost and complexity.

## M

- **MCP (Model Context Protocol)**: A standardized protocol for connecting AI models to external tools and data sources; part of the Skills Library component in the technology stack.
- **Memory Bank**: A layered memory system in the GAOS architecture. Includes fast scratchpads, BigQuery for recent history, and long-term storage in Vertex AI Memory Bank.
- **Mistral**: An open-source AI language model produced by Mistral AI; listed as an alternative local model for machines with a dedicated GPU when higher reasoning quality is needed.
- **Mypy**: A Python tool that checks for type errors in code before it runs; part of the static analysis every imported skill must pass before it can be integrated into the system.

## N

- **NER (Named Entity Recognition)**: An AI technique that identifies named entities (company names, people, dollar amounts) in text; referenced as what would be required for thorough automated PII redaction before prompts are sent to cloud AI models.
- **Nexus-Prime**: The general manager agent in the GAOS system. Oversees the entire system, routes jobs, and authorizes operational changes.
- **NSSM (Non-Sucking Service Manager)**: A free Windows utility that registers Ollama as a Windows service so it starts automatically on system boot and restarts if it crashes — the primary reliability fix for keeping the local AI available 24/7.

## O

- **OAuth (Open Authorization)**: An open standard that lets users grant third-party applications limited, scoped access to their Google account without sharing their password; used during local development setup to authorize the system to access Sheets and Drive.
- **OIDC (OpenID Connect)**: An authentication layer built on top of OAuth that Pub/Sub uses to sign each push delivery request so Cloud Run can verify the message genuinely came from Google's infrastructure and not from an outside attacker.
- **Ollama**: Free, locally-installed software that runs open-source AI models directly on your computer; handles all high-frequency, low-complexity agent tasks (logging, formatting, summarizing) at zero cloud cost.

## P

- **P&L (Profit and Loss)**: A financial summary of revenue versus expenses over a period; Ledger generates a weekly P&L summary row every Monday and appends it to the Accounting dashboard tab.
- **PEP 8 (Python Enhancement Proposal 8)**: The official Python style guide defining code formatting conventions; the project's Ruff linter automatically enforces PEP 8 compliance and fails any build where violations are found.
- **PII (Personally Identifiable Information)**: Any data that could identify a specific individual, such as email addresses or phone numbers; the privacy spec defines scrubbing rules to prevent PII from being included in prompts sent to cloud AI models.
- **Pydantic**: A Python library that enforces strict data types on every agent input and output; ensures that a task ID, project ID, and cost figure are always present and correctly typed at every agent boundary.
- **PyPI (Python Package Index)**: The public repository where Python packages are published and downloaded; every third-party library the project imports is sourced from PyPI and vetted for known CVEs.
- **pytest**: The Python testing framework used to run the project's test suite; all tests must pass before any code is committed.

## R

- **RBAC (Role-Based Access Control)**: A permission model where each person in the Authorized Approvers sheet is assigned a numeric tier that determines which proposal priority levels they are authorized to approve.
- **RFC 1918**: An internet standard that defines private IP address ranges (e.g., `10.x.x.x`, `192.168.x.x`); the system blocks any agent from making outbound network calls to these addresses to prevent internal-network hijacking attacks.
- **ROAS (Return on Ad Spend)**: A marketing metric showing how many dollars of revenue are generated per dollar spent on advertising; the Beacon agent tracks 7-day rolling ROAS per campaign and triggers an alert if any campaign drops below 1.0.
- **ROI (Return on Investment)**: The financial return relative to money spent; Beacon incorporates ROI analysis — combining ad spend data with Pursuit's win/loss data — before making any campaign recommendation.
- **Ruff**: The Python linting and formatting tool the project uses; enforces code style and a set of security-oriented rules, and must report zero violations before code passes the static analysis gate.

## S

- **SA (Service Account)**: A Google Cloud identity for a non-human program rather than a person; each of the system's eight agents has a dedicated SA with only the minimum permissions its role requires.
- **SDK (Software Development Kit)**: A pre-packaged collection of tools and libraries that makes it easier to build software for a specific platform; agents must never call Google SDKs directly — they go through the shared tool layer.
- **Serverless**: A cloud deployment model where agents run on Cloud Run and automatically scale to zero instances when idle — you pay only per invocation rather than for always-on servers.
- **SHA-256 (Secure Hash Algorithm, 256-bit)**: A one-way mathematical fingerprint of any text; every code proposal submitted to the Approval Gate is SHA-256 pinned so that if anyone edits the code after submission, the mismatch is detected and the deploy is blocked.
- **SKU (Stock Keeping Unit)**: A unique identifier for a distinct product variant in inventory; Foreman monitors stock levels per SKU and triggers reorder proposals when any SKU reaches its threshold.
- **SLA (Service Level Agreement)**: A defined time commitment for completing a task; agents have internal SLAs — for example, Pursuit must follow up on every qualified lead within the window defined in the sales policy.
- **SQL (Structured Query Language)**: The standard language used to query databases; used to read from and write to BigQuery for agent-history queries, outcome analysis, and weekly cost-summary reports.
- **SSRF (Server-Side Request Forgery)**: A security attack where a compromised agent is tricked into making internal network requests on behalf of an attacker; the tool layer validates every outbound URL and blocks private IP ranges to prevent this.

## T

- **TLS (Transport Layer Security)**: The encryption protocol that protects data while it travels over the internet; all GCP services in this stack communicate exclusively over TLS 1.2 or higher.
- **TTL (Time To Live)**: An expiry timer applied to pending Approval Gate proposals; when a TTL expires, the Cloud Scheduler fires a sweep that re-notifies the owner and eventually auto-rejects the item so stale proposals never silently accumulate.

## U

- **UUID (Universally Unique Identifier)**: A randomly generated ID that guarantees uniqueness; every task, message, memory entry, and knowledge observation is assigned a UUID so all related log entries can be linked without ambiguity.
- **uv**: A fast Python package manager used to install all project dependencies and create the virtual environment in a reproducible way.

## V

- **Vertex AI**: A Google Cloud service for building and deploying machine learning models. Used in the GAOS system for long-term memory storage and knowledge promotion.
- **VPC (Virtual Private Cloud)**: A private network perimeter in Google Cloud that can restrict which services and external IPs are allowed to communicate with your project; the optional VPC Service Controls configuration prevents data exfiltration even if a service-account credential is stolen.
- **VRAM (Video RAM)**: Memory located on a graphics card; the limiting factor when choosing a local AI model — running a 70B-parameter model requires approximately 48 GB of VRAM.

## W

- **winget**: The Windows package manager used to install utilities during setup; referenced specifically for installing NSSM (`winget install nssm`) to register Ollama as a Windows service.
- **Write-Test-Refine loop**: The self-improvement cycle where an agent writes a Python solution in the Vertex AI sandbox, tests it, and iterates up to five times within a 15-minute time limit before submitting the result for human approval.

## Y

- **YAML (YAML Ain't Markup Language)**: A human-readable configuration file format used for `config/settings.yaml`, which stores the AI model aliases, GCP project ID, Pub/Sub topic list, and Sheet workbook ID that every agent reads at startup.

---

## Google Tool Stack Reference

| Tool | Role in GAOS |
|------|-------------|
| **Google Sheets** | Operational dashboard — live agent status, approval queues, task logs, business data |
| **Google Drive** | Procedural knowledge storage — structured folder hierarchy accessible to all agents |
| **Google Apps Script** | Approval Gate trigger and webhook automation |
| **Cloud Pub/Sub** | Agent-to-agent messaging — reliable, decoupled delivery |
| **Cloud Run** | Serverless host for all eight agent containers |
| **BigQuery** | Agent logs, task outcomes, approval history, and weekly analytics |
| **Vertex AI** | Long-term memory storage and knowledge promotion |
| **Secret Manager** | Secure storage for all API keys, HMAC secrets, and credentials |
| **Cloud Scheduler** | Fires timed jobs — TTL sweeps, nightly archive, weekly review |
| **Cloud Logging** | Structured runtime logs from all agents |

---

*This glossary is updated whenever new abbreviations are introduced in the project documentation.*
