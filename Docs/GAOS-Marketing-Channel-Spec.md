# GAOS Marketing Channel Spec

> **Status:** Stub — Phase 1 Research active. Sections 3–4 (MC1–MC3 mandates) are
> implementation-ready. Sections 5–8 (MC4–MC8) are design sketches gated on the
> HUMAN DECISION at the end of Phase 1.
>
> **Last updated:** 2026-04-20

---

## Table of Contents

1. [Overview](#1-overview)
2. [Target Audience Profile](#2-target-audience-profile)
3. [Channel Candidates](#3-channel-candidates)
4. [Phase 1 — Research Mandates (MC1–MC3)](#4-phase-1--research-mandates-mc1mc3)
   - [4.1 MC1 — Channel Audience Mandate](#41-mc1--channel-audience-mandate)
   - [4.2 MC2 — Competitor Audit Mandate](#42-mc2--competitor-audit-mandate)
   - [4.3 MC3 — Platform API Inventory Mandate](#43-mc3--platform-api-inventory-mandate)
   - [4.4 HUMAN DECISION Gate — Channel Strategy Approval](#44-human-decision-gate--channel-strategy-approval)
5. [Phase 2 — Setup (MC4–MC5)](#5-phase-2--setup-mc4mc5)
   - [5.1 MC4 — Beacon Channel Profile Content](#51-mc4--beacon-channel-profile-content)
   - [5.2 MC5 — Channel Setup Automation (n8n)](#52-mc5--channel-setup-automation-n8n)
6. [Phase 3 — Content Pipeline (MC6–MC8)](#6-phase-3--content-pipeline-mc6mc8)
   - [6.1 MC6 — 30-Day Seed Content Calendar](#61-mc6--30-day-seed-content-calendar)
   - [6.2 MC7 — Content Publishing Workflow (n8n)](#62-mc7--content-publishing-workflow-n8n)
   - [6.3 MC8 — Channel Metrics in BI5 (n8n)](#63-mc8--channel-metrics-in-bi5-n8n)
7. [Platform API Capability Matrix](#7-platform-api-capability-matrix)
8. [Approval Gate Proposal Design](#8-approval-gate-proposal-design)
9. [Dependencies and Risk](#9-dependencies-and-risk)

---

## 1. Overview

This spec governs SL10 Products' marketing channel build-out — from platform selection
through automated publishing. The initiative is structured as three sequential phases,
each gated on human approval before the next phase begins.

**Business context:** SL10 Products serves the B2B commercial maintenance market
(facility managers, building operators, commercial cleaning service teams). The initial
social media presence is zero. Phase 1 research determines which 1–2 platforms to launch
first and what organic content strategy looks like before any paid spend is considered.

**Agent ownership:**
- **Scout** — Research phases (MC1, MC2, MC3): audience discovery, competitor audit, API inventory.
- **Beacon** — Content phases (MC4, MC6): profile content, 30-day seed calendar.
- **Nexus-Prime** — Approval Gate orchestration, HUMAN DECISION routing.
- **n8n** — External automation (MC5, MC7, MC8): OAuth setup, publishing, metrics ingest.

---

## 2. Target Audience Profile

SL10's audience consists of **B2B decision-makers and practitioners in the commercial
maintenance industry**. Research mandates should be framed around these personas:

| Persona | Role | Content Needs |
|---------|------|---------------|
| Facility Manager | Evaluates and procures maintenance products for commercial buildings | Product comparisons, ROI evidence, compliance info |
| Building Operator | Daily use of maintenance products; influencer on procurement | How-to guides, product demos, tips and tricks |
| Service Technician | Performs the physical maintenance work | Technical specs, application guides, product news |
| Procurement Manager | Approves purchases, evaluates vendors | Supplier credibility signals, case studies, pricing |

**Primary market segment:** Commercial cleaning supply and equipment for multi-unit
residential buildings, office complexes, and light industrial facilities.

---

## 3. Channel Candidates

Candidates assessed by MC1 (audience presence) and MC3 (API capability). Final selection
is the HUMAN DECISION gate at the end of Phase 1.

| Platform | B2B Audience Signal | API Capability Signal | Priority |
|----------|--------------------|-----------------------|----------|
| LinkedIn | High — facility managers, procurement, operators | Limited Pages API (requires Partner Program) | Primary |
| YouTube | Medium-High — how-to search traffic, long-form product demos | Full Data API v3, no app review required | Primary |
| Meta (Facebook) | Medium — older B2B trades audience, industry groups | Graph API with business login app review | Secondary |
| Instagram | Low-Medium — visual product content, younger trades audience | Graph API (same app review as Facebook) | Secondary |
| TikTok | Low — younger audience, growing B2B presence | Content Posting API (limited, no app review currently) | Exploratory |
| X (Twitter/Threads) | Low | API v2 (paid tiers required for posting volume) | Deprioritized |

> **Note:** The above signals are hypotheses. MC1 and MC2 are the authoritative source.
> This table must be updated after the HUMAN DECISION gate with the approved platform set.

---

## 4. Phase 1 — Research Mandates (MC1–MC3)

### Prerequisite

Before running any mandate, the following GCP secrets must exist in
`morphic-gaos-prod` Secret Manager:

- `GOOGLE_SEARCH_API_KEY` — Google Cloud Console → Credentials → API Key (restricted to Custom Search JSON API)
- `GOOGLE_SEARCH_CX` — Custom Search Engine ID from [cse.google.com](https://cse.google.com)

**MC1 must run first.** Its `recommended_launch_platforms` output must be injected
into the MC2 mandate payload via `--platforms` before MC2 is sent — MC2 seed queries
are scoped to the specific platforms MC1 identifies. MC3 has no platform dependency
and may be started at the same time as MC2 once MC1 output is available.

Execution sequence:
1. `python scripts/send_scout_mandates.py --mandate MC1`
2. Review `recommended_launch_platforms` in the `Research Products` tab
3. `python scripts/send_scout_mandates.py --mandate MC2 MC3 --platforms YouTube LinkedIn`
   (replace `YouTube LinkedIn` with MC1's actual output)

---

### 4.1 MC1 — Channel Audience Mandate

**Objective:** Determine which digital/social platforms the SL10 B2B maintenance
audience (facility managers, building operators, commercial cleaning service teams)
actually uses for professional content consumption, discovery, and vendor research.

**Mandate payload sent to Scout (`RESEARCH_MANDATE`):**

```json
{
  "mandate_id": "MC1",
  "research_domain": "channel_audience",
  "context_hint": "SL10 Products serves B2B commercial maintenance market — facility managers, building operators, service technicians, procurement managers in multi-unit residential and commercial facilities",
  "seed_queries": [
    "which social media platforms do facility managers use professionally 2025 2026",
    "B2B content marketing for commercial cleaning industry platforms",
    "where do building operators research maintenance products online",
    "facility management industry digital media consumption survey",
    "commercial cleaning supply B2B marketing channels effectiveness",
    "LinkedIn vs YouTube B2B industrial trades audience 2026",
    "building operations professional community online platforms",
    "maintenance technician social media usage survey statistics",
    "B2B procurement content consumption platforms facility management",
    "commercial real estate operations digital marketing channels"
  ],
  "output_schema": {
    "ranked_platforms": [
      {
        "platform": "string",
        "rank": "integer (1=highest priority)",
        "estimated_b2b_audience_size": "string e.g. '2M facility managers'",
        "organic_reach_potential": "high | medium | low",
        "primary_content_formats": ["list of format strings"],
        "evidence_summary": "string — why this platform for this audience",
        "source_citations": ["list of [N] citation markers"]
      }
    ],
    "recommended_launch_platforms": ["top 1-2 platform names"],
    "rationale": "string — overall strategic rationale",
    "source_count": "integer",
    "confidence": "float 0.0-1.0"
  },
  "max_queries": 15,
  "max_depth": 3,
  "min_sources": 5,
  "confidence_threshold": 0.70
}
```

**Expected output location:** `Research Products` tab, rows tagged `mandate_id: MC1`.

**KNOWLEDGE_INJECTION trigger:** If confidence ≥ 0.70 and source_count ≥ 5, Scout
publishes `KNOWLEDGE_INJECTION` with `knowledge_type: "market_intel"` and
`domain: "marketing_channels"`.

---

### 4.2 MC2 — Competitor Audit Mandate

**Objective:** Crawl the top 10–15 SL10 competitors on the MC1-recommended platforms.
Identify posting frequency, dominant content formats, estimated engagement rates, content
gaps, and 3–5 underserved angles SL10 can own.

**Mandate payload sent to Scout (`RESEARCH_MANDATE`):**

```json
{
  "mandate_id": "MC2",
  "research_domain": "competitor_audit",
  "context_hint": "SL10 Products serves B2B commercial maintenance — audit competitor social presence on platforms identified in MC1. Competitor types: commercial cleaning supply companies, facility maintenance product manufacturers, janitorial supply distributors.",
  "platforms_to_audit": ["LinkedIn", "YouTube"],
  "seed_queries": [
    "commercial cleaning supply companies LinkedIn content strategy",
    "janitorial supply B2B YouTube channel examples",
    "facility maintenance product manufacturers social media",
    "commercial cleaning equipment brand LinkedIn engagement",
    "SL10 competitors social media content facility management",
    "industrial cleaning supply content marketing examples 2025 2026",
    "building maintenance product companies YouTube how-to",
    "facility management supplier LinkedIn posting frequency",
    "commercial cleaning brand Instagram engagement rate",
    "maintenance product B2B content gaps underserved topics"
  ],
  "output_schema": {
    "competitive_matrix": [
      {
        "competitor_name": "string",
        "platform": "string",
        "estimated_posting_frequency": "string e.g. '3x/week'",
        "dominant_content_formats": ["list of format strings"],
        "estimated_engagement_rate": "string e.g. '1.2%'",
        "content_themes": ["list of theme strings"],
        "content_gap": "string — what they are NOT covering",
        "source_url": "string"
      }
    ],
    "underserved_content_angles": [
      {
        "angle": "string — specific topic SL10 can own",
        "rationale": "string — why competitors are not covering this",
        "suggested_format": "string"
      }
    ],
    "top_competitor_count": "integer",
    "platforms_audited": ["list of platform strings"],
    "source_count": "integer",
    "confidence": "float 0.0-1.0"
  },
  "max_queries": 15,
  "max_depth": 3,
  "min_sources": 5,
  "confidence_threshold": 0.70
}
```

> ⚠️ **MC2 platform dependency:** The `platforms_to_audit` field should be updated with
> MC1 output before sending. The seed queries above default to LinkedIn + YouTube.
> Update `scripts/send_scout_mandates.py` with `--platforms` flag if MC1 recommends
> different platforms.

**Expected output:** `Research Products` tab, rows tagged `mandate_id: MC2`. The
`underserved_content_angles` output feeds directly into the MC6 content calendar.

---

### 4.3 MC3 — Platform API Inventory Mandate

**Objective:** Research which target platforms expose usable content publishing APIs,
what OAuth scopes are required, whether app review is required, and realistic approval
timelines.

**Mandate payload sent to Scout (`RESEARCH_MANDATE`):**

```json
{
  "mandate_id": "MC3",
  "research_domain": "platform_api_inventory",
  "context_hint": "Research publishing API capabilities for social media platforms and third-party scheduling tools for a B2B brand account. For each platform: can we post content (text, video, image) via API? What OAuth app registration is required? How long does approval take? For scheduling tools (Buffer, Publer): what platforms do they support, what OAuth scopes are needed, what are their rate limits and batching/scheduling limits, and do they expose their own API?",
  "seed_queries": [
    "YouTube Data API v3 video upload programmatic posting 2026",
    "LinkedIn Pages API content publishing OAuth scopes 2026",
    "Meta Graph API business content publishing app review requirements",
    "Facebook pages API video post programmatic 2026",
    "TikTok Content Posting API brand account 2026",
    "Instagram Graph API business publishing limitations 2026",
    "social media publishing API comparison B2B 2026",
    "Buffer, Publer, Hootsuite API vs direct platform API publishing",
    "LinkedIn Marketing Developer Program partner requirements",
    "Meta business login app review timeline content publishing",
    "Buffer API OAuth scopes supported platforms rate limits scheduling limits 2026",
    "Publer API publishing capabilities supported platforms batch scheduling rate limits 2026",
    "Buffer vs Publer third-party scheduling API programmatic access comparison 2026"
  ],
  "output_schema": {
    "capability_matrix": [
      {
        "platform": "string",
        "has_publishing_api": "boolean",
        "api_name": "string",
        "supported_content_types": ["video", "image", "text", "carousel"],
        "oauth_scopes_required": ["list of scope strings"],
        "app_review_required": "boolean",
        "estimated_approval_weeks": "integer or null",
        "partner_program_required": "boolean",
        "rate_limits": "string — e.g. '100 posts/day'",
        "notes": "string — key caveats",
        "source_url": "string"
      }
    ],
    "scheduling_tools": [
      {
        "tool_name": "string — e.g. 'Buffer'",
        "has_api": "boolean",
        "supported_platforms": ["list of platforms this tool can post to"],
        "oauth_scopes_required": ["list of scope strings"],
        "rate_limits": "string — e.g. '150 posts/month on free tier'",
        "batching_supported": "boolean",
        "scheduling_supported": "boolean",
        "notes": "string — key caveats or plan-tier restrictions",
        "source_url": "string"
      }
    ],
    "recommended_api_first_platforms": ["platforms with usable APIs, no long review"],
    "long_lead_registrations": ["platforms requiring early app registration"],
    "source_count": "integer",
    "confidence": "float 0.0-1.0"
  },
  "max_queries": 15,
  "max_depth": 3,
  "min_sources": 5,
  "confidence_threshold": 0.70
}
```

**Expected output:** Populates [Section 7 — Platform API Capability Matrix](#7-platform-api-capability-matrix) after review.

**Action trigger:** Any `long_lead_registrations` result must be flagged to Nexus-Prime
immediately — LinkedIn and Meta developer app approvals can take 1–3 weeks and must start
in Week 1 of Phase 1.

---

### 4.4 HUMAN DECISION Gate — Channel Strategy Approval

**Trigger:** MC1 and MC2 both return `requires_approval: true` in their task output.
Scout's `_park` node submits an `ApprovalProposal` to the `Agent_Approvals` tab
consolidating both results.

**What the owner must decide:**
1. Which 1–2 platforms to activate first (from MC1's `recommended_launch_platforms`)
2. Which content angles to prioritize (from MC2's `underserved_content_angles`)
3. Which platforms to activate for API-based MC5 automation — MC3's `long_lead_registrations`
   identifies developer app review timelines; registration is assumed already underway

**Approval format (Agent_Approvals tab):**

| Field | Value |
|-------|-------|
| Agent ID | `scout` |
| Issue | `HUMAN DECISION: Channel strategy selection — MC1+MC2 research complete` |
| Trigger Reason | `RESEARCH_MANDATE MC1 + MC2 complete` |
| Proposed Code | Full MC1+MC2 structured output (JSON) — including `recommended_launch_platforms` and `underserved_content_angles` |
| Status | `Pending` → owner sets to `Approved` to accept Scout's `recommended_launch_platforms`, or edits `Proposed Code` first to override the platform selection |
| Approved By | Owner's identity only (e.g. email address) — **not** platform names |

> **How the platform selection is transmitted:** `Proposed Code` already contains the
> MC1+MC2 structured JSON. When Nexus-Prime processes the `APPROVAL_RESULT`, it calls
> `json.loads(row["Proposed Code"])` and reads `recommended_launch_platforms` (from MC1)
> and `underserved_content_angles` (from MC2) to construct the downstream TASK_HANDOFF
> payloads for MC4 and MC6. `Approved By` records the approver's identity and is never
> parsed for platform or channel names — it maps to `approval_history.approved_by` in BQ.

**What happens after Approved:**
- Nexus-Prime dispatches MC4 to Beacon with the approved platform set
- n8n pipeline for MC5 starts with approved platform credentials
- MC6 content calendar scope is set to approved platforms only

> ⚠️ **Everything in Phase 2 is blocked until this decision is made.** Do not
> start MC4, MC5, or MC6 without owner sign-off on channel selection.

---

## 5. Phase 2 — Setup (MC4–MC5)

> **Status:** Design sketch. Gated on HUMAN DECISION gate completion.

### 5.1 MC4 — Beacon Channel Profile Content

**Owner:** Beacon agent
**Trigger:** Nexus-Prime dispatches `TASK_HANDOFF` to Beacon after HUMAN DECISION approval
**Prerequisite:** Approved platform set from HUMAN DECISION gate

Beacon produces, for each approved platform:
- Bio / About text (platform character-limit aware)
- Keyword tags / hashtag seed list
- Banner/header image creative brief (not the image itself — brief for human designer)
- First-post draft (brand introduction)

All output is submitted as a single `ApprovalProposal` batch. Nothing is published
until the proposal is Approved.

**Approval Gate design:**
- `trigger_reason`: `"MC4: Channel profile content batch — {N} platforms"`
- `proposed_code`: JSON array of profile content objects, one per platform
- Approver reviews for brand voice compliance (see `Docs/brand-voice.md`)

---

### 5.2 MC5 — Channel Setup Automation (n8n)

**Owner:** n8n
**Trigger:** MC4 Approval Gate result `APPROVED`
**Prerequisite:** Platform OAuth apps registered, credentials in n8n credential vault

For API-capable platforms (per MC3): n8n workflow creates/updates the page or channel
with the MC4-approved profile content via platform API.

For non-API platforms: n8n generates and emails a **manual-setup checklist** with
the MC4-approved content pre-formatted for copy-paste.

**n8n workflow sketch — YouTube + LinkedIn first:**

```
Trigger: Webhook from Nexus-Prime (APPROVAL_RESULT for MC4)
  → Node 1: Parse approved profile content JSON
  → Node 2: YouTube Data API — update channel description, keywords
  → Node 3: LinkedIn Pages API — update company page about text
  → Node 4: Pub/Sub publish: STATUS_UPDATE to nexus-prime
  → Node 5: BQ log: api_call_log entry per platform call
```

> **Note:** MC5 cannot automate a platform until its developer app is approved.
> See §9 (Dependencies and Risk) for expected review timelines.

---

## 6. Phase 3 — Content Pipeline (MC6–MC8)

> **Status:** Design sketch. MC6 and MC7 are gated on MC5 completion only. MC8 is additionally gated on BI5 14-day validation (see §6.3).

### 6.1 MC6 — 30-Day Seed Content Calendar

**Owner:** Beacon agent
**Trigger:** MC5 complete; Nexus-Prime dispatches `TASK_HANDOFF` to Beacon
**Prerequisite:** Approved platform set; MC2 `underserved_content_angles` available in the
TASK_HANDOFF payload (see below)

Beacon generates, for each active platform, 30 days of content:
- Post copy (platform-length-aware)
- Image/video prompt (for human creative execution)
- Hashtag set
- Posting time recommendation
- Content theme tag (maps to MC2 underserved angles)

Single `ApprovalProposal` batch. MC7 is blocked until this is Approved.

#### MC6 TASK_HANDOFF payload shape

When Nexus-Prime dispatches the MC6 `TASK_HANDOFF`, it **must** embed the MC2
`underserved_content_angles` array directly in the payload. Beacon's `_plan` node appends
`msg.payload` to `pending_items` unchanged, so the angles are available to the planning
LLM call with no additional sheet reads.

Nexus-Prime reads the angles from the MC2 `APPROVAL_RESULT` — specifically from the
`proposed_code` field of the MC2+MC1 combined `ApprovalProposal` row in `Agent_Approvals`
(the HUMAN DECISION proposal written by Scout's `_park` node).

**Required payload fields:**

```json
{
  "task_type": "content_calendar_30day",
  "mandate_id": "MC6",
  "approved_platforms": ["YouTube", "LinkedIn"],
  "underserved_content_angles": [
    {
      "angle": "string — specific topic SL10 can own",
      "rationale": "string — why competitors are not covering this well",
      "suggested_format": "string — video | article | infographic | etc"
    }
  ],
  "context_hint": "SL10 Products B2B commercial maintenance — 30-day seed content calendar"
}
```

> ⚠️ **Implementation note:** `underserved_content_angles` originates as structured JSON
> in the MC2 Scout output. Scout's `_park` node writes it to `Agent_Approvals.proposed_code`
> as a serialized string. When Nexus-Prime processes the HUMAN DECISION `APPROVAL_RESULT`,
> it must `json.loads()` the `proposed_code` field and extract the `underserved_content_angles`
> key before constructing the MC6 TASK_HANDOFF. Do **not** attempt to read this data from
> the `Research Products` tab — Scout's `_report` node truncates the full output to 500
> characters in the `summary` column, which is insufficient for structured angle extraction.

---

### 6.2 MC7 — Content Publishing + Scheduling (n8n)

**Owner:** n8n
**Trigger:** MC6 Approval Gate `APPROVED`

n8n posts approved content on schedule. Two modes:
1. **Direct API:** Platform supports publishing API (per MC3) → n8n posts directly
2. **Buffer/Publer handoff:** Platform requires a scheduling tool → n8n pushes to Buffer
   via API, which handles the platform posting

**n8n workflow sketch:**

```
Trigger: Cloud Scheduler (daily at 7 AM PT)
  → Node 1: Read today's posts from BQ (approved, not yet published, scheduled_date = today)
  → Node 2: Route by platform (YouTube / LinkedIn / Buffer)
  → Node 3a: YouTube Data API — upload video or post community update
  → Node 3b: LinkedIn Pages API — create share
  → Node 3c: Buffer API — schedule remaining platforms
  → Node 4: BQ update: mark post status = "published", add publish_timestamp
  → Node 5: Pub/Sub STATUS_UPDATE to nexus-prime
```

---

### 6.3 MC8 — Channel Metrics in BI5 (n8n)

**Owner:** n8n
**Trigger:** After 14 days of clean BI5 operation AND MC7 active for ≥7 days
**Prerequisite:** `BI5` n8n workflow validated (see `GAOS-n8n-Integration-Spec.md §5`)

Activates the pre-scaffolded YouTube/LinkedIn metric columns in BI5:
- YouTube: views, watch time, subscribers, top videos
- LinkedIn: impressions, clicks, engagement rate, follower growth

Brings channel data into the Grafana CEO dashboard (see `Docs/GAOS-CEO-Dashboard.md`).

---

## 7. Platform API Capability Matrix

> **Status:** Placeholder — to be populated by MC3 Scout output.
> Run `scripts/send_scout_mandates.py --mandate MC3` and paste structured results here.

| Platform | Publishing API | Content Types | App Review | Est. Approval | Notes |
|----------|----------------|---------------|------------|---------------|-------|
| YouTube | ✓ Data API v3 | Video, community | No | — | Quota: 10K units/day default |
| LinkedIn | Partial (Partner) | Text, image | Yes (Partner Program) | 4–8 weeks | Pages API limited without partner status |
| Meta/Facebook | ✓ Graph API | Video, image, text | Yes (Business Login) | 2–4 weeks | Requires business verification |
| Instagram | ✓ Graph API | Image, video, carousel | Yes (same as Meta) | 2–4 weeks | Shares Meta app with Facebook |
| TikTok | ✓ Content Posting API | Video | No (currently) | — | Limited to 1 video/day per account |
| X (Twitter) | ✓ API v2 | Text, image | No | — | Free tier: 1500 posts/month cap |

_This table is a hypothesis baseline. MC3 results are authoritative — update this section
after Scout completes the MC3 mandate._

---

## 8. Approval Gate Proposal Design

### MC4 and MC6 Proposal Schema

Both MC4 and MC6 submit content batches via `ApprovalProposal`. The `proposed_code`
field (col H in the sheet) carries the JSON content batch — not actual code.

**MC4 proposal format:**

```json
{
  "batch_type": "channel_profile",
  "platforms": ["YouTube", "LinkedIn"],
  "items": [
    {
      "platform": "YouTube",
      "channel_name": "SL10 Products",
      "description": "...",
      "keywords": ["commercial cleaning", "facility maintenance", "..."],
      "first_post_draft": "..."
    }
  ]
}
```

**MC6 proposal format:**

```json
{
  "batch_type": "content_calendar",
  "platform": "LinkedIn",
  "period": "2026-05-01 to 2026-05-30",
  "posts": [
    {
      "date": "2026-05-01",
      "copy": "...",
      "image_prompt": "...",
      "hashtags": ["#FacilityManagement", "..."],
      "theme": "underserved_angle_1",
      "post_time_pt": "09:00"
    }
  ]
}
```

> **Approver instruction:** The owner sets `Status = Approved` (and optionally edits
> `Proposed Code` to override the platform selection before approving). `Approved By`
> records the approver's identity only — it is NOT parsed for platform names.
> Nexus-Prime extracts `recommended_launch_platforms` from `json.loads(proposed_code)`
> when routing to the next agent after approval.

---

## 9. Dependencies and Risk

| Dependency | Risk Level | Mitigation |
|------------|-----------|------------|
| `GOOGLE_SEARCH_API_KEY` + `GOOGLE_SEARCH_CX` secrets not created | **BLOCKS MC1–MC3** | Create before sending any mandates. See TODO.md §Phase 0. |
| LinkedIn Partner Program app review (4–8 weeks) | High | Start registration speculatively in Week 1 — app review does not require knowing final content strategy. MC5 cannot activate LinkedIn until app is approved. |
| Meta Business Login app review (2–4 weeks) | Medium | Start registration speculatively in Week 1 for the same reason. Platform activation at HUMAN DECISION gate assumes registration is already in progress. |
| BI5 14-day validation window | Medium | MC8 cannot start until BI5 has run cleanly. Do not compress this. |
| `scaffold_agent()` for dedicated Beacon content sub-agent | Low | Only needed if MC6 content volume overloads Beacon. Defer until first 30-day calendar is generated. |
| Buffer/Publer API feasibility (OAuth scopes, platform coverage, rate/batch limits) | Medium | MC3 `scheduling_tools` output is the required validation gate for the MC7 Buffer/Publer path. Do not implement MC7 Node 3c until MC3 confirms API availability and rate limits are within operational bounds for the target platform set. |

---

_Last updated: 2026-04-20 — Initial stub created for Phase 1 mandate launch._
