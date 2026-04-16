# Automation Wish List

Ideas for workflows, automations, and agent capabilities to build. Items here feed into the
`Project_Incubator` Sheet tab once scoped into a Vision submission.

---

## Format

Each item should capture:
- **What** — one sentence describing the desired outcome
- **Trigger** — what kicks it off (email, schedule, manual, event)
- **Runtime** — GAOS agent, n8n workflow, or undecided
- **Priority** — High / Medium / Low

---

## Ideas

<!-- Add items below. No format required at capture time — clean up before promoting to Project_Incubator. -->

---

## 📂 General Administration & Operations

| # | What | Trigger | Runtime | Priority |
|---|------|---------|---------|----------|
| A1 | Meeting transcription (via Recall.ai or Fireflies) → summary + action items auto-created in Jira/Notion for named participants | Meeting ends | n8n | High |
| A2 | Auto-rename scanned PDFs based on content (Invoice_Date_Vendor.pdf) and route to correct folder | File lands in Drive | GAOS / n8n | Medium |
| A3 | AI-driven calendar scheduling — handles availability negotiation via scheduling link | Inbound email/request | n8n | Medium |
| A4 | Sync lead/sales data between forms, CRM, and master spreadsheet | Form submission / POS event | n8n | High |
| A5 | Parse flight/hotel confirmation emails → build centralized itinerary + add to calendar | Email received | GAOS (Nexus-Prime) | Low |

---

## 🎧 Customer Support & Engagement

| # | What | Trigger | Runtime | Priority |
|---|------|---------|---------|----------|
| C1 | Inbound ticket triage — classify email/Slack messages (Refund, Bug, Pricing), auto-respond from knowledge base or draft for human approval; targets 70% auto-resolution | Email / Slack message received | n8n (AI Agent node) | High |
| C2 | Vision-based payment verification — AI reads uploaded bank transfer screenshots, confirms payment, updates CRM account status | File upload / email attachment | n8n (Gemini Vision) | Medium |
| C3 | CSAT survey — trigger "How did we do?" email automatically after ticket closed or product delivered | Support ticket closed / delivery confirmed | n8n | Medium |

---

## 💰 Finance & Accounting

| # | What | Trigger | Runtime | Priority |
|---|------|---------|---------|----------|
| F1 | Receipt/invoice OCR — pull PDFs from Gmail, extract line items with AI, sync to ERP/accounting software, flag PO discrepancies | Email attachment / file upload | n8n | High |
| F2 | End-to-end Quote-to-Cash — signed DocuSign contract → create customer in QuickBooks → generate invoice → wait node pings if unpaid after 15 days | Contract signed | n8n | High |
| F3 | Late payment nudges — trigger reminder sequence when invoice passes due date | Daily schedule | n8n | High |
| F4 | Fraud / anomaly detection — monitor transaction logs (Postgres/Snowflake); on deviation from normal pattern, pause and send Human-in-the-loop approval request | Continuous / scheduled | n8n (Human-in-the-loop node) | Medium |
| F5 | Timesheet reminder — ping employees who haven't submitted hours by Friday noon | Schedule (weekly) | n8n | Medium |

> **Note:** F2 supersedes the original standalone "recurring invoicing" idea — the Quote-to-Cash flow covers it end-to-end.

---

## 📣 Marketing & Sales

| # | What | Trigger | Runtime | Priority |
|---|------|---------|---------|----------|
| M1 | Lead intake + LLM scoring — sort new leads by company size/job title/message nuance; hot leads → instant Slack alert; cold leads → nurture sequence | Form submission | n8n (AI Agent node) | High |
| M2 | Proposal follow-up — send check-in email 3 days after proposal if no reply | Proposal sent + 3d silence | GAOS (Nexus-Prime) | High |
| M3 | Social repurposing agent — new video uploaded → transcribe → generate 5 LinkedIn posts + 10 tweets + blog summary → schedule in Buffer/Hootsuite | Video uploaded to Drive/Vimeo | n8n | Medium |
| M4 | Competitor content research — monitor Reddit/X/LinkedIn trends, compile weekly report | Schedule (weekly) | GAOS (Scout) | Medium |
| M6 | LinkedIn Intelligence Scout — new CRM lead triggers scrape of their LinkedIn, summarizes recent posts, drafts hyper-personalized outreach email | Lead created in CRM | n8n | Medium |
| M7 | Brand reputation monitor — scan news and public web for brand mentions, run AI sentiment analysis, compile weekly digest with actionable insights | Schedule (weekly) | GAOS (Scout) / n8n | Medium |
| M8 | GTM playbook generator — input ICP + competitive intel → auto-draft go-to-market strategy doc with recommended channels, messaging, and KPIs | Manual trigger | n8n (AI Agent node) | Medium |
| M9 | GEO (Generative Engine Optimization) — track how AI search engines (Gemini, Perplexity, ChatGPT) describe and recommend the brand; identify missing citations; suggest content updates to improve AI-search visibility | Schedule (weekly) | Undecided (Gauge pattern) | Medium |

> **Note:** M3 upgraded from simple scheduling to full repurposing agent. M5 (CSAT) moved to Customer Support (C3). M9 replaces traditional SEO monitoring — GEO is the 2026 standard.

---

## 🤝 Human Resources

| # | What | Trigger | Runtime | Priority |
|---|------|---------|---------|----------|
| H1 | Resume screening — filter applicants by keywords/certifications before human review | Application submitted | Undecided | Medium |
| H2 | Onboarding checklist — auto-grant software access, send training videos, schedule welcome meetings on contract signed | Contract signed | n8n | High |
| H3 | Internal HR FAQ chatbot — answer common questions (tax forms, benefits, policies) | Chat message | GAOS (Nexus-Prime) | Medium |
| H4 | Employee milestone alerts — notify team of birthdays and work anniversaries | Schedule (daily check) | n8n | Low |
| H5 | Candidate analyzer pipeline — Agent 1 sources profiles (LinkedIn/Indeed), Agent 2 scores resumé against job description, Agent 3 drafts personalized outreach email; human approves before send | New job opening posted | n8n (multi-agent) | Medium |
| H6 | Performance coaching nudges — AI reviews recent output/feedback signals per employee, drafts personalized coaching note for manager review | Schedule (monthly) | Undecided | Low |

---

## 🖥️ Internal Productivity

| # | What | Trigger | Runtime | Priority |
|---|------|---------|---------|----------|
| P1 | SQL Query Visualizer — non-technical staff ask questions in Slack ("What was our top product in March?"), AI writes SQL, queries DB, returns a chart | Slack message | n8n (AI Agent node + QuickChart) | Medium |
| P2 | Internal knowledge base (RAG) agent — indexes all company SOPs and PDFs; employees ask questions and get instant cited answers | Chat message | GAOS (Nexus-Prime + Vertex Search) | High |
| P3 | CrewAI reasoning layer inside GAOS — run a CrewAI crew as a task agent within a Foreman or Nexus-Prime workflow for goals that require dynamic subtask decomposition and multi-agent collaboration; CrewAI handles "who should do what" reasoning, GAOS handles infrastructure, approval gates, cost tracking, and audit trail around it | Task received by Foreman | GAOS (Foreman → CrewAI crew) | Medium |

> **Note:** P2 maps directly to the existing Vertex AI Search datastore already provisioned in GAOS (tools/vertex_search.py). Low implementation delta.
> **Note:** P3 is an architecture evolution, not a standalone automation. CrewAI's dynamic delegation fills the gap where GAOS routing is currently hardcoded. Evaluate after Phase 4 exit.

---

## n8n 2.0 Features to Use

These specific nodes unlock the most valuable items on this list:

| Feature | Relevant Items |
|---------|---------------|
| **Human-in-the-Loop node** | F4 (fraud detection), C1 (ticket drafts), F2 (quote-to-cash exceptions) |
| **AI Agent node** (chooses which tools to call) | C1, M1, P1 |
| **Vision / multimodal input** | C2 (payment screenshots), F1 (invoice PDFs) |
| **Vector Store nodes** (Pinecone/Supabase) | P2 (knowledge base RAG) |
| **Wait node** | F2 (payment follow-up after 15 days), M2 (proposal follow-up) |
| **Google YouTube node** | BI5 (daily performance report) |
| **Google Analytics node** | BI5 (landing page sessions, bounce rate, traffic sources) |
| **Google Sheets node** | BI5 (KPI tracker write), V1 (SaaS audit), BI4 (budget tracker) |

---

## 📢 Advertising & Paid Media

| # | What | Trigger | Runtime | Priority |
|---|------|---------|---------|----------|
| AD1 | Media buying automation — connect ad accounts (Google, Meta, TikTok); AI manages bids and reallocates budget across channels in real-time to hit a target ROAS; alerts human when efficiency floor is breached for 48h | Continuous / campaign live | Albert.ai / Revealbot (SaaS) | Medium |
| AD2 | Ad creative automation — dynamically generate and A/B-test ad variants (copy + visuals); winning variant auto-scaled, losers paused; integrates brand guardrails (negative keywords, tone rules) | Campaign running | Smartly.io / AdCreative.ai (SaaS) | Medium |
| AD3 | Amazon / e-commerce price monitoring — scrape competitor product prices and availability on a schedule; alert via SMS/Slack on significant changes (>X%) | Schedule (hourly/daily) | n8n (ScrapeGraph AI) | Low |
| AD4 | Cross-channel performance report — pull spend, ROAS, CPC, impressions from all ad platforms; compile into a single weekly Slack/email digest with trend arrows | Schedule (weekly) | n8n | Medium |

---

## 📝 Content Research & Production

| # | What | Trigger | Runtime | Priority |
|---|------|---------|---------|----------|
| R1 | Deep research agent — multi-stage pipeline: query web sources → scrape and process → synthesize into structured report; replaces 3–4h of manual desk research | Manual trigger / task created | n8n (AI Agent node) | Medium |
| R2 | AI trends analysis pipeline — fetch latest AI news from X/Twitter (Exa), pull benchmark data (Tavily), compile into a structured weekly market brief | Schedule (weekly) | GAOS (Scout) | Medium |
| R3 | Conference CFP generator — crawl a conference website, RAG over past accepted talks, draft 3 unique proposal abstracts tailored to the program committee's themes | Manual trigger (URL input) | n8n / Undecided | Low |
| R4 | YouTube trend agent — monitor trending topics in a niche, surface video ideas with estimated search demand, auto-add to content calendar | Schedule (daily) | n8n | Low |

---

## ⚖️ Legal & Contracts

| # | What | Trigger | Runtime | Priority |
|---|------|---------|---------|----------|
| L1 | Contract review agent — ingest a draft contract PDF, highlight risky clauses, summarize key obligations and critical dates, output a structured review checklist for attorney sign-off | File uploaded / email attachment | n8n (AI Agent node) | Medium |
| L2 | Legal research assistant — answer plain-language questions on jurisdiction-specific regulations (e.g., R&D tax credits, contractor classification rules); draft a memo with cited primary sources | Chat message / task | Undecided (CoCounsel pattern) | Low |
| L3 | Client / matter intake bot — capture new matter details via form or inbound email, create CRM record, draft engagement letter from a template, route to attorney for review | Form submission / email | n8n | Medium |

> **Policy note:** Per the AI Agent List governance framework, legal agents must operate under a Human-in-the-Loop mandate. No autonomous filing, no unsigned outbound correspondence. All agent outputs are drafts until attorney-approved.

---

## 👷 Contractor Management

| # | What | Trigger | Runtime | Priority |
|---|------|---------|---------|----------|
| CO1 | Contractor onboarding — trigger compliance checklist (W-9, insurance certificate, NDA), dispatch DocuSign for e-signature, create vendor record in accounting software, add to access provisioning queue | Contractor hire event | n8n | High |
| CO2 | Classification compliance monitor — quarterly review of all active contractor relationships against IRS/DOL employee-classification criteria; flag any relationship with ≥3 employee-like dependency indicators for legal review | Schedule (quarterly) | GAOS / Undecided | Medium |
| CO3 | Invoice validation — match contractor invoice against agreed SOW rate × hours logged; flag over-bill or missing line items; route to approver via Slack with one-click Approve/Reject | Invoice received (email/upload) | n8n (AI Agent + Human-in-the-Loop node) | Medium |

---

## 🛡️ IT & Cybersecurity

| # | What | Trigger | Runtime | Priority |
|---|------|---------|---------|----------|
| IT1 | Phishing / social-engineering shield — scan inbound emails for deepfake indicators, brand impersonation, and suspicious link patterns; quarantine flagged messages before inbox delivery; daily summary of blocked items | Continuous (email received) | Undecided (Huntress / KnowBe4 pattern) | High |
| IT2 | Security hygiene monitor — track which employees haven't completed phishing-simulation tests; auto-enroll overdue staff and notify their manager | Schedule (monthly) | n8n | Low |

---

## 📦 Inventory & Supply Chain

| # | What | Trigger | Runtime | Priority |
|---|------|---------|---------|----------|
| IS1 | Inventory reorder automation — monitor sales velocity in real-time; auto-draft or submit POs when on-hand stock falls below a dynamic reorder point; scan logistics data feeds for shipping delay alerts and suggest backup suppliers | Continuous / daily | Undecided (Inventory Planner / Netstock pattern) | Low |

---

## 🔧 Platform Reference Library

Curated SaaS agents and open-source frameworks from the AI Agent List. Use when scoping a Project_Incubator submission to identify the fastest path to implementation.

**Advertising & Media**
| Tool | Category | Notes |
|------|----------|-------|
| [Albert.ai](https://albert.ai) | Media buying | Full campaign lifecycle across Google, Meta, Bing |
| [Smartly.io](https://www.smartly.io) | Social ad creative | Dynamic creative optimization |
| [Revealbot](https://revealbot.com) | Rule-based ad automation | Meta, Google, TikTok budget rules |
| [AdCreative.ai](https://www.adcreative.ai) | Ad creative generation | High-performing ad production at scale |
| [Gauge](https://www.withgauge.com) | GEO + Paid Search | AI brand visibility tracking |

**Marketing & Sales**
| Tool | Category | Notes |
|------|----------|-------|
| [Clay](https://www.clay.com) | Lead enrichment | Contact data + AI personalization |
| [11x.ai](https://11x.ai) | Autonomous SDR | Outbound sales rep agent |
| [Enrich Labs](https://www.enrichlabs.ai) | Marketing squad | Helena (digital marketing), Kai (social listening), Sam (SEO/GEO) |
| [NoimosAI](https://noimosai.com) | Growth strategy + social | SMB command center with persistent memory layer |

**Finance & Bookkeeping**
| Tool | Category | Notes |
|------|----------|-------|
| [LayerNext](https://www.layernext.ai) | Autonomous bookkeeping | Daily reconciliation, always-closed books |
| [Botkeeper](https://www.botkeeper.com) | Hybrid bookkeeping | 95% autonomous data entry + bank matching |
| [Docyt](https://www.docyt.com) | Multi-location finance | Lives in email + vendor portals, updates GL in real-time |
| [Inkle](https://www.inkle.ai) | Tax compliance | US startups + global founders; 1099s, federal/state |
| [TaxGPT](https://www.taxgpt.com) | Tax preparation | Scans records for missing deductions, CPA-ready packages |
| [Ramp](https://www.ramp.com) | Spend control | Corporate card + autonomous expense categorization |

**HR & Recruiting**
| Tool | Category | Notes |
|------|----------|-------|
| [Fireflies.ai](https://www.fireflies.ai) | Meeting memory | Transcription, summaries, action items |
| [Motion](https://www.usemotion.com) | AI calendar/task manager | Reschedules automatically when meetings run over |
| [Lindy](https://www.lindy.ai) | Personal assistant agents | Multi-step workflow automation, email to task |

**Customer Support**
| Tool | Category | Notes |
|------|----------|-------|
| [Intercom Fin](https://www.intercom.com) | Web/chat support | 70%+ auto-resolution; escalates to human |
| [Tidio](https://www.tidio.com) | Web chat + email | SMB-friendly chat + email bot |
| [My AI Front Desk](https://www.myaifrontdesk.com) | Voice / phone | 24/7 phone answering with booking actions |

**Legal**
| Tool | Category | Notes |
|------|----------|-------|
| [CoCounsel (Thomson Reuters)](https://www.thomsonreuters.com) | Legal research | Multi-jurisdiction tax + legal memos with citations |

**IT & Security**
| Tool | Category | Notes |
|------|----------|-------|
| [Huntress](https://www.huntress.com) | Managed detection | SMB-grade SOC; autonomous threat quarantine |
| [KnowBe4](https://www.knowbe4.com) | Security awareness | AI-driven phishing simulation + training |
| [Darktrace](https://www.darktrace.com) | Autonomous threat response | Network anomaly detection |

**Inventory & Supply Chain**
| Tool | Category | Notes |
|------|----------|-------|
| [Inventory Planner](https://www.inventory-planner.com) | Demand forecasting | PO automation based on sales velocity |
| [Netstock](https://www.netstock.com) | Inventory optimization | Excess stock + stockout prevention |

**Build-Your-Own Frameworks**
| Tool | Category | Notes |
|------|----------|-------|
| [CrewAI](https://www.crewai.com) | Multi-agent orchestration | Role-based agents collaborating on a single objective |
| [Google ADK](https://google.github.io/adk-docs/) | Agent development | Used in GAOS; Startup Idea Validator and Trends Analyzer examples on GitHub |
| [Gumloop](https://www.gumloop.com) | No-code agent builder | Connect LLMs to internal data + ad platforms |

---

## 🔁 Post-Sale Customer Lifecycle

| # | What | Trigger | Runtime | Priority |
|---|------|---------|---------|----------|
| PS1 | Customer health scoring — monitor product usage signals, support ticket volume, and payment history; assign a health score (Green/Yellow/Red) and alert CSM when an account flips to Red | Continuous / daily | GAOS (Foreman) / n8n | High |
| PS2 | Churn prediction nudge — when health score drops below threshold, auto-enqueue a personalized "check-in" email draft for human review before sending | Health score update | GAOS (Nexus-Prime) | High |
| PS3 | Upsell/cross-sell trigger — after a customer uses Feature X for 30 days, surface a personalized upgrade prompt via email or in-app message | Usage event / schedule | n8n | Medium |
| PS4 | Renewal automation — 90/60/30 days before contract renewal, send templated reminder sequence; if no response by Day 14, escalate to account owner via Slack | Schedule (relative to renewal date) | n8n | High |
| PS5 | NPS / review request — after onboarding milestone or 90-day mark, trigger an NPS survey; positive scores automatically request a Google/G2 review | Schedule / milestone event | n8n | Medium |

---

## 🏷️ Vendor & SaaS Subscription Management

| # | What | Trigger | Runtime | Priority |
|---|------|---------|---------|----------|
| V1 | SaaS subscription audit — inventory all active subscriptions (pulled from credit card feeds or bank exports), flag tools with zero logins in 30 days, estimate annual waste | Schedule (monthly) | n8n (AI Agent node) | High |
| V2 | Vendor contract renewal alert — 60 days before any vendor contract auto-renews, notify owner with current cost, usage summary, and a one-click "flag for re-quote" button | Schedule (daily check against contract dates) | n8n | High |
| V3 | Competitive re-quote assistant — when a vendor renewal is flagged, agent researches current market pricing for equivalent tools and drafts a negotiation brief | Renewal flagged (V2) | GAOS (Scout) / Undecided | Medium |
| V4 | Duplicate tool detector — scan the SaaS inventory for capability overlap (e.g., two project management tools, three video conferencing subscriptions) and surface rationalization candidates | Manual trigger / quarterly | n8n (AI Agent node) | Medium |

---

## 📊 Executive & BI Reporting

| # | What | Trigger | Runtime | Priority |
|---|------|---------|---------|----------|
| BI1 | Weekly KPI digest — pull revenue, pipeline, support volume, and top metrics from BigQuery/CRM/accounting; compose a one-page Slack/email summary with MoM trend indicators | Schedule (Monday 7 AM) | GAOS (Foreman) / n8n | High |
| BI2 | Automated board-pack generation — compile approved data sources into a structured slide deck (Google Slides via API) ahead of monthly/quarterly board meetings | Schedule (relative to meeting date) | n8n | Medium |
| BI3 | Anomaly alert — monitor key metrics (daily revenue, ticket volume, ad spend) against a rolling baseline; alert Slack channel when any metric deviates >2σ from the 30-day average | Continuous / hourly | GAOS (Foreman) | High |
| BI4 | Department budget vs. actual tracker — pull actuals from accounting software weekly, compare to approved budget per department, flag any line over 90% burn before month-end | Schedule (weekly) | n8n | Medium |
| BI5 | **[PILOT — Phase A: GA4 only]** Daily digital performance report — pull Google Analytics 4 (sessions, bounce rate, avg. session duration, goal conversions, traffic source breakdown) on a morning schedule; write raw metrics to a KPI tracker Sheet (rolling 7-day and 30-day baselines maintained); email a formatted digest with directional trend arrows. YouTube columns scaffolded in the Sheet but inactive until channel launches (Phase B). | Schedule (daily 7 AM) | n8n (GA4 + Sheets + Email nodes) | High |
| BI6 | KPI anomaly escalation — when the daily BI5 pull detects a metric deviating >20% from its 7-day baseline, publish an ALERT to GAOS Foreman for awareness and cross-domain context | Triggered by BI5 (Class 2 hook) | n8n → Pub/Sub → GAOS (Foreman) | Medium |

> **Note:** BI5 is the selected n8n pilot workflow (see `GAOS-n8n-Integration-Spec.md §5`). GA4 is the active data source; YouTube columns are reserved in the Sheet schema and will be activated when the channel launches. BI6 is the Phase 2 upgrade — implement only after BI5 runs cleanly for 14 days.

---

## 🔒 Compliance & Data Governance

| # | What | Trigger | Runtime | Priority |
|---|------|---------|---------|----------|
| DG1 | GDPR/CCPA data subject request handler — on receipt of a "delete my data" or "export my data" request, auto-locate all PII records across CRM, BigQuery, and email archives; compile report for human review before actioning | Email / form submission | GAOS (Nexus-Prime) | High |
| DG2 | Data retention enforcement — on a scheduled run, identify records in BigQuery/Sheets older than the configured retention window; archive to cold storage or flag for deletion approval | Schedule (monthly) | n8n / GAOS | Medium |
| DG3 | SOC 2 / audit evidence collector — automatically export access logs, change logs, and policy acknowledgments on a schedule and deposit into a locked Drive folder for auditors | Schedule (quarterly) | n8n | Medium |
| DG4 | New-employee data access review — when an employee is offboarded, trigger a checklist to revoke SSO, de-provision SaaS seats, and transfer Drive ownership; log each step for audit trail | HR offboarding event | n8n (Human-in-the-Loop node) | High |

---

## ☁️ Cloud & Infrastructure Cost Monitoring

| # | What | Trigger | Runtime | Priority |
|---|------|---------|---------|----------|
| CC1 | GCP billing spike alert — monitor daily Cloud Billing exports in BigQuery; if any service's daily cost exceeds its 7-day moving average by >50%, send an immediate Slack/email alert with the top cost drivers | Continuous / hourly | GAOS (Foreman) | High |
| CC2 | Idle resource detector — weekly scan for Cloud Run services with zero invocations, unattached persistent disks, and unused static IPs; compile a teardown candidate report | Schedule (weekly) | n8n / gcloud script | Medium |
| CC3 | Monthly cloud cost report — summarize GCP spend by service and project, compare to prior month, project end-of-month total, and email to owner | Schedule (1st of month) | n8n | Medium |

---

## 🛒 E-Commerce Order Lifecycle

| # | What | Trigger | Runtime | Priority |
|---|------|---------|---------|----------|
| EC1 | Abandoned cart recovery — detect cart abandonment after 1 hour; send a personalized email sequence (3 touches over 48h) with the abandoned items and a time-limited incentive | Cart abandoned event (webhook) | n8n | High |
| EC2 | Order status notifications — auto-trigger shipping confirmation, out-for-delivery, and delivered emails with tracking links; escalate to support queue if delivery is 2+ days late | Order status change (webhook) | n8n | High |
| EC3 | Return / refund processing — customer initiates return via web form; agent validates eligibility against policy, generates return label, triggers refund in payment processor, updates inventory count | Form submission | n8n (AI Agent node + Human-in-the-Loop) | Medium |
| EC4 | Review solicitation — 7 days after confirmed delivery, send a review request email; positive responses routed to Google/Trustpilot, negative responses routed to support queue for follow-up | Schedule (relative to delivery) | n8n | Medium |

> **Note:** EC1–EC4 are conditional — only relevant if the business sells physical or digital goods via an e-commerce storefront. Skip for pure service businesses.

