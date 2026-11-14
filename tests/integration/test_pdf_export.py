"""
Tests for the PDF export feature.

All tests work without weasyprint/kaleido installed by using mocks.
"""

from __future__ import annotations

from tests._repo import REPO_ROOT

import sys
from unittest import mock

import pytest

# Ensure the package is importable
_SCRIPT_DIR = str(REPO_ROOT)
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)


def _make_vehicle():
    """Create a minimal VehicleInfo for testing."""
    from lib.vehicle_detect import VehicleInfo
    return VehicleInfo(
        vehicle_type="Copter",
        firmware_version="4.5.0",
        board_name="CubeOrange",
        frame_class=1,
        motor_count=4,
        imu_count=3,
        is_quadplane=False,
    )


class TestHtmlToPdfImportError:
    """html_to_pdf raises ImportError with a helpful message when weasyprint
    is missing."""

    def test_html_to_pdf_import_error(self):
        """When weasyprint is not installed, html_to_pdf should raise
        ImportError with install instructions."""
        with mock.patch.dict(sys.modules, {"weasyprint": None}):
            from lib.pdf_export import html_to_pdf
            with pytest.raises(ImportError, match="pip install arduscope\\[pdf\\]"):
                html_to_pdf("<html></html>", "/tmp/test.pdf")


class TestStaticImagesFlag:
    """HtmlReportGenerator with static_images=True renders <img> tags
    instead of interactive Plotly divs."""

    def test_static_images_produces_img_tags(self):
        """When static_images=True and kaleido is available (mocked),
        _render_static_figure returns an <img> tag with base64 PNG data."""
        import plotly.graph_objects as go
        from lib.html_report import HtmlReportGenerator

        fig = go.Figure(go.Scatter(x=[1, 2, 3], y=[4, 5, 6]))

        fake_png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
        with mock.patch("plotly.io.to_image", return_value=fake_png):
            result_html = HtmlReportGenerator._render_static_figure(fig)

        assert "<img " in result_html
        assert "data:image/png;base64," in result_html

    def test_failed_chart_raises(self):
        """A PDF without its charts must not pass as a report."""
        import plotly.graph_objects as go
        from lib.html_report import ChartRenderError, HtmlReportGenerator

        fig = go.Figure(go.Scatter(x=[1, 2], y=[3, 4]))

        with mock.patch("plotly.io.to_image",
                        side_effect=RuntimeError("Kaleido requires Chrome")):
            with pytest.raises(ChartRenderError, match="requires Chrome"):
                HtmlReportGenerator._render_static_figure(fig)

    def test_first_failure_stops_the_report(self):
        """Each attempt searched for Chrome for seconds, so one run spent
        six minutes failing on every figure of a 5 MB log."""
        import plotly.graph_objects as go
        from lib.html_report import ChartRenderError, HtmlReportGenerator
        from analyzers import AnalysisResult

        figures = [go.Figure(go.Scatter(x=[1, 2], y=[3, 4]))
                   for _ in range(3)]
        result = AnalysisResult(title="Test", status="good", summary="OK",
                                figures=figures)
        gen = HtmlReportGenerator(vehicle_info=_make_vehicle(),
                                  results=[result], static_images=True)

        with mock.patch("plotly.io.to_image",
                        side_effect=RuntimeError("no Chrome")) as to_image, \
             mock.patch.object(HtmlReportGenerator, "_get_plotly_js",
                               return_value=""):
            with pytest.raises(ChartRenderError):
                gen.generate()

        assert to_image.call_count == 1

    def test_static_images_flag_routes_to_static(self):
        """When static_images=True, generate() uses _render_static_figure
        for figures instead of pio.to_html."""
        import plotly.graph_objects as go
        from lib.html_report import HtmlReportGenerator
        from analyzers import AnalysisResult

        fig = go.Figure(go.Scatter(x=[1, 2, 3], y=[4, 5, 6]))
        result = AnalysisResult(
            title="Test Analyzer",
            status="good",
            summary="Everything OK",
            figures=[fig],
        )
        vehicle = _make_vehicle()

        fake_png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100

        with mock.patch("plotly.io.to_image", return_value=fake_png), \
             mock.patch.object(
                 HtmlReportGenerator, '_get_plotly_js',
                 return_value='<script>/* plotly stub */</script>',
             ):
            gen = HtmlReportGenerator(
                vehicle_info=vehicle,
                results=[result],
                log_filename="test.bin",
                static_images=True,
            )
            html_output = gen.generate()

        assert "<img " in html_output
        assert "data:image/png;base64," in html_output

    def test_non_static_uses_plotly_divs(self):
        """Default (static_images=False) should use interactive Plotly HTML,
        not static <img> tags."""
        import plotly.graph_objects as go
        from lib.html_report import HtmlReportGenerator
        from analyzers import AnalysisResult

        fig = go.Figure(go.Scatter(x=[1, 2], y=[3, 4]))
        result = AnalysisResult(
            title="Test",
            status="info",
            summary="Test",
            figures=[fig],
        )
        vehicle = _make_vehicle()

        with mock.patch.object(
            HtmlReportGenerator, '_get_plotly_js',
            return_value='<script>/* plotly stub */</script>',
        ):
            gen = HtmlReportGenerator(
                vehicle_info=vehicle,
                results=[result],
                log_filename="test.bin",
                static_images=False,
            )
            html_output = gen.generate()

        # Interactive plotly output should contain plotly div markers
        assert "plotly" in html_output.lower()
        # Should NOT contain static image tags for figures
        assert "data:image/png;base64," not in html_output


class TestPdfFormatInArgparse:
    """Verify that 'pdf' is an accepted value for the --format argument."""

    def test_pdf_in_format_choices(self):
        """The flight_report argparser should accept --format pdf."""
        import subprocess
        result = subprocess.run(
            [sys.executable, "-c",
             "import sys; sys.argv = ['flight_report', '--format', 'pdf', '--help']; "
             "import flight_report; flight_report.main()"],
            capture_output=True, text=True, cwd=_SCRIPT_DIR,
        )
        # --help exits with 0 and prints usage; 'pdf' should appear
        # in the choices without causing an argument error
        assert result.returncode == 0, (
            f"--format pdf caused an error: {result.stderr}"
        )
        assert "pdf" in result.stdout.lower()

    def test_pdf_format_generates_pdf_extension(self):
        """build_output_path with fmt='pdf' should produce a .pdf file."""
        from flight_report import build_output_path

        path = build_output_path("/tmp/test.bin", None, "pdf")
        assert path.endswith(".pdf")
        assert "test_report.pdf" in path


class TestLocaleKeys:
    """Verify PDF-related locale keys exist in both zh and en."""

    def test_locale_keys_exist(self):
        from lib.i18n import set_locale, t, get_locale

        original = get_locale()
        try:
            for lang in ("zh", "en"):
                set_locale(lang)
                pdf_exported = t("report.pdf_exported", path="/tmp/test.pdf")
                assert pdf_exported != "report.pdf_exported", (
                    f"Key report.pdf_exported missing in {lang}"
                )
                assert "/tmp/test.pdf" in pdf_exported

                pdf_req = t("report.pdf_requires_weasyprint")
                assert pdf_req != "report.pdf_requires_weasyprint", (
                    f"Key report.pdf_requires_weasyprint missing in {lang}"
                )
                assert "pip install" in pdf_req
        finally:
            set_locale(original)


class TestCliChartFailure:
    def test_exits_nonzero_and_names_chrome(self, tmp_path, capsys):
        import flight_report
        from lib.i18n import set_locale
        from tests.binlog import flight_log

        log = flight_log(duration_s=8.0).save(tmp_path / "flight.BIN")
        out = tmp_path / "report.pdf"
        argv = ["flight_report.py", log, "--format", "pdf", "--only",
                "summary", "--no-open", "--no-fleet-record", "--no-ai",
                "--lang", "en", "-o", str(out)]

        with mock.patch.object(sys, "argv", argv), \
             mock.patch("plotly.io.to_image",
                        side_effect=RuntimeError("Kaleido requires Chrome")), \
             mock.patch("lib.pdf_export.html_to_pdf") as to_pdf:
            with pytest.raises(SystemExit) as stop:
                flight_report.main()
        set_locale("zh")

        assert stop.value.code == 1
        err = capsys.readouterr().err
        assert "Chrome" in err and "kaleido 1.x" in err
        to_pdf.assert_not_called()
        assert not out.exists()
