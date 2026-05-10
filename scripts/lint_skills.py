"""Lint skill folders for compliance with SKILLS_DESIGN_PRINCIPLES.md.

Run with: python scripts/lint_skills.py

Exits 0 if all skills are compliant, 1 otherwise.

Rules enforced (see docs/architecture/SKILLS_DESIGN_PRINCIPLES.md):
  - Each skill folder must contain BOTH `skill.yaml` and `SKILL.md`.
  - `skill.yaml` must declare `name` and `description`.
  - `skill.yaml` must NOT contain banned legacy keys: `instructions`,
    `next_skill`, `conversational`.
  - `SKILL.md` must NOT contain banned control-flow markers:
    `[AWAITING_CONFIRMATION]`, `[STOP_CHAIN]`.
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
SKILLS = ROOT / "src" / "hub_cowork" / "skills"

BANNED_YAML_KEYS = ("instructions", "next_skill", "conversational")
BANNED_MARKERS = ("[AWAITING_CONFIRMATION]", "[STOP_CHAIN]")
REQUIRED_YAML_KEYS = ("name", "description")
LEGACY_YAML_KEYS = ("tools",)  # warn but don't fail — back-compat path


def lint_skill(folder: Path) -> list[str]:
    """Return a list of error strings for this skill folder."""
    errors: list[str] = []
    yaml_path = folder / "skill.yaml"
    md_path = folder / "SKILL.md"

    if not yaml_path.is_file():
        errors.append(f"missing {yaml_path.relative_to(ROOT)}")
        return errors  # nothing else to check
    if not md_path.is_file():
        errors.append(f"missing {md_path.relative_to(ROOT)}")

    try:
        data = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    except Exception as e:
        errors.append(f"{yaml_path.relative_to(ROOT)}: invalid YAML: {e}")
        return errors

    if not isinstance(data, dict):
        errors.append(f"{yaml_path.relative_to(ROOT)}: top-level YAML is not a mapping")
        return errors

    for k in REQUIRED_YAML_KEYS:
        if k not in data:
            errors.append(f"{yaml_path.relative_to(ROOT)}: missing required key '{k}'")

    for k in BANNED_YAML_KEYS:
        if k in data:
            errors.append(
                f"{yaml_path.relative_to(ROOT)}: banned legacy key '{k}' "
                f"(see SKILLS_DESIGN_PRINCIPLES.md \u00a72 / \u00a76)"
            )

    for k in LEGACY_YAML_KEYS:
        if k in data:
            errors.append(
                f"{yaml_path.relative_to(ROOT)}: legacy key '{k}' \u2014 "
                f"replace with 'mcp_servers:' + 'tool_allowlist:' "
                f"(see SKILLS_DESIGN_PRINCIPLES.md \u00a711)"
            )

    # Legacy `model: full|mini` should be renamed to `model_tier: reasoning|fast`.
    if "model" in data and "model_tier" not in data:
        errors.append(
            f"{yaml_path.relative_to(ROOT)}: legacy key 'model:' \u2014 "
            f"rename to 'model_tier:' with values 'reasoning' or 'fast' "
            f"(see SKILLS_DESIGN_PRINCIPLES.md \u00a712)"
        )
    tier_val = data.get("model_tier") or data.get("model")
    if tier_val is not None and tier_val not in ("reasoning", "fast", "full", "mini"):
        errors.append(
            f"{yaml_path.relative_to(ROOT)}: model_tier must be 'reasoning' or 'fast' "
            f"(got {tier_val!r})"
        )

    if md_path.is_file():
        md_text = md_path.read_text(encoding="utf-8")
        for marker in BANNED_MARKERS:
            if marker in md_text:
                errors.append(
                    f"{md_path.relative_to(ROOT)}: contains banned control marker "
                    f"'{marker}' (see SKILLS_DESIGN_PRINCIPLES.md \u00a76 / \u00a714.4)"
                )

    return errors


def find_skill_folders() -> list[Path]:
    """A skill folder is any folder containing a `skill.yaml`."""
    if not SKILLS.is_dir():
        return []
    out: list[Path] = []
    for yaml_path in sorted(SKILLS.rglob("skill.yaml")):
        if "tools" in yaml_path.parts:
            continue
        out.append(yaml_path.parent)
    return out


def main() -> int:
    folders = find_skill_folders()
    if not folders:
        print("No skill folders found under", SKILLS, file=sys.stderr)
        return 1

    total_errors = 0
    for folder in folders:
        errs = lint_skill(folder)
        rel = folder.relative_to(ROOT)
        if errs:
            total_errors += len(errs)
            print(f"FAIL  {rel}")
            for e in errs:
                print(f"      - {e}")
        else:
            print(f"OK    {rel}")

    print()
    if total_errors:
        print(f"{total_errors} lint error(s) across {len(folders)} skill(s).")
        return 1
    print(f"All {len(folders)} skill(s) clean.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
