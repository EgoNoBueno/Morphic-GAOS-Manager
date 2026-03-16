# GAOS Project Glossary

This glossary provides definitions and explanations for terms and acronyms used in the Morphic-GAOS-Manager project. It includes references to the GAOS system and Google's platform services.

## A
- **AOS (Agent Operating System)**: The intelligent workforce system built on Google's cloud ecosystem. It coordinates specialized AI agents to handle business operations autonomously.

## B
- **BigQuery**: A Google Cloud service used for analyzing large datasets. In the GAOS system, it is used for storing and querying recent history for pattern recognition.

## C
- **Cloud Pub/Sub**: A Google Cloud messaging service used for communication between agents. Ensures reliable message delivery even during restarts or crashes.
- **Cloud Run**: A Google Cloud service for running containerized applications. Used in the GAOS system for scalable and cost-effective deployment.

## G
- **Google Apps Script**: A scripting platform for automating tasks across Google Workspace. Used in the GAOS system for triggers and approval workflows.
- **Google Drive**: A cloud storage service used for storing procedural knowledge in the GAOS system.
- **Google Sheets**: A spreadsheet application used as the operational dashboard for the GAOS system. It serves as the control plane for live agent status, approval queues, and task logs.
- **Google Workspace**: A suite of productivity tools including Gmail, Drive, and Sheets. The GAOS system leverages free-tier services from Google Workspace.

## M
- **Memory Bank**: A layered memory system in the GAOS architecture. Includes fast scratchpads, BigQuery for recent history, and long-term storage in Vertex AI Memory Bank.

## N
- **Nexus-Prime**: The general manager agent in the GAOS system. Oversees the entire system, routes jobs, and authorizes operational changes.

## V
- **Vertex AI**: A Google Cloud service for building and deploying machine learning models. Used in the GAOS system for long-term memory storage and knowledge promotion.

## Google Tool Stack

The following Google tools are utilized in the GAOS project:

- **Google Sheets**: Serves as the operational dashboard for the system. It provides live agent status, approval queues, task logs, and business data in a user-friendly interface.
- **Google Drive**: Used for storing procedural knowledge in a structured folder hierarchy. It ensures version control and accessibility for all agents.
- **Google Apps Script**: Automates workflows and integrates with Google Workspace tools. It is used for triggers, approval workflows, and other automation tasks.
- **Cloud Pub/Sub**: A messaging service that facilitates communication between agents. It ensures reliable message delivery and decouples agent interactions.
- **Cloud Run**: Hosts containerized applications, providing scalable and cost-effective deployment for the GAOS system.
- **BigQuery**: Analyzes large datasets for pattern recognition and recent history storage. It supports the memory architecture of the system.
- **Vertex AI**: Manages long-term memory storage and knowledge promotion. It is a key component of the system's learning and evolution capabilities.

## Additional Notes
- The GAOS system is designed to minimize costs by leveraging free-tier Google services and routing routine tasks to local AI models.
- Communication between agents is decoupled using Cloud Pub/Sub, ensuring resilience and scalability.

---

This glossary will be updated as new terms and acronyms are introduced in the project.