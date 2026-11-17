"""YAML dumping that is safe for YAML 1.1 readers.

ruamel.yaml writes YAML 1.2, where a bare ``OFF`` or ``no`` is a string. PyYAML (used by ADK) and
Go's YAML libraries (used by Kubernetes tooling) are YAML 1.1 and read them as booleans, so an
emitted ``threshold: OFF`` silently became ``False``. ``new_yaml()`` quotes every string that a
1.1 resolver would turn into another type.
"""
from __future__ import annotations

import re

from ruamel.yaml import YAML
from ruamel.yaml.representer import RoundTripRepresenter

_WORDS = {"y", "n", "yes", "no", "on", "off", "true", "false", "null", "~"}
_NUMBERISH = re.compile(
    r"^[-+]?(0b[01_]+|0[0-7_]+|0x[0-9a-f_]+|[0-9][0-9_]*(:[0-5]?[0-9])+(\.[0-9_]*)?)$", re.IGNORECASE)


def needs_quotes(s: str) -> bool:
    return s.lower() in _WORDS or bool(_NUMBERISH.match(s))


class _Representer(RoundTripRepresenter):
    def represent_str(self, data):
        if needs_quotes(data):
            return self.represent_scalar("tag:yaml.org,2002:str", data, style="'")
        return super().represent_str(data)


_Representer.add_representer(str, _Representer.represent_str)


def new_yaml() -> YAML:
    y = YAML()
    y.Representer = _Representer
    return y
