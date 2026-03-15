# agents/ — Tier 1 and Tier 2 agent implementations for Morphic-G AOS.
#
# Structure:
#   agents/
#     nexus_prime/
#       orchestrator.py   — Tier 1 root agent (public repo)
#       tasks/            — gitignored; add business-specific task agents here
#     ledger/
#       orchestrator.py   — Tier 2 accounting orchestrator (public repo)
#       tasks/            — gitignored
#     beacon/             — marketing
#     pursuit/            — sales
#     foreman/            — operations
#     steward/            — admin & HR
#     scout/              — research
#
# See Docs/GAOS-Agent-Spec.md for construction requirements.
