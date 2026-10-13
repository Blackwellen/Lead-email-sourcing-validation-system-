"""Write backend/openapi.json (the contract the TypeScript client is generated from).

Usage:  python scripts/dump_openapi.py [output-path]
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.main import create_app  # noqa: E402


def main() -> None:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "openapi.json"
    schema = create_app().openapi()
    out.write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    ops = sum(len(v) for v in schema["paths"].values())
    print(f"wrote {out} ({len(schema['paths'])} paths, {ops} operations, {len(schema['components']['schemas'])} schemas)")


if __name__ == "__main__":
    main()
