"""Runtime schema registry for event-type metadata validation."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path


DEFAULT_SCHEMAS: dict[str, dict] = {
    "note.text": {"required": [], "properties": {}},
    "task.update": {
        "required": ["job_id"],
        "properties": {"job_id": {"type": "string"}},
    },
    "artifact.created": {
        "required": ["artifact_type", "artifact_id"],
        "properties": {
            "artifact_type": {"type": "string"},
            "artifact_id": {"type": "string"},
        },
    },
    "run.result": {
        "required": ["run_id", "status"],
        "properties": {
            "run_id": {"type": "string"},
            "status": {"type": "string", "enum": ["ok", "error", "cancelled"]},
        },
    },
    "task.*": {
        "required": ["job_id"],
        "properties": {"job_id": {"type": "string"}},
    },
}


class SchemaValidationError(ValueError):
    pass


class SchemaRegistry:
    def __init__(self, path: str | Path | None = None):
        configured = os.environ.get("AGENTBRIDGE_SCHEMA_FILE")
        self.path = Path(path or configured or (Path.home() / ".agentbridge" / "event_schemas.json")).expanduser()
        self._lock = threading.RLock()
        self._schemas: dict[str, dict] = {}
        self._resolved_path: Path | None = None
        self.reload()

    def _candidate_paths(self) -> list[Path]:
        configured = os.environ.get("AGENTBRIDGE_SCHEMA_FILE")
        candidates = []
        if configured:
            candidates.append(Path(configured).expanduser())
        candidates.extend([
            self.path,
            Path.cwd() / ".agentbridge" / "event_schemas.json",
            Path("/tmp") / "agentbridge-event-schemas.json",
        ])
        unique: list[Path] = []
        seen = set()
        for p in candidates:
            s = str(p)
            if s in seen:
                continue
            seen.add(s)
            unique.append(p)
        return unique

    def reload(self):
        with self._lock:
            schemas = dict(DEFAULT_SCHEMAS)
            for candidate in self._candidate_paths():
                if not candidate.exists():
                    continue
                try:
                    data = json.loads(candidate.read_text())
                    if isinstance(data, dict):
                        for event_type, schema in data.items():
                            if isinstance(schema, dict):
                                schemas[event_type] = schema
                        self._resolved_path = candidate
                        break
                except json.JSONDecodeError:
                    continue
            self._schemas = schemas

    def list(self) -> dict[str, dict]:
        with self._lock:
            return json.loads(json.dumps(self._schemas))

    def upsert(self, event_type: str, schema: dict):
        if not event_type or not isinstance(schema, dict):
            raise SchemaValidationError("Invalid schema registration payload")
        required = schema.get("required", [])
        properties = schema.get("properties", {})
        if not isinstance(required, list) or not all(isinstance(x, str) for x in required):
            raise SchemaValidationError("'required' must be a list of strings")
        if not isinstance(properties, dict):
            raise SchemaValidationError("'properties' must be an object")
        with self._lock:
            self._schemas[event_type] = schema
            persisted = {k: v for k, v in self._schemas.items() if k not in DEFAULT_SCHEMAS or self._schemas[k] != DEFAULT_SCHEMAS[k]}
            errors = []
            for candidate in self._candidate_paths():
                try:
                    candidate.parent.mkdir(parents=True, exist_ok=True)
                    candidate.write_text(json.dumps(persisted, indent=2, sort_keys=True))
                    self._resolved_path = candidate
                    return
                except OSError as e:
                    errors.append(str(e))
            raise SchemaValidationError("Unable to persist schema file: " + " | ".join(errors))

    def validate(self, event_type: str, metadata: dict):
        with self._lock:
            schema = self._resolve_schema(event_type)
        if schema is None:
            return
        if metadata is None:
            metadata = {}
        if not isinstance(metadata, dict):
            raise SchemaValidationError("metadata must be an object")

        required = schema.get("required", [])
        properties = schema.get("properties", {})
        for key in required:
            if key not in metadata:
                raise SchemaValidationError(f"Missing required metadata field: '{key}'")
        for key, rule in properties.items():
            if key not in metadata:
                continue
            value = metadata[key]
            expected_type = rule.get("type")
            if expected_type == "string" and not isinstance(value, str):
                raise SchemaValidationError(f"metadata.{key} must be string")
            if expected_type == "number" and not isinstance(value, (int, float)):
                raise SchemaValidationError(f"metadata.{key} must be number")
            if expected_type == "boolean" and not isinstance(value, bool):
                raise SchemaValidationError(f"metadata.{key} must be boolean")
            enum_values = rule.get("enum")
            if isinstance(enum_values, list) and value not in enum_values:
                raise SchemaValidationError(
                    f"metadata.{key} must be one of: {', '.join(map(str, enum_values))}"
                )

    def _resolve_schema(self, event_type: str) -> dict | None:
        return self._resolve_schema_with_seen(event_type, set())

    def _resolve_schema_with_seen(self, event_type: str, seen: set[str]) -> dict | None:
        # Precedence: exact > longest-prefix wildcard (foo.bar.*) > global wildcard (*)
        schema = self._schemas.get(event_type)
        if schema is not None:
            return self._materialize_schema(schema, seen)

        wildcard_matches: list[tuple[int, dict]] = []
        global_wildcard: dict | None = None
        for key, candidate in self._schemas.items():
            if key == "*":
                global_wildcard = candidate
                continue
            if key.endswith("*"):
                prefix = key[:-1]
                if event_type.startswith(prefix):
                    wildcard_matches.append((len(prefix), candidate))
        if wildcard_matches:
            wildcard_matches.sort(key=lambda x: x[0], reverse=True)
            return self._materialize_schema(wildcard_matches[0][1], seen)
        if global_wildcard is not None:
            return self._materialize_schema(global_wildcard, seen)
        return None

    def _materialize_schema(self, schema: dict, seen: set[str]) -> dict:
        extends = schema.get("extends")
        if not extends:
            return schema
        if not isinstance(extends, str):
            raise SchemaValidationError("'extends' must be a string")
        if extends in seen:
            raise SchemaValidationError("schema inheritance cycle detected")
        seen.add(extends)
        parent = self._resolve_schema_with_seen(extends, seen)
        if parent is None:
            raise SchemaValidationError(f"extends references unknown schema: '{extends}'")
        merged_required = list(dict.fromkeys((parent.get("required", []) or []) + (schema.get("required", []) or [])))
        merged_properties = dict(parent.get("properties", {}) or {})
        merged_properties.update(schema.get("properties", {}) or {})
        merged = dict(parent)
        merged.update(schema)
        merged["required"] = merged_required
        merged["properties"] = merged_properties
        merged.pop("extends", None)
        return merged
