"""Temporary E2E test for vision_blueprint node — delete after validation."""

import uuid

from agents.nexus_prime.orchestrator import vision_blueprint
from models import A2AMessage, MessageType
from tools.google_sheets import init_sheets_client

task_id = str(uuid.uuid4())
project_id = "morphic-gaos-prod"

msg = A2AMessage(
    task_id=task_id,
    project_id=project_id,
    source_agent="owner",
    target_agent="nexus-prime",
    message_type=MessageType.VISION_SUBMITTED,
    priority=3,
    payload={
        "vision_text": (
            "Build a fully automated invoice approval workflow that reads invoices from email, "
            "validates against purchase orders in our system, and routes exceptions to a human "
            "reviewer with a 24-hour SLA."
        ),
        "submitted_by": "dhess@sl10repairtechs.com",
        "space_name": "spaces/jbpdpSAAAAE",
    },
)

init_sheets_client(project_id)

state = {
    "task_id": task_id,
    "project_id": project_id,
    "current_objective": "Process VISION_SUBMITTED",
    "sub_task_results": [],
    "parked_proposals": [],
    "error_history": [],
    "memory_context": {},
    "episodic_cache": {},
    "observation_buffer": [],
    "cost_usd": 0.0,
    "iteration_count": 0,
    "step_count": 0,
    "tokens_used": 0,
    "incoming_message": msg,
    "messages": [],
    "hard_stop_triggered": False,
    "evolution_triggered": False,
    "active_blueprints": {},
    "blueprint_constraints": [],
}

print(f"Running vision_blueprint | task_id={task_id}")
result = vision_blueprint(state)

blueprints = result.get("active_blueprints", {})
cost = result.get("cost_usd", 0.0)
tokens = result.get("tokens_used", 0)

print(f"active_blueprints: {blueprints}")
print(f"cost_usd: {cost}")
print(f"tokens_used: {tokens}")
print("DONE - check Google Docs, Project_Incubator tab, and Chat space")
