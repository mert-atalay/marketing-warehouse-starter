"""Read a small, plain subset of Dataform .sqlx and turn it into SQL DuckDB can run.

Supported: one `config { ... }` block per file, `${ref("name")}`,
`${ref("schema", "name")}`, `${ref({schema: "...", name: "..."})}` and
`${dataform.projectConfig.vars.some_var}`.

Not supported: `js { }` blocks, includes, pre and post operations, incremental
models and any other JavaScript. A file that uses them fails loudly instead of
running something different from what Dataform would run.
"""

import json
import re
from dataclasses import dataclass, field
from graphlib import TopologicalSorter
from pathlib import Path

import yaml

_STRING = r'"(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\''
_REF = re.compile(r"\$\{\s*ref\(\s*(.*?)\s*\)\s*\}", re.DOTALL)
_VAR = re.compile(r"\$\{\s*dataform\.projectConfig\.vars\.(\w+)\s*\}")
_UNSUPPORTED_BLOCK = re.compile(r"^\s*(js|pre_operations|post_operations|incremental_where)\s*\{", re.MULTILINE)


@dataclass
class Action:
    """One .sqlx file: a source declaration, a model or an assertion."""

    name: str
    schema: str
    type: str  # declaration, view, table or assertion
    path: Path
    config: dict
    body: str  # SQL with the config block removed, refs not yet resolved
    depends_on: list = field(default_factory=list)  # Actions this one refs
    sql: str = ""  # runnable SQL

    @property
    def relation(self) -> str:
        return f"{self.schema}.{self.name}"


def js_object_to_dict(text: str) -> dict:
    """Parse a JavaScript object literal that only holds plain data."""
    parts = re.split(f"({_STRING})", text)
    for i, part in enumerate(parts):
        if i % 2:  # a string literal
            if part.startswith("'"):
                parts[i] = json.dumps(part[1:-1].replace("\\'", "'"))
        else:
            part = re.sub(r"//[^\n]*", "", part)  # line comments
            part = re.sub(r"([A-Za-z_]\w*)\s*:", r'"\1":', part)  # quote keys
            parts[i] = re.sub(r",(\s*[}\]])", r"\1", part)  # trailing commas
    return json.loads("".join(parts))


def split_config(text: str) -> tuple[dict, str]:
    """Return (config dict, SQL body) for the text of one .sqlx file."""
    match = re.match(r"\s*config\s*\{", text)
    if not match:
        return {}, text.strip()
    start = match.end() - 1
    depth, i = 0, start
    while i < len(text):
        string = re.compile(_STRING).match(text, i)
        if string:
            i = string.end()
            continue
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                break
        i += 1
    else:
        raise ValueError("config block is never closed")
    return js_object_to_dict(text[start : i + 1]), text[i + 1 :].strip()


def load_settings(project_dir: Path) -> dict:
    return yaml.safe_load((Path(project_dir) / "workflow_settings.yaml").read_text())


def _parse_ref(argument: str) -> tuple[str | None, str]:
    """Return (schema or None, name) for the text inside ref(...)."""
    if argument.startswith("{"):
        target = js_object_to_dict(argument)
        return target.get("schema") or target.get("dataset"), target["name"]
    names = [json.loads(js_string) for js_string in _to_json_strings(argument)]
    if len(names) == 1:
        return None, names[0]
    if len(names) == 2:
        return names[0], names[1]
    raise ValueError(f"cannot read ref({argument})")


def _to_json_strings(argument: str) -> list[str]:
    return [s if s.startswith('"') else json.dumps(s[1:-1]) for s in re.findall(_STRING, argument)]


def load_project(project_dir: Path) -> list[Action]:
    """Read every .sqlx file under definitions/ and return Actions in run order."""
    project_dir = Path(project_dir)
    settings = load_settings(project_dir)
    variables = settings.get("vars") or {}

    actions = []
    for path in sorted((project_dir / "definitions").rglob("*.sqlx")):
        text = path.read_text()
        if _UNSUPPORTED_BLOCK.search(text):
            raise ValueError(f"{path}: this runner does not support js or operation blocks")
        config, body = split_config(text)
        action_type = config.get("type")
        if action_type not in ("declaration", "view", "table", "assertion"):
            raise ValueError(f"{path}: unsupported type {action_type!r}")
        if "assertions" in config:
            raise ValueError(f"{path}: built-in `assertions:` config is not supported, write an assertion file")
        default_schema = settings["defaultAssertionDataset"] if action_type == "assertion" else settings["defaultDataset"]
        actions.append(
            Action(
                name=config.get("name", path.stem),
                schema=config.get("schema", default_schema),
                type=action_type,
                path=path,
                config=config,
                body=body.rstrip(";").strip(),
            )
        )

    def find(schema: str | None, name: str, where: Path) -> Action:
        found = [a for a in actions if a.name == name and schema in (None, a.schema)]
        if len(found) != 1:
            raise ValueError(f"{where}: ref to {name!r} matches {len(found)} actions")
        return found[0]

    for action in actions:

        def resolve(match: re.Match) -> str:
            target = find(*_parse_ref(match.group(1)), action.path)
            if target not in action.depends_on:
                action.depends_on.append(target)
            return target.relation

        def variable(match: re.Match) -> str:
            if match.group(1) not in variables:
                raise ValueError(f"{action.path}: var {match.group(1)!r} is not in workflow_settings.yaml")
            return str(variables[match.group(1)])

        action.sql = _VAR.sub(variable, _REF.sub(resolve, action.body))
        if "${" in action.sql:
            raise ValueError(f"{action.path}: unsupported ${{...}} expression")

    by_relation = {a.relation: a for a in actions}
    if len(by_relation) != len(actions):
        raise ValueError("two actions share the same schema and name")
    order = TopologicalSorter({a.relation: [d.relation for d in a.depends_on] for a in actions}).static_order()
    return [by_relation[relation] for relation in order]
