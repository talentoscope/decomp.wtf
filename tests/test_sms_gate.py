"""The tool-backed gate must not turn missing tools, skipped checks or stale reports into success."""
import xml.etree.ElementTree as ET

import pytest

from tools import sms_gate


def report(tmp_path, *, skipped=False, failed=False, missing_module=False, gg=True):
    suite = ET.Element("testsuite")
    modules = sms_gate.TESTS[:-1] if missing_module else sms_gate.TESTS
    for name in modules:
        case = ET.SubElement(suite, "testcase", classname=name[:-3].replace("/", "."),
                             name="test_rebuild[gg]" if gg else "test_rebuild[sms]")
        if skipped:
            ET.SubElement(case, "skipped")
        if failed:
            ET.SubElement(case, "failure")
    path = tmp_path / "results.xml"
    ET.ElementTree(suite).write(path)
    return path


def test_gate_rejects_missing_or_stale_tool_install(monkeypatch):
    monkeypatch.setattr(sms_gate.toolchains, "installed", lambda _: None)
    with pytest.raises(ValueError, match="install the pinned tools"):
        sms_gate.require_tools()
    monkeypatch.setattr(sms_gate.toolchains, "installed", lambda _: {"source": "old", "parts": []})
    with pytest.raises(ValueError, match="differs from the registry"):
        sms_gate.require_tools()


def test_gate_rejects_an_install_manifest_without_binaries(tmp_path, monkeypatch):
    spec = sms_gate.toolchains.get("wla-dx")
    monkeypatch.setattr(sms_gate.toolchains, "installed", lambda _: {"source": spec.source, "parts": spec.parts_for()})
    monkeypatch.setattr(sms_gate.toolchains, "path", lambda _: tmp_path)
    with pytest.raises(ValueError, match="missing or not executable"):
        sms_gate.require_tools()


@pytest.mark.parametrize("kwargs", [{"skipped": True}, {"failed": True}, {"missing_module": True}, {"gg": False}])
def test_gate_rejects_incomplete_or_unsuccessful_execution(tmp_path, kwargs):
    with pytest.raises(ValueError):
        sms_gate.verify_report(report(tmp_path, **kwargs))


def test_gate_requires_a_valid_report_with_executed_tests(tmp_path):
    path = tmp_path / "missing.xml"
    with pytest.raises(ValueError, match="valid JUnit"):
        sms_gate.verify_report(path)
    path.write_text("<testsuite />")
    with pytest.raises(ValueError, match="executed, passing tests"):
        sms_gate.verify_report(path)
    assert sms_gate.verify_report(report(tmp_path)) == len(sms_gate.TESTS)
