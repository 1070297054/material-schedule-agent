"""Explicit local env-file loading; values are never evaluated as shell code."""
import os
from pathlib import Path
from .domain import InputError

ALLOWED_ENV = {"OPENAI_API_KEY", "OPENAI_MODEL", "OPENAI_BASE_URL",
               "DEEPSEEK_API_KEY", "DEEPSEEK_MODEL", "DEEPSEEK_BASE_URL"}


def load_env_file(filename):
    if filename is None:
        return
    values = {}
    for number, line in enumerate(Path(filename).read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not separator or key not in ALLOWED_ENV:
            raise InputError("环境配置第 %d 行无效（仅支持声明过的 KEY=value，不显示原文）" % number)
        if value[:1] in {"'", '"'}:
            if len(value) < 2 or value[-1] != value[0]:
                raise InputError("环境配置第 %d 行引号不匹配" % number)
            value = value[1:-1]
        values[key] = value
    for key, value in values.items():
        os.environ.setdefault(key, value)  # Explicit process environment has priority.
