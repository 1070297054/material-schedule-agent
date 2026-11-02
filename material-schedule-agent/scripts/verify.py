#!/usr/bin/env python3
"""One command: executable tests + reproducible offline acceptance examples."""
import argparse
import json
import os
import platform
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from schedule_agent.cli import write_json
from evaluate import evaluate


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default=str(ROOT / "examples"))
    args = parser.parse_args()
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
    command = [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"]
    proc = subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
    log = proc.stdout + proc.stderr
    (output / "test-results.txt").write_text(log, encoding="utf-8")
    print(log, end="")
    summary = evaluate(output / "mock")
    evidence = {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "python": platform.python_version(), "platform": platform.platform(),
                "test_command": "PYTHONPATH=src python3 -m unittest discover -s tests -v",
                "tests_exit_code": proc.returncode, "test_log": "test-results.txt",
                "mock_cases_total": summary["cases_total"], "mock_cases_passed": summary["cases_passed"],
                "real_model_validation": "not_run", "real_model_reason": "此命令仅执行离线测试；真实模型请运行 scripts/evaluate.py --provider openai"}
    write_json(output / "verification.json", evidence)
    print(json.dumps(evidence, ensure_ascii=False, indent=2))
    return 0 if proc.returncode == 0 and summary["cases_passed"] == summary["cases_total"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
