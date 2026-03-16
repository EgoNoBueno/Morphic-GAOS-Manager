"""Temporary script — create Vertex AI RAG corpora for all 7 domains."""
import vertexai
from vertexai import rag
import yaml
from pathlib import Path

SETTINGS = Path(__file__).parent.parent / "config" / "settings.yaml"

vertexai.init(project="morphic-gaos-prod", location="us-west1")

domains = ["global", "accounting", "marketing", "sales", "operations", "admin", "research"]
results = {}
for domain in domains:
    try:
        corpus = rag.create_corpus(
            display_name=f"gaos-{domain}",
            description=f"Morphic-G AOS semantic memory — {domain} domain",
        )
        results[domain] = corpus.name
        print(f"  {domain}: {corpus.name}")
    except Exception as e:
        print(f"  {domain}: ERROR — {e}")

# Update settings.yaml
with open(SETTINGS) as f:
    settings = yaml.safe_load(f)

settings.setdefault("memory_bank", {})["corpora"] = results
with open(SETTINGS, "w") as f:
    yaml.dump(settings, f, default_flow_style=False, allow_unicode=True)

print("\nsettings.yaml updated with corpus resource names.")
