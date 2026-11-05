"""Smoke test RAG and the journal against the real local Ollama model.

Run from the repository root with:

    .venv/bin/python scripts/smoke_rag.py

The script creates a temporary MAGPIE_HOME, inserts five realistic captures
from this morning, then asks "What did Priya ask me this morning?" and writes
today's journal.  It prints the answer, its citations and the journal markdown.
"""

from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from magpie import config, db, journal, llm, rag


def morning_time(hour: int, minute: int, now: datetime) -> float:
    return now.replace(hour=hour, minute=minute, second=0, microsecond=0).timestamp()


def seed_captures(database: db.Database, now: datetime) -> list[dict]:
    rows = [
        (
            morning_time(8, 47, now),
            "Slack",
            "com.tinyspeck.slackmacgap",
            "#design - Priya Singh",
            "Priya: Can you send the updated contract to legal by noon? "
            "Also confirm the offsite date before the team sync.",
        ),
        (
            morning_time(9, 5, now),
            "Gmail",
            "com.google.Chrome",
            "Contract review - Priya Patel",
            "Hi, could you review the redlines before our 11am call? "
            "I left two comments about the payment terms. Thanks, Priya.",
        ),
        (
            morning_time(9, 38, now),
            "VS Code",
            "com.microsoft.VSCode",
            "contract.py - magpie",
            "def export_contract(account):\n"
            "    payload = build_payload(account)\n"
            "    return send_to_legal(payload)\n"
            "# TODO: add the signed date before Friday",
        ),
        (
            morning_time(10, 4, now),
            "Safari",
            "com.apple.Safari",
            "Q4 roadmap - Notion",
            "Q4 roadmap: contract automation is scheduled for Friday. "
            "Priya owns the legal review; Sam owns the customer rollout.",
        ),
        (
            morning_time(10, 26, now),
            "Slack",
            "com.tinyspeck.slackmacgap",
            "#design - Priya Singh",
            "Priya: Thanks! Legal just approved the contract. "
            "Let's keep the Friday deadline and review the rollout notes tomorrow.",
        ),
    ]
    inserted = []
    for ts, app, bundle, title, text in rows:
        capture_id = database.insert_capture(ts, app, bundle, title, text)
        row = database.get_capture(capture_id)
        if row is not None:
            inserted.append(row)
    return inserted


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="magpie-smoke-") as home:
        os.environ["MAGPIE_HOME"] = home
        config.ensure_dirs()
        cfg = config.defaults()
        cfg["provider"] = "ollama"
        cfg["providers"]["ollama"]["base_url"] = "http://localhost:11434"
        cfg["providers"]["ollama"]["model"] = "llama3.2:1b"
        config.save_config(cfg)

        now = datetime.now()
        database = db.Database(config.db_path())
        try:
            seeded = seed_captures(database, now)
            today = now.strftime("%Y-%m-%d")

            print(f"MAGPIE_HOME: {home}")
            print(f"Today: {today}")
            print(f"Inserted {len(seeded)} captures:")
            for row in seeded:
                stamp = datetime.fromtimestamp(row["ts"]).strftime("%H:%M")
                print(f"  {stamp}  {row['app']:<8} {row['window_title']}")

            provider = llm.get_provider(cfg, timeout=180.0)
            try:
                question = "What did Priya ask me this morning?"
                print(f"\n== RAG answer ==\nQuestion: {question}")
                result = rag.answer(
                    question, [], cfg, database, provider=provider, now=now.timestamp()
                )
                print(result["answer"])

                print("\nCitations:")
                for index, citation in enumerate(result["citations"], start=1):
                    stamp = datetime.fromtimestamp(citation["ts"]).strftime("%H:%M")
                    print(
                        f"  [{index}] {stamp} {citation['app']} - "
                        f"{citation['window_title']} (capture {citation['id']})"
                    )

                print("\n== Journal ==")
                generated = journal.generate_journal(
                    today,
                    cfg,
                    database,
                    provider=provider,
                    regenerate=True,
                    now=now.timestamp(),
                )
                print(generated["summary"])
                print(
                    f"\n(cached={generated['cached']}, model={generated['model']}, "
                    f"day={generated['day']})"
                )
            finally:
                provider.close()
        finally:
            database.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
