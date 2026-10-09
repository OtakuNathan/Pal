"""Fail closed on incomplete inventory and false-positive TLC outcomes."""
import json
from pathlib import Path

import pytest

from scripts.run_tlc import SUCCESS, accepted, ensure_jar, load_inventory


@pytest.mark.parametrize("kind", ["negative", "witness"])
@pytest.mark.parametrize("initial", [False, True])
def test_counterexamples_require_exact_invariant_and_clean_exit(kind, initial):
    case = {"kind": kind, "invariant": "Ownership"}
    output = "Error: Invariant Ownership is violated.\nError: The behavior up to this point is:\nFinished in 1s\n"
    if initial:
        output = "Error: Invariant Ownership is violated by the initial state:\ns = 0\nFinished in 1s\n"
    assert accepted(case, 12, output)
    for status in (0, 1, 13, 137, None):
        assert not accepted(case, status, output)
    assert not accepted(case, 12, output.replace("Ownership", "AnotherInvariant"))
    assert not accepted(case, 12, output + "Error: TLC threw an unexpected exception.\n")
    assert not accepted(case, 12, output.replace("Finished in 1s", ""))


def test_positive_models_must_complete_without_errors():
    case = {"kind": "positive"}
    complete = SUCCESS + "\n1 states generated, 1 distinct states found, 0 states left on queue.\nFinished in 1s\n"
    assert accepted(case, 0, complete)
    assert not accepted(case, 0, SUCCESS)
    assert not accepted(case, 0, complete.replace("1 distinct", "0 distinct"))
    assert not accepted(case, 0, complete.replace("0 states left", "1 states left"))
    assert not accepted(case, 0, complete.replace("Finished in 1s", ""))
    assert not accepted(case, 0, "Parsing file Example.tla")
    assert not accepted(case, 0, complete + "\nError: failure")
    assert not accepted(case, None, SUCCESS)
    assert not accepted({"kind": "retired"}, 0, SUCCESS)


def test_inventory_rejects_new_configs_and_unchecked_modules(tmp_path):
    folder = tmp_path / "spec/core"
    folder.mkdir(parents=True)
    (folder / "Example.cfg").write_text("INIT Init\nNEXT Next\n")
    (folder / "Example.tla").write_text("---- MODULE Example ----\n====\n")
    case = {"config": "spec/core/Example.cfg", "module": "spec/core/Example.tla", "kind": "positive"}
    manifest = tmp_path / "spec/tlc-suite.json"
    manifest.write_text(json.dumps({"cases": [case]}))
    assert load_inventory(tmp_path)["cases"] == [case]
    (folder / "Forgotten.cfg").write_text("INIT Init\nNEXT Next\n")
    with pytest.raises(ValueError, match="Unclassified TLC configs"):
        load_inventory(tmp_path)
    (folder / "Forgotten.cfg").unlink()
    (folder / "Forgotten.tla").write_text("---- MODULE Forgotten ----\n====\n")
    with pytest.raises(ValueError, match="Unclassified/missing TLA modules"):
        load_inventory(tmp_path)
    (folder / "Forgotten.tla").unlink()
    manifest.write_text(json.dumps({"cases": [case, case]}))
    with pytest.raises(ValueError, match="Duplicate"):
        load_inventory(tmp_path)
    manifest.write_text(json.dumps({"cases": [{**case, "kind": "negative", "invariant": "Ownership"}]}))
    with pytest.raises(ValueError, match="explicit INVARIANT declaration"):
        load_inventory(tmp_path)
    (folder / "Example.cfg").write_text("INIT Init\nNEXT Next\nINVARIANTS TypeOK Ownership\n")
    assert load_inventory(tmp_path)["cases"][0]["invariant"] == "Ownership"


def test_missing_or_changed_tool_is_an_error_not_a_skip(tmp_path):
    jar = tmp_path / "tla2tools.jar"
    pin = {"sha256": "0" * 64}
    with pytest.raises(ValueError, match="missing"):
        ensure_jar(jar, pin)
    jar.write_bytes(b"different tool")
    with pytest.raises(ValueError, match="mismatch"):
        ensure_jar(jar, pin)
    assert jar.read_bytes() == b"different tool"


def test_repository_inventory_is_complete():
    load_inventory()


def test_download_is_verified_before_installing_jar(tmp_path, monkeypatch):
    import hashlib
    import io
    import urllib.request

    jar = tmp_path / "tools/tla2tools.jar"
    payload = b"pinned test tool bytes"
    pin = {"url": "https://example.invalid/tla2tools.jar", "sha256": hashlib.sha256(payload).hexdigest()}
    monkeypatch.setattr(urllib.request, "urlopen", lambda *args, **kwargs: io.BytesIO(b"wrong bytes"))
    with pytest.raises(ValueError, match="pinned SHA-256"):
        ensure_jar(jar, pin, download=True)
    assert not jar.exists()
    monkeypatch.setattr(urllib.request, "urlopen", lambda *args, **kwargs: io.BytesIO(payload))
    ensure_jar(jar, pin, download=True)
    assert jar.read_bytes() == payload
