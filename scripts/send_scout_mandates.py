"""
scripts/send_scout_mandates.py — Send MC1, MC2, and/or MC3 RESEARCH_MANDATE messages to Scout.

Publishes A2AMessage(message_type=RESEARCH_MANDATE) to the ``agent.nexus-prime.events``
Pub/Sub topic, which Scout subscribes to. This simulates what Nexus-Prime would publish
when dispatching structured research to Scout.

Usage:
    # Send all three mandates (MC1 first, then MC2 + MC3 in parallel)
    python scripts/send_scout_mandates.py

    # Send a specific mandate
    python scripts/send_scout_mandates.py --mandate MC1
    python scripts/send_scout_mandates.py --mandate MC2 MC3

    # Override the platform list for MC2 (use after MC1 results are reviewed)
    python scripts/send_scout_mandates.py --mandate MC2 --platforms LinkedIn YouTube

Prerequisites:
    - GOOGLE_SEARCH_API_KEY and GOOGLE_SEARCH_CX secrets created in Secret Manager
    - Scout Cloud Run service running and subscribed to agent.nexus-prime.events
    - ADC configured: gcloud auth application-default login

Spec: Docs/GAOS-Marketing-Channel-Spec.md §4
"""

from __future__ import annotations

import argparse
import sys
import uuid
from typing import Any

# ── Mandate payload definitions ───────────────────────────────────────────────

_MC1_PAYLOAD: dict[str, Any] = {
    "mandate_id": "MC1",
    "research_domain": "channel_audience",
    "context_hint": (
        "SL10 Products serves the B2B commercial maintenance market — "
        "facility managers, building operators, service technicians, and procurement "
        "managers in multi-unit residential buildings, office complexes, and light "
        "industrial facilities. We have zero social media presence and need to determine "
        "which 1-2 platforms our audience actually uses for professional content."
    ),
    "seed_queries": [
        "which social media platforms do facility managers use professionally 2025 2026",
        "B2B content marketing for commercial cleaning industry platforms survey",
        "where do building operators research maintenance products online",
        "facility management industry digital media consumption statistics",
        "commercial cleaning supply B2B marketing channels effectiveness study",
        "LinkedIn vs YouTube B2B industrial trades audience reach 2026",
        "building operations professional community online forums platforms",
        "maintenance technician social media usage survey statistics",
        "B2B procurement content consumption platforms facility management",
        "commercial real estate operations digital marketing channels effectiveness",
    ],
    "output_schema": {
        "ranked_platforms": [
            {
                "platform": "string",
                "rank": "integer (1=highest priority)",
                "estimated_b2b_audience_size": "string e.g. '2M facility managers on LinkedIn'",
                "organic_reach_potential": "high | medium | low",
                "primary_content_formats": ["list of format strings e.g. 'how-to video'"],
                "evidence_summary": "string — why this platform for this audience",
                "source_citations": ["list of [N] citation index markers"],
            }
        ],
        "recommended_launch_platforms": ["top 1-2 platform names"],
        "rationale": "string — overall strategic rationale for the recommendation",
        "source_count": "integer",
        "confidence": "float 0.0-1.0",
    },
    "max_queries": 15,
    "max_depth": 3,
    "min_sources": 5,
    "confidence_threshold": 0.70,
}

_MC2_PAYLOAD: dict[str, Any] = {
    "mandate_id": "MC2",
    "research_domain": "competitor_audit",
    "context_hint": (
        "SL10 Products sells commercial maintenance and cleaning supply products B2B. "
        "Competitor types: commercial cleaning supply companies, facility maintenance product "
        "manufacturers, janitorial supply distributors, commercial cleaning equipment brands. "
        "Audit their presence on the platforms identified in MC1 (defaulting to LinkedIn + YouTube). "
        "Find content gaps and underserved angles SL10 can own."
    ),
    "platforms_to_audit": ["LinkedIn", "YouTube"],  # Updated by --platforms flag
    "seed_queries": [
        "commercial cleaning supply companies LinkedIn content strategy examples",
        "janitorial supply B2B YouTube channel how-to content examples",
        "facility maintenance product manufacturers social media presence",
        "commercial cleaning equipment brand LinkedIn posting frequency engagement",
        "industrial cleaning supply content marketing case studies 2025 2026",
        "building maintenance product companies YouTube subscriber count",
        "facility management supplier LinkedIn content gaps opportunities",
        "commercial cleaning brand YouTube engagement rate benchmarks B2B",
        "maintenance product B2B social media content gaps underserved topics",
        "facility management content marketing best practices competitors",
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
                "content_gap": "string — specific topics this competitor does NOT cover",
                "source_url": "string",
            }
        ],
        "underserved_content_angles": [
            {
                "angle": "string — specific topic SL10 can own",
                "rationale": "string — why competitors are not covering this well",
                "suggested_format": "string — video | article | infographic | etc",
            }
        ],
        "top_competitor_count": "integer",
        "platforms_audited": ["list of platform strings"],
        "source_count": "integer",
        "confidence": "float 0.0-1.0",
    },
    "max_queries": 15,
    "max_depth": 3,
    "min_sources": 5,
    "confidence_threshold": 0.70,
}

_MC3_PAYLOAD: dict[str, Any] = {
    "mandate_id": "MC3",
    "research_domain": "platform_api_inventory",
    "context_hint": (
        "Research programmatic content publishing API capabilities for social platforms "
        "and third-party scheduling tools for a B2B brand account. For each platform: can "
        "we post content (text, video, image) via API without manual UI interaction? What "
        "OAuth app registration is required? How long does developer app review/approval take? "
        "Are there partner program requirements? For scheduling tools (Buffer, Publer): what "
        "platforms do they support, what OAuth scopes are needed, what are their rate limits "
        "and batching/scheduling limits, and do they expose their own API?"
    ),
    "seed_queries": [
        "YouTube Data API v3 video upload programmatic posting brand account 2026",
        "LinkedIn Pages API content publishing OAuth scopes requirements 2026",
        "Meta Graph API business content publishing app review requirements 2026",
        "Facebook pages API video post programmatic publishing requirements",
        "TikTok Content Posting API brand account limitations 2025 2026",
        "Instagram Graph API business publishing limitations content types 2026",
        "social media publishing API comparison B2B brand account 2026",
        "Buffer, Publer, Hootsuite API vs direct platform API publishing comparison",
        "LinkedIn Marketing Developer Program partner requirements publishing API",
        "Meta business login app review timeline content publishing approval",
        "Buffer API OAuth scopes supported platforms rate limits scheduling limits 2026",
        "Publer API publishing capabilities supported platforms batch scheduling rate limits 2026",
        "Buffer vs Publer third-party scheduling API programmatic access comparison 2026",
    ],
    "output_schema": {
        "capability_matrix": [
            {
                "platform": "string",
                "has_publishing_api": "boolean",
                "api_name": "string e.g. 'YouTube Data API v3'",
                "supported_content_types": ["video", "image", "text", "carousel"],
                "oauth_scopes_required": ["list of scope strings"],
                "app_review_required": "boolean",
                "estimated_approval_weeks": "integer or null if no review required",
                "partner_program_required": "boolean",
                "rate_limits": "string e.g. '100 posts/day'",
                "notes": "string — key caveats or limitations",
                "source_url": "string",
            }
        ],
        "scheduling_tools": [
            {
                "tool_name": "string e.g. 'Buffer'",
                "has_api": "boolean",
                "supported_platforms": ["list of platforms this tool can post to"],
                "oauth_scopes_required": ["list of scope strings"],
                "rate_limits": "string e.g. '150 posts/month on free tier'",
                "batching_supported": "boolean",
                "scheduling_supported": "boolean",
                "notes": "string — key caveats or plan-tier restrictions",
                "source_url": "string",
            }
        ],
        "recommended_api_first_platforms": [
            "platform names with usable APIs and no long review cycle"
        ],
        "long_lead_registrations": [
            "platform names requiring early app registration due to long review"
        ],
        "source_count": "integer",
        "confidence": "float 0.0-1.0",
    },
    "max_queries": 15,
    "max_depth": 3,
    "min_sources": 5,
    "confidence_threshold": 0.70,
}

_MANDATE_MAP: dict[str, dict[str, Any]] = {
    "MC1": _MC1_PAYLOAD,
    "MC2": _MC2_PAYLOAD,
    "MC3": _MC3_PAYLOAD,
}

# Scout subscribes to agent.nexus-prime.events (Nexus-Prime's outbound topic)
_TARGET_TOPIC = "agent.nexus-prime.events"


def _send_mandate(mandate_id: str, project_id: str, platforms: list[str] | None = None) -> None:
    """Publish a single RESEARCH_MANDATE A2AMessage to Scout's inbound Pub/Sub topic.

    Args:
        mandate_id:  One of "MC1", "MC2", "MC3".
        project_id:  GCP project ID for Pub/Sub and message routing.
        platforms:   Optional platform override for MC2's ``platforms_to_audit`` list.
    """
    from models import A2AMessage, MessageType
    from tools.pubsub import publish

    payload = dict(_MANDATE_MAP[mandate_id])  # shallow copy — do not mutate the original

    if mandate_id == "MC2" and platforms:
        payload = dict(payload)
        payload["platforms_to_audit"] = platforms
        # Rebuild seed queries with user-supplied platforms substituted for the
        # LinkedIn/YouTube defaults. Replace the compound token first so
        # individual token passes don't partially overlap it.
        platform_str = " ".join(platforms)
        primary = platforms[0]
        payload["seed_queries"] = [
            q.replace("LinkedIn YouTube", platform_str)
            .replace("YouTube LinkedIn", platform_str)
            .replace("LinkedIn", primary)
            .replace("YouTube", platform_str)
            for q in payload["seed_queries"]
        ]

    msg = A2AMessage(
        message_id=str(uuid.uuid4()),
        correlation_id=str(uuid.uuid4()),
        task_id=str(uuid.uuid4()),
        project_id=project_id,
        source_agent="nexus-prime",
        target_agent="scout",
        message_type=MessageType.RESEARCH_MANDATE,
        priority=3,
        payload=payload,
    )
    publish(_TARGET_TOPIC, msg)
    print(
        f"  ✓ {mandate_id} published → {_TARGET_TOPIC} "
        f"(task_id={msg.task_id}, domain={payload['research_domain']})"
    )


def main() -> None:
    """Parse args and send the requested Scout mandates."""
    parser = argparse.ArgumentParser(
        description="Send MC1/MC2/MC3 RESEARCH_MANDATE messages to Scout via Pub/Sub."
    )
    parser.add_argument(
        "--mandate",
        nargs="+",
        choices=["MC1", "MC2", "MC3"],
        default=["MC1", "MC2", "MC3"],
        metavar="MANDATE",
        help="Which mandates to send (default: all three). MC2 and MC3 run in parallel.",
    )
    parser.add_argument(
        "--platforms",
        nargs="+",
        default=None,
        metavar="PLATFORM",
        help=(
            "Override MC2 platforms_to_audit list. Use after reviewing MC1 results. "
            "Example: --platforms LinkedIn YouTube"
        ),
    )
    parser.add_argument(
        "--project",
        default=None,
        metavar="PROJECT_ID",
        help="GCP project ID. Defaults to settings.GCP_PROJECT_ID.",
    )
    args = parser.parse_args()

    from config import get_settings

    settings = get_settings()
    project_id: str = args.project or settings.GCP_PROJECT_ID

    mandates: list[str] = args.mandate
    platforms: list[str] | None = args.platforms

    # Warn if MC2/MC3 requested without MC1 and no --platforms override
    if "MC2" in mandates and "MC1" not in mandates and not platforms:
        print(
            "WARNING: Sending MC2 without MC1 results and without --platforms override.\n"
            "  MC2 will default to LinkedIn + YouTube. To use MC1 findings, run MC1 first,\n"
            "  review Research Products tab, then re-run with --mandate MC2 --platforms <list>."
        )

    print(f"\nSending Scout mandates to project '{project_id}' via {_TARGET_TOPIC}")
    print("-" * 60)

    for mandate_id in mandates:
        try:
            _send_mandate(
                mandate_id, project_id, platforms=platforms if mandate_id == "MC2" else None
            )
        except Exception as exc:
            print(f"  ✗ {mandate_id} FAILED: {exc}", file=sys.stderr)
            sys.exit(1)

    print("-" * 60)
    print(f"Done. {len(mandates)} mandate(s) sent.")
    if "MC1" in mandates:
        print(
            "\nNext steps:\n"
            "  1. Wait for Scout to process MC1 (check Research Products tab)\n"
            "  2. Review MC1 ranked_platforms output\n"
            "  3. Re-send MC2 with --platforms flag if needed:\n"
            "       python scripts/send_scout_mandates.py --mandate MC2 "
            "--platforms <platform1> <platform2>\n"
            "  4. After MC1+MC2 complete, review Agent_Approvals tab for HUMAN DECISION proposal"
        )
    if "MC3" in mandates:
        print(
            "\n  MC3 reminder: Review 'long_lead_registrations' in Research Products.\n"
            "  Start LinkedIn/Meta developer app registrations immediately if listed."
        )


if __name__ == "__main__":
    main()
