"""
verify_summaries.py — 验证子 agent 摘要输出是否完整有效。

读取 digest_raw.json 获取预期条目数，检查 /tmp/dtn_summaries/{id}.json 是否存在且有效。
输出验证报告：哪些通过、哪些缺失、哪些无效。
"""
import json
import os
import sys

SUMMARY_DIR = "/tmp/dtn_summaries"
DIGEST_PATH = "/tmp/dtn_digest_raw.json"


def verify():
    with open(DIGEST_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)

    entries = data.get("entries", [])
    total = len(entries)

    if total == 0:
        print("verify: 0 entries in digest — nothing to verify.")
        return "skip"

    passed = []
    missing = []
    invalid = []

    for entry in entries:
        eid = entry["id"]
        summary_path = os.path.join(SUMMARY_DIR, f"{eid}.json")

        if not os.path.exists(summary_path):
            missing.append(eid)
            continue

        try:
            with open(summary_path, "r", encoding="utf-8") as f:
                summary = json.load(f)

            if not summary.get("summary") or not summary.get("angle"):
                invalid.append((eid, "missing summary or angle"))
            else:
                passed.append(eid)

        except json.JSONDecodeError as e:
            invalid.append((eid, f"invalid JSON: {e}"))
        except Exception as e:
            invalid.append((eid, f"error: {e}"))

    print(f"verify: {total} entries total")
    print(f"  ✓ passed:  {len(passed)}")

    if missing:
        print(f"  ✗ missing:  {len(missing)} → entry ids: {missing}")

    if invalid:
        print(f"  ✗ invalid:  {len(invalid)}")
        for eid, reason in invalid:
            print(f"    id={eid}: {reason}")

    if not missing and not invalid:
        print("verify: ALL OK — all summaries valid.")
        return "ok"

    return "needs_retry"


if __name__ == "__main__":
    result = verify()
    print(f"\nResult: {result}")
    sys.exit(0 if result == "ok" else 1)
