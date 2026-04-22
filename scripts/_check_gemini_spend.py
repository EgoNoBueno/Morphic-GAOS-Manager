#!/usr/bin/env python3
"""Check Gemini spend breakdown from api_call_log and Cloud Logging."""

from google.cloud import bigquery

PROJECT = "morphic-gaos-prod"


def main() -> None:
    client = bigquery.Client(project=PROJECT)

    # Schema: ts, api_name, operation, caller, project_id, success, latency_ms,
    #         error_code, attempts, tokens_used, model

    print("=== Token usage by day + caller + model (last 30 days) ===")
    q1 = """
    SELECT
      DATE(ts) AS day,
      caller,
      model,
      SUM(tokens_used) AS total_tokens,
      COUNT(*) AS calls,
      COUNTIF(NOT success) AS failures
    FROM `morphic-gaos-prod.aos_logs.api_call_log`
    WHERE ts >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY)
      AND model IS NOT NULL
    GROUP BY day, caller, model
    ORDER BY day DESC, total_tokens DESC
    LIMIT 100
    """
    try:
        rows = list(client.query(q1).result())
        if not rows:
            print("  No model call data in api_call_log")
        else:
            grand_tokens = 0
            for r in rows:
                tokens = r.total_tokens or 0
                grand_tokens += tokens
                # Rough Gemini Flash cost: $0.075/1M input + $0.30/1M output ~ $0.15/1M blended
                est_cost = tokens / 1_000_000 * 0.15
                print(
                    f"  {r.day}  {r.caller or '?':20s}  {r.model:30s}  "
                    f"tokens={tokens:>8,}  calls={r.calls:>5}  fail={r.failures}  ~${est_cost:.3f}"
                )
            print(
                f"\n  TOTAL TOKENS (30d): {grand_tokens:,}  ~${grand_tokens / 1_000_000 * 0.15:.2f} (rough estimate)"
            )
    except Exception as exc:
        print(f"  Query q1 failed: {exc}")

    print()
    print("=== Top callers by total tokens ===")
    q2 = """
    SELECT
      caller,
      model,
      SUM(tokens_used) AS total_tokens,
      COUNT(*) AS calls
    FROM `morphic-gaos-prod.aos_logs.api_call_log`
    WHERE ts >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY)
      AND model IS NOT NULL
    GROUP BY caller, model
    ORDER BY total_tokens DESC
    LIMIT 20
    """
    try:
        rows2 = list(client.query(q2).result())
        if rows2:
            for r in rows2:
                tokens = r.total_tokens or 0
                print(
                    f"  {r.caller or '?':20s}  {r.model:30s}  tokens={tokens:>10,}  calls={r.calls}"
                )
        else:
            print("  No recent LLM usage found in api_call_log.")
    except Exception as exc:
        print(f"  Query q2 failed: {exc}")

    print()
    print("=== task_outcomes cost breakdown ===")
    q3 = """
    SELECT column_name, data_type
    FROM `morphic-gaos-prod.aos_logs.INFORMATION_SCHEMA.COLUMNS`
    WHERE table_name = 'task_outcomes'
    ORDER BY ordinal_position
    """
    try:
        schema_rows = list(client.query(q3).result())
        print("  task_outcomes columns:", [r.column_name for r in schema_rows])
        # Find cost column if any
        cost_col = next(
            (r.column_name for r in schema_rows if "cost" in r.column_name.lower()), None
        )
        if cost_col:
            # Whitelist cost_col to ensure f-string interpolation is safe for this read-only script
            allowed_cols = {"cost_usd", "total_cost", "spend"}
            safe_col = cost_col if cost_col in allowed_cols else "cost_usd"

            q4 = f"""
            SELECT DATE(created_at) AS day, agent_id, task_type,
                   SUM({safe_col}) AS total_cost, SUM(tokens_used) AS total_tokens,
                   COUNT(*) AS tasks
            FROM `morphic-gaos-prod.aos_logs.task_outcomes`
            WHERE created_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY)
            GROUP BY day, agent_id, task_type
            ORDER BY day DESC, total_cost DESC
            LIMIT 50
            """
            rows4 = list(client.query(q4).result())
            for r in rows4:
                print(
                    f"  {r.day}  {r.agent_id}  {r.task_type}  "
                    f"cost=${r.total_cost:.4f}  tokens={r.total_tokens or 0}  tasks={r.tasks}"
                )
        else:
            print("  No cost column in task_outcomes")
    except Exception as exc:
        print(f"  task_outcomes query failed: {exc}")


if __name__ == "__main__":
    main()
