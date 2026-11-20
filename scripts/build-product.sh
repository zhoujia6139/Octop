#!/usr/bin/env bash
# Build a self-contained wheel set from the repository's local library sources.
# Does not remove unrelated artifacts (including earlier candidate archives).
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUTPUT="${1:-${ROOT}/dist/product}"
mkdir -p "$OUTPUT"
OUTPUT="$(cd "$OUTPUT" && pwd)"
for project in octop-memory octop-harness; do
  uv build --wheel --out-dir "$OUTPUT" "$ROOT/vendor/$project"
done
uv build --out-dir "$OUTPUT" "$ROOT"
for role in cloud local-runtime; do
  uv export --project "$ROOT" --frozen --no-dev --extra "$role" \
    --no-emit-local --no-hashes -o "$OUTPUT/requirements-$role.txt" >/dev/null
done
uv run --no-project --python 3.12 python - "$ROOT" "$OUTPUT" <<'PYTHON'
import hashlib
import json
import sys
from pathlib import Path
root, output = map(Path, sys.argv[1:])
libraries = {}
for name in ("octop-harness", "octop-memory"):
    directory = root / "vendor" / name
    digest = hashlib.sha256()
    files = [directory / "pyproject.toml", *directory.joinpath("src").rglob("*")]
    for path in sorted(files):
        if not path.is_file() or "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        digest.update(path.relative_to(directory).as_posix().encode() + b"\0")
        digest.update(path.read_bytes())
    libraries[name] = {**json.loads((directory / "VENDOR.json").read_text()), "local_source_sha256": digest.hexdigest()}
(output / "manifest.json").write_text(json.dumps({"format": 1, "libraries": libraries}, indent=2) + "\n")
files = [*output.glob("*.whl"), *output.glob("*.tar.gz"), *output.glob("requirements-*.txt"), output / "manifest.json"]
(output / "SHA256SUMS").write_text("".join(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n" for path in sorted(files)))
PYTHON
echo "[product] wheel set and locked requirements: $OUTPUT"
