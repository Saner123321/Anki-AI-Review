from pathlib import Path
import hashlib
import json
import zipfile

root = Path(__file__).parent
source = root / "src" / "anki_ai_review"
dist = root / "dist"
dist.mkdir(exist_ok=True)
version = json.loads((source / "manifest.json").read_text(encoding="utf-8"))["human_version"]
target = dist / f"Anki-AI-Review-{version}.ankiaddon"
with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
    for path in sorted(source.rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts:
            z.write(path, path.relative_to(source))
print(target)
checksum = hashlib.sha256(target.read_bytes()).hexdigest()
target.with_suffix(".sha256").write_text(f"{checksum}  {target.name}\n", encoding="ascii")
print("SHA256", checksum)
