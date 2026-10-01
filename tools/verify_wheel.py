"""Run with the clean environment's Python, outside the checkout, after wheel install."""

import hashlib
import json
import sys
import zipfile
from importlib.resources import files
from pathlib import Path

from tokenizers import Tokenizer

from local_ai.config import Settings
from local_ai.context import tokenizer_for

wheel = Path(sys.argv[1]).resolve()
assets = files("local_ai")
required = [
    *(f"prompts/{task}.txt" for task in ("project", "diff", "logs")),
    "tokenizers/manifest.json",
    "tokenizers/NOTICE.md",
    "tokenizers/gemma3.terms.html",
    "tokenizers/nemotron3.terms.html",
    "tokenizers/gemma3.json",
    "tokenizers/nemotron3.json",
]
with zipfile.ZipFile(wheel) as archive:
    names = archive.namelist()
    assert any(name.endswith(".dist-info/licenses/LICENSE") for name in names)
    for name in required:
        assert "local_ai/" + name in names, name
        assert assets.joinpath(name).read_bytes(), name

manifest = json.loads(assets.joinpath("tokenizers/manifest.json").read_text())
for name, entry in manifest.items():
    data = assets.joinpath(f"tokenizers/{name}.json").read_bytes()
    assert hashlib.sha256(data).hexdigest() == entry["sha256"], name
    assert Tokenizer.from_str(data.decode()).encode("Offline packaging check").ids
for profile in Settings().profiles.values():
    assert tokenizer_for(profile).encode("Bundled tokenizer accessible").ids
print(
    f"Wheel verified: {wheel.name}; {wheel.stat().st_size} bytes "
    f"({wheel.stat().st_size / 1048576:.2f} MiB); prompts, tokenizers, notices and licenses OK"
)
