"""The publication check catches API keys of the shapes this app handles, not the tests' placeholders.

Keys are built at run time from short pieces, so this file itself never holds one.
"""

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_publication.py"


def load():
    spec = importlib.util.spec_from_file_location("check_publication", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_api_keys_are_caught_and_placeholders_pass():
    check = load()
    random = "Zp4Lm9Xw2Rt8" + "Vn5Kc3Hb6Jd1" + "Qa7Ws0EdRf5T"
    hexadecimal = "".join("0123456789abcdef"[(i * 7) % 16] for i in range(32))
    for key in (
        "sk-" + random,
        "sk-" + "proj-" + random + random,
        "sk-" + "ant-api03-" + random + random,
        "sk-" + hexadecimal,  # DeepSeek's shape
    ):
        assert check.credential(f'key = "{key}"'), key[:12]
    for text in (
        "KEY = " + '"sk-unit-" + "q" * 40 + "Z9yx"',
        "sk-unit-" + "q" * 40 + "Z9yx",
        "sk-endpoint-" + "k" * 30 + "TAIL",
        "sk-unit-****************************Z9yx",
        "the prefix sk-ant- and sk-proj- in prose",
    ):
        assert not check.credential(text), text[:20]
