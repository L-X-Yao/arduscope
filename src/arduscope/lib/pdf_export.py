"""PDF export using weasyprint + kaleido for static chart images."""


def html_to_pdf(html_string: str, output_path: str) -> None:
    """Convert an HTML report string to PDF.

    Requires the ``weasyprint`` package.  Install via::

        pip install arduscope[pdf]

    Args:
        html_string: Full HTML document as a string (from HtmlReportGenerator.generate()).
        output_path: Destination path for the generated PDF file.

    Raises:
        ImportError: If weasyprint is not installed.
    """
    try:
        from weasyprint import HTML  # type: ignore[import-untyped]
    except ImportError:
        raise ImportError(
            "PDF export requires weasyprint. Install: pip install arduscope[pdf]"
        )
    HTML(string=html_string).write_pdf(output_path)
