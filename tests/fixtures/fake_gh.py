#!/usr/bin/env python3
"""A fake `gh api -i` driven by a JSON scenario, for the forge adapter tests.

FAKE_GH_SCENARIO names the scenario file:
    {"rest": {"<endpoint>": [status, body]},
     "graphql": {"PrFacts": [body, ...],                 # consumed in order, last one repeats
                 "Checks": {"": page, "<cursor>": page},  # keyed by the `after` variable
                 "Threads": {"": page}}}
Every call is appended to FAKE_GH_LOG when it is set. It never touches the network.
"""

import json
import os
import re
import sys
from pathlib import Path


def respond(status: int, body: object) -> int:
    sys.stdout.write(f"HTTP/2.0 {status} Fake\nContent-Type: application/json\n\n{json.dumps(body)}")
    return 0 if status < 400 else 1


def main() -> int:
    args = sys.argv[1:]
    scenario_path = Path(os.environ["FAKE_GH_SCENARIO"])
    scenario = json.loads(scenario_path.read_text())
    if os.environ.get("FAKE_GH_LOG"):
        with open(os.environ["FAKE_GH_LOG"], "a") as log:
            log.write(json.dumps(args) + "\n")
    if args[:2] != ["api", "-i"]:
        return respond(400, {"message": f"fake gh supports only `api -i`, got {args[:2]}"})
    target = args[2]
    if target != "graphql":
        status, body = scenario.get("rest", {}).get(target, [404, {"message": "Not Found"}])
        return respond(status, body)
    fields = dict(a.split("=", 1) for a in args[3:] if "=" in a and not a.startswith("-"))
    operation = re.search(r"query (\w+)", fields.get("query", "")).group(1)
    pages = scenario.get("graphql", {}).get(operation)
    if isinstance(pages, list):
        counter = scenario_path.with_suffix(f".{operation}.count")
        n = int(counter.read_text()) if counter.exists() else 0
        counter.write_text(str(n + 1))
        return respond(200, {"data": pages[min(n, len(pages) - 1)]})
    if isinstance(pages, dict):
        page = pages.get(fields.get("after", ""))
        if page is None:
            return respond(200, {"errors": [{"message": f"no page for cursor {fields.get('after')!r}"}]})
        return respond(200, {"data": page})
    return respond(200, {"errors": [{"message": f"no scenario for {operation}"}]})


if __name__ == "__main__":
    sys.exit(main())
