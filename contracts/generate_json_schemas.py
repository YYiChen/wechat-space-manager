"""Generate checked-in JSON Schema artifacts for public versioned contracts."""

from __future__ import annotations

import json
from pathlib import Path

from wechat_cleaner.domain.contracts import PUBLIC_CONTRACTS


def main() -> None:
    output_dir = Path(__file__).parent / "jsonschema"
    output_dir.mkdir(parents=True, exist_ok=True)
    for stem, model in PUBLIC_CONTRACTS.items():
        schema = model.model_json_schema()
        version = str(model.model_fields["schema_version"].default)
        schema["$id"] = f"https://wechat-space-manager.local/schema/{stem}/{version}.json"
        path = output_dir / f"{stem}.schema.json"
        path.write_text(json.dumps(schema, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
