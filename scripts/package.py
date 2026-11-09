"""Build the custom integration archive, excluding local/private runtime files."""

import json
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

root = Path(__file__).resolve().parents[1]
source = root / "custom_components" / "home_insights"
version = json.loads((source / "manifest.json").read_text())["version"]
destination = root / "dist"
destination.mkdir(exist_ok=True)
with ZipFile(destination / f"home-insights-{version}.zip", "w", ZIP_DEFLATED) as archive:
    for path in sorted(source.rglob("*")):
        if path.is_file() and path.suffix in {".py", ".json", ".yaml"}:
            archive.write(path, path.relative_to(root))
print(f"Built dist/home-insights-{version}.zip")
