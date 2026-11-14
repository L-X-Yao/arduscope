"""tools/check_suggestions.py: the registry and the code stay together.

Each case breaks one thing and asserts the checker names it. A guard that has
never been seen to fail is not known to work.
"""

from __future__ import annotations

from tests._repo import TOOLS_DIR

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(TOOLS_DIR))

import check_suggestions as cs  # noqa: E402

pytestmark = pytest.mark.fast

HEADER = "## Registry\n\n| Parameter | Analyzer | Rule | Source | Flight | Status |\n"


def _table(*lines: str) -> str:
    return HEADER + "\n".join(lines) + "\n"


def _row(params="`A_B`", analyzer="mod", source="A: wiki x", flight="N1",
         status="flown") -> str:
    return f"| {params} | {analyzer} | rule | {source} | {flight} | {status} |"


def _rows(*lines: str) -> list[cs.Row]:
    rows, errors = cs.parse_registry(_table(*lines))
    assert not errors
    return rows


@pytest.fixture(scope="module")
def registry() -> list[cs.Row]:
    rows, errors = cs.parse_registry(
        Path(cs.REGISTRY_PATH).read_text(encoding="utf-8"))
    assert not errors
    return rows


class TestTheRepository:
    def test_passes(self):
        assert cs.main([]) == 0

    def test_a_row_taken_out_fails(self, registry):
        scanned = cs.scan_analyzers()
        kept = [r for r in registry if r.key != "gps:GPS_AUTO_SWITCH"]
        assert len(kept) == len(registry) - 1
        errors = cs.check_code(kept, scanned, cs.DYNAMIC_SITES)
        assert errors == ["gps suggests GPS_AUTO_SWITCH: no row in the registry"]

    def test_a_suggestion_added_to_an_analyzer_fails(self, registry):
        scanned = dict(cs.scan_analyzers())
        names, dynamic = scanned["gps"]
        scanned["gps"] = (names | {"GPS_NEW"}, dynamic)
        errors = cs.check_code(registry, scanned, cs.DYNAMIC_SITES)
        assert errors == ["gps suggests GPS_NEW: no row in the registry"]

    def test_a_new_row_that_never_flew_fails(self, registry):
        row = cs.Row(("GPS_NEW",), "gps", "A: wiki x", "-", "not_flown")
        errors = cs.check_rows([*registry, row], cs.NOT_FLOWN_WAIVERS)
        assert len(errors) == 1 and "no waiver" in errors[0]


class TestScan:
    def test_literal_name(self):
        names, dynamic = cs.scan_source("mod", """
def f():
    return {"param": "A_B", "suggested": 1}
""")
        assert names == {"A_B"} and not dynamic

    def test_the_new_key_counts_as_a_suggestion(self):
        names, _ = cs.scan_source("mod", '{"param": "A_B", "new": 1}')
        assert names == {"A_B"}

    def test_a_dict_with_no_value_is_not_a_suggestion(self):
        names, dynamic = cs.scan_source("mod", '{"param": x, "suggestions": 1}')
        assert not names and not dynamic

    def test_variable_name_is_a_dynamic_site(self):
        names, dynamic = cs.scan_source("mod", """
class X:
    def f(self):
        return {"param": name, "suggested": 1}
""")
        assert not names and dynamic == {"mod.f:name"}

    def test_copying_another_suggestion_is_not_a_site(self):
        names, dynamic = cs.scan_source("mod", """
def f(r):
    return {"param": r["param"], "suggested": r["new"]}
""")
        assert not names and not dynamic

    def test_names_unpacked_from_tuples_are_read(self):
        names, dynamic = cs.scan_source("mod", """
def f(recs):
    rows = [("A_B", "0", "1"), ("C_D", "0", "1")]
    rows.append(("E_F", "0", "1"))
    for param, old, new in rows:
        recs.append({"param": param, "old": old, "new": new})
""")
        assert names == {"A_B", "C_D", "E_F"} and not dynamic


class TestRows:
    def test_a_good_row_passes(self):
        assert not cs.check_rows(_rows(_row()), {})

    @pytest.mark.parametrize("line, needle", [
        (_row(status="tested"), "status"),
        (_row(source="forum post"), "source must be"),
        (_row(flight="-"), "no run named"),
        (_row(status="not_flown"), "run named but status"),
        (_row(params="`A_B`", flight="-", status="log_setting"), "only LOG_"),
        (_row(flight="-", status="not_flown"), "no waiver"),
        (_row(source="none"), "no waiver"),
    ])
    def test_a_bad_row_fails(self, line, needle):
        errors = cs.check_rows(_rows(line), {})
        assert errors and needle in errors[0]

    def test_a_waiver_lets_a_row_ship_unflown(self):
        rows = _rows(_row(flight="-", status="not_flown", source="none"))
        assert not cs.check_rows(rows, {"mod:A_B": "reason"})

    def test_a_waiver_nothing_needs_fails(self):
        errors = cs.check_rows(_rows(_row()), {"mod:A_B": "reason"})
        assert errors == ["mod:A_B: waiver for a row that does not need one"]

    def test_a_row_with_the_wrong_cell_count_fails(self):
        _, errors = cs.parse_registry(_table("| `A_B` | mod | rule |"))
        assert errors and "expected 6 cells" in errors[0]

    def test_rows_stop_at_the_next_section(self):
        rows, _ = cs.parse_registry(
            _table(_row()) + "\n## Removed\n\n| `C_D` | v1 | why |\n")
        assert [r.key for r in rows] == ["mod:A_B"]

    def test_no_registry_section_fails(self):
        assert cs.parse_registry("# Title\n")[1]


class TestCode:
    def test_a_row_nothing_suggests_fails(self):
        errors = cs.check_code(_rows(_row(params="`A_B` `C_D`")),
                               {"mod": ({"A_B"}, set())}, {})
        assert errors == ["mod:A_B: `C_D` matches nothing in the code"]

    def test_a_row_for_another_analyzer_does_not_count(self):
        errors = cs.check_code(_rows(_row(analyzer="other")),
                               {"mod": ({"A_B"}, set())}, {})
        assert "mod suggests A_B: no row in the registry" in errors

    def test_an_undeclared_site_fails(self):
        errors = cs.check_code([], {"mod": (set(), {"mod.f:name"})}, {})
        assert errors == ["mod.f:name: variable name, declare it in DYNAMIC_SITES"]

    def test_a_declared_site_must_exist(self):
        errors = cs.check_code([], {}, {"mod.f:name": ("A_B",)})
        assert errors == ["mod.f:name: in DYNAMIC_SITES but not in the code"]

    def test_a_declared_site_produces_its_names(self):
        assert not cs.check_code(_rows(_row(params="`A_*`")),
                                 {"mod": (set(), {"mod.f:name"})},
                                 {"mod.f:name": ("A_B", "A_C")})


class TestBaseline:
    def _scan(self, *suggestions: str) -> dict:
        return {"logs": [{"analyzers": {"mod": {"suggestions": list(suggestions)}}}]}

    def test_a_registered_name_passes(self):
        assert not cs.check_baseline(_rows(_row()), self._scan("A_B=1.0"))

    def test_a_name_the_table_lacks_fails(self):
        assert cs.check_baseline(_rows(_row()), self._scan("C_D=1.0")) == [
            "mod suggested C_D in a scan: no row in the registry"]

    def test_a_log_that_failed_has_nothing_to_check(self):
        scan = {"logs": [{"analyzers": None}, {"analyzers": {"mod": {}}}]}
        assert not cs.check_baseline(_rows(_row()), scan)
