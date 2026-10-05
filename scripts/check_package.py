"""Validate the distributed skill and current documentation using dev-only YAML."""

import ast
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills" / "onchain-address-inspector"


def check_package():
    text = (SKILL / "SKILL.md").read_text()
    frontmatter = re.match(r"\A---\n(.*?)\n---\n", text, re.S)
    assert frontmatter, "SKILL.md must begin with YAML frontmatter"
    metadata = yaml.safe_load(frontmatter.group(1))
    assert isinstance(metadata, dict), "frontmatter must be a mapping"
    assert metadata["name"] == SKILL.name, "name must match directory"
    assert re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", metadata["name"])
    assert len(metadata["name"]) <= 64
    assert isinstance(metadata["description"], str)
    assert 1 <= len(metadata["description"]) <= 1024
    assert len(text.splitlines()) < 500
    interface = yaml.safe_load((SKILL / "agents" / "openai.yaml").read_text())["interface"]
    assert interface["display_name"] and interface["short_description"]
    for path in (SKILL / "scripts").glob("*.py"):
        ast.parse(path.read_text(), filename=str(path), feature_version=(3, 10))
    docs = [ROOT / "README.md", ROOT / "README.en.md", ROOT / "CONTRIBUTING.md"]
    docs += list(SKILL.rglob("*.md")) + list((ROOT / "docs").glob("*.md"))
    for path in docs:
        for target in re.findall(r"\[[^\]]*\]\(([^)]+)\)", path.read_text()):
            if re.match(r"(?:[a-z]+:|#)", target):
                continue
            destination = (path.parent / target.split("#", 1)[0]).resolve()
            assert destination.is_relative_to(ROOT), f"link escapes repository: {path}"
            assert destination.exists(), f"broken link in {path.relative_to(ROOT)}: {target}"
    print("Skill metadata, Python 3.10 syntax, interface and current local links verified.")


if __name__ == "__main__":
    check_package()
