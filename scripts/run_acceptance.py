#!/usr/bin/env python3
"""
Локальный acceptance-runner для проекта legal-audit-tool.

Запуск:
    .venv/bin/python scripts/run_acceptance.py
    .venv/bin/python scripts/run_acceptance.py --full

По умолчанию выполняет быстрые проверки без внешней сети, LLM и браузерных
бинарников. Флаг --full дополнительно гоняет весь pytest-набор.
"""
from __future__ import annotations

import argparse
import importlib
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


SMOKE_MODULES = [
    "app",
    "auth",
    "config.settings",
    "scanner.orchestrator",
    "scanner.browser",
    "scanner.crawler",
    "scanner.document_finder",
    "scanner.document_fetcher",
    "scanner.form_detector",
    "legal.rule_engine",
    "legal.document_analyzer",
    "llm.llm_client",
    "reports.html_renderer",
    "reports.pdf_generator",
    "database.db",
    "services.jobs",
]


YAML_FILES = [
    "data/legal_rules.yml",
    "data/tracker_domains.yml",
    "data/document_checklists.yml",
    "data/service_packages.yml",
    "data/liability.yml",
]


def _run(cmd):
    print("\n$ " + " ".join(cmd), flush=True)
    completed = subprocess.run(cmd, cwd=str(ROOT))
    return completed.returncode


def _check_imports() -> int:
    print("\n== Import smoke ==")
    failed = []
    for name in SMOKE_MODULES:
        try:
            importlib.import_module(name)
            print("OK  " + name)
        except Exception as exc:  # pragma: no cover - diagnostic path
            failed.append((name, exc))
            print("ERR {}: {}".format(name, exc))
    return 1 if failed else 0


def _check_yaml() -> int:
    print("\n== YAML validation ==")
    try:
        import yaml
    except Exception as exc:
        print("ERR PyYAML unavailable: {}".format(exc))
        return 1

    failed = []
    for rel in YAML_FILES:
        path = ROOT / rel
        try:
            with path.open("r", encoding="utf-8") as fh:
                yaml.safe_load(fh)
            print("OK  " + rel)
        except Exception as exc:
            failed.append((rel, exc))
            print("ERR {}: {}".format(rel, exc))
    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--full",
        action="store_true",
        help="Run the complete pytest suite after the fast acceptance subset.",
    )
    args = parser.parse_args()

    rc = 0
    rc |= _check_imports()
    rc |= _check_yaml()
    rc |= _run(
        [
            sys.executable,
            "-m",
            "pytest",
            "tests/test_e2e_scan_fixtures.py",
            "tests/test_pdf_quality_acceptance.py",
            "-q",
        ]
    )
    if args.full:
        rc |= _run([sys.executable, "-m", "pytest", "tests/", "-q"])

    print("\n== Result ==")
    if rc == 0:
        print("PASS: acceptance-проверки прошли.")
    else:
        print("FAIL: есть ошибки, см. вывод выше.")
    return 0 if rc == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
