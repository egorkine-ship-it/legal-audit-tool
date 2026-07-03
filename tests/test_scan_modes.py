from __future__ import annotations

from scanner.models import ScanInput, ScanResult


def test_scan_input_defaults_to_quick_no_llm_no_full_pdf():
    scan_input = ScanInput(site_url="https://example.ru")

    assert scan_input.scan_mode == "quick"
    assert scan_input.use_llm is False
    assert scan_input.use_agent is False
    assert scan_input.create_pdf is False


def test_deep_scan_input_is_explicit():
    scan_input = ScanInput(
        site_url="https://example.ru",
        scan_mode="deep",
        use_llm=True,
        use_agent=True,
        create_pdf=True,
    )

    assert scan_input.scan_mode == "deep"
    assert scan_input.use_llm is True
    assert scan_input.use_agent is True
    assert scan_input.create_pdf is True


def test_scan_result_defaults_to_quick_mode():
    result = ScanResult(site_url="https://example.ru")

    assert result.scan_mode == "quick"
