import json
from pathlib import Path
from .domain import InputError, canonical_slot


class Catalog:
    def __init__(self, directory):
        root = Path(directory)
        self.materials = json.loads((root / "materials.json").read_text(encoding="utf-8"))
        self.supplements = json.loads((root / "supplements.json").read_text(encoding="utf-8"))
        self.rules = json.loads((root / "rules.json").read_text(encoding="utf-8"))
        schedule = json.loads((root / "schedule.json").read_text(encoding="utf-8"))
        try:
            if not isinstance(self.materials, dict) or not isinstance(self.supplements, dict):
                raise ValueError()
            for key, value in self.materials.items():
                if not isinstance(value, dict) or any(not isinstance(v, str) for v in value.values()):
                    raise ValueError()
                if value.get("id") != key:
                    raise ValueError()
            for value in self.supplements.values():
                if not isinstance(value, dict) or any(not isinstance(v, str) for v in value.values()):
                    raise ValueError()
                if set(value) - {"rights", "title", "description", "platform", "version"}:
                    raise ValueError()
            for key in ["required_fields", "blocked_terms", "allowed_rights", "manual_review_terms"]:
                if not isinstance(self.rules[key], list) or any(not isinstance(s, str) for s in self.rules[key]):
                    raise ValueError()
            if not {"title", "platform", "rights"}.issubset(self.rules["required_fields"]):
                raise ValueError()
            if not isinstance(self.rules["version"], str):
                raise ValueError()
            self.slots = sorted(set(canonical_slot(s) for s in schedule["slots"]))
            self.existing = [{"platform": s["platform"], "slot": canonical_slot(s["slot"])}
                             for s in schedule["occupied"]]
            if any(not isinstance(s["platform"], str) for s in self.existing):
                raise ValueError()
        except (TypeError, KeyError, ValueError, AttributeError):
            raise InputError("合成资料格式无效，请检查 materials/supplements/rules/schedule.json") from None
