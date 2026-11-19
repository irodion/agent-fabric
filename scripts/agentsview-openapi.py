# /// script
# requires-python = ">=3.13"
# dependencies = ["pyyaml==6.0.2"]
# [tool.uv]
# exclude-newer = "2026-10-17T00:00:00Z"
# ///
"""Cut agentsview's OpenAPI spec at the pinned commit down to what the adapter reads (docs/design.md §4.1).

Writes tests/fixtures/agentsview/openapi.json: the adapter's endpoints, their query parameters, and every schema their
responses reach. The contract tests validate the recorded responses against it, and fail when vendor/agentsview.pin
names another commit, so a pin bump regenerates it: `uv run scripts/agentsview-openapi.py`.
"""

import json
import sys
import urllib.request
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
PIN = ROOT / "vendor" / "agentsview.pin"
OUT = ROOT / "tests" / "fixtures" / "agentsview" / "openapi.json"
ENDPOINTS = ["/api/v1/sessions", "/api/v1/sessions/{id}", "/api/v1/sessions/{id}/messages"]


def pin() -> dict[str, str]:
    lines = (line.split("=", 1) for line in PIN.read_text().splitlines() if line and not line.startswith("#"))
    return {key: value for key, value in lines}


def refs(node: object) -> set[str]:
    """Names of the component schemas that node points at."""
    if isinstance(node, dict):
        found = {node["$ref"].rsplit("/", 1)[1]} if isinstance(node.get("$ref"), str) else set()
        return found.union(*(refs(value) for value in node.values()))
    if isinstance(node, list):
        return set().union(*(refs(item) for item in node))
    return set()


def main() -> int:
    p = pin()
    url = f"https://raw.githubusercontent.com/kenn-io/agentsview/{p['commit']}/openapi.yaml"
    with urllib.request.urlopen(url, timeout=60) as response:
        spec = yaml.safe_load(response)
    schemas = spec["components"]["schemas"]
    paths = {}
    for path in ENDPOINTS:
        get = spec["paths"][path]["get"]
        paths[path] = {
            "parameters": sorted(param["name"] for param in get.get("parameters", []) if param["in"] == "query"),
            "response": get["responses"]["200"]["content"]["application/json"]["schema"],
        }
    wanted, todo = set(), refs(paths)
    while todo:
        name = todo.pop()
        wanted.add(name)
        todo |= refs(schemas[name]) - wanted
    out = {
        "tag": p["tag"],
        "commit": p["commit"],
        "paths": paths,
        "components": {"schemas": {name: schemas[name] for name in sorted(wanted)}},
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, indent=1, sort_keys=True) + "\n")
    print(f"{OUT.relative_to(ROOT)}: {len(paths)} endpoints, {len(wanted)} schemas, agentsview {p['tag']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
