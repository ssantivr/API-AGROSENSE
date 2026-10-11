"""Checks that openapi.json is consistent with itself. Standard library only, no backend needed.

    python tools/validate_spec.py

It does not replace check_contract.py, which compares the contract with a running backend; this one
catches the mistakes made while editing the file: a reference to a schema that does not exist, a path
parameter nobody declared, a protected route without its 401, a required field missing from its schema.
"""

import json
import re
import sys
from pathlib import Path

SPEC_PATH = Path(__file__).resolve().parent.parent / "openapi.json"
METHODS = ("get", "post", "put", "patch", "delete")


def problems_in(spec: dict) -> list[str]:
    """Everything wrong with the contract; empty when it is consistent."""
    found = []

    def resolve(reference: str):
        node = spec
        for part in reference.removeprefix("#/").split("/"):
            if not isinstance(node, dict) or part not in node:
                return None
            node = node[part]
        return node

    def walk(node, where: str) -> None:
        if isinstance(node, dict):
            reference = node.get("$ref")
            if reference is not None and (not isinstance(reference, str) or not reference.startswith("#/")
                                          or resolve(reference) is None):
                found.append(f"{where}: {reference!r} points to nothing")
            if node.get("type") == "object" and "properties" in node:
                for name in node.get("required", []):
                    if name not in node["properties"]:
                        found.append(f"{where}: required field '{name}' is not among its properties")
            for key, child in node.items():
                walk(child, f"{where}/{key}")
        elif isinstance(node, list):
            for index, child in enumerate(node):
                walk(child, f"{where}/{index}")

    walk(spec, "#")

    known_tags = {tag["name"] for tag in spec.get("tags", [])}
    default_security = spec.get("security", [])
    operation_ids = {}
    for path, item in spec.get("paths", {}).items():
        if not path.startswith("/"):
            found.append(f"{path}: a path starts with /")
        for method in METHODS:
            if method not in item:
                continue
            operation = item[method]
            name = f"{method.upper()} {path}"
            operation_id = operation.get("operationId")
            if not operation_id:
                found.append(f"{name}: has no operationId")
            elif operation_id in operation_ids:
                found.append(f"{name}: operationId '{operation_id}' is also used by {operation_ids[operation_id]}")
            else:
                operation_ids[operation_id] = name
            if not operation.get("summary"):
                found.append(f"{name}: has no summary")
            for tag in operation.get("tags") or [None]:
                if tag not in known_tags:
                    found.append(f"{name}: tag {tag!r} is not declared in the list of tags")

            parameters = []
            for parameter in operation.get("parameters", []):
                target = resolve(parameter["$ref"]) if "$ref" in parameter else parameter
                if target:
                    parameters.append(target)
            declared = {parameter["name"] for parameter in parameters if parameter.get("in") == "path"}
            in_path = set(re.findall(r"\{([^}]+)\}", path))
            for missing in sorted(in_path - declared):
                found.append(f"{name}: path parameter '{missing}' is not declared")
            for extra in sorted(declared - in_path):
                found.append(f"{name}: declares path parameter '{extra}', which the path does not have")
            for parameter in parameters:
                if parameter.get("in") == "path" and not parameter.get("required"):
                    found.append(f"{name}: path parameter '{parameter['name']}' must be required")

            responses = operation.get("responses", {})
            if not any(code.startswith("2") for code in responses):
                found.append(f"{name}: documents no successful answer")
            protected = operation.get("security", default_security) != []
            if protected and "401" not in responses:
                found.append(f"{name}: needs a token but does not document the 401")
            if not protected and "401" in responses and not path.endswith("/login"):
                found.append(f"{name}: is public but documents a 401")
            if in_path and "404" not in responses:
                found.append(f"{name}: takes an identifier but does not document the 404")
            if "requestBody" in operation and "400" not in responses:
                found.append(f"{name}: takes a body but does not document the 400")
            for code, response in responses.items():
                target = resolve(response["$ref"]) if "$ref" in response else response
                if not target:
                    continue
                if not target.get("description"):
                    found.append(f"{name}: answer {code} has no description")
                if code.startswith(("4", "5")) and "content" not in target:
                    found.append(f"{name}: error {code} does not say what its body looks like")
    return found


def main() -> int:
    spec = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
    found = problems_in(spec)
    for problem in found:
        print(f"FAIL {problem}")
    operations = sum(1 for item in spec["paths"].values() for method in METHODS if method in item)
    print(f"{SPEC_PATH.name} {spec['info']['version']}: {len(spec['paths'])} paths, {operations} operations, "
          f"{len(spec['components']['schemas'])} schemas - "
          + ("consistent" if not found else f"{len(found)} problem(s)"))
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
