"""User-facing identity values take the locale's word for Unknown."""

from __future__ import annotations

import pytest

from lib.i18n import localized_value, set_locale


@pytest.mark.fast
def test_unknown_values_follow_the_locale():
    try:
        set_locale("zh")
        assert localized_value("Unknown") == "未知"
        assert localized_value(None) == "未知"
        assert localized_value("  ") == "未知"
        assert localized_value("CubeOrange") == "CubeOrange"

        set_locale("en")
        assert localized_value("Unknown") == "Unknown"
        assert localized_value("CubeOrange") == "CubeOrange"
    finally:
        set_locale("zh")
