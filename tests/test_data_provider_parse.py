from app.services.data_provider import _parse_percent, _parse_up_down


def test_parse_percent_handles_sign_and_suffix():
    assert _parse_percent("+1.23%") == 1.23
    assert _parse_percent("-0.56%") == -0.56
    assert _parse_percent("--") == 0.0


def test_parse_up_down_handles_valid_and_invalid_values():
    assert _parse_up_down("21/28") == (21, 28)
    assert _parse_up_down("0/0") == (0, 0)
    assert _parse_up_down("N/A") == (0, 0)
