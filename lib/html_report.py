"""
HTML Report Generator for flight log analysis.

Generates a standalone HTML file with:
- Embedded plotly.js (offline, no CDN needed)
- Health dashboard with colored status circles
- Table of contents
- Card-style sections with status-colored left border
- Chinese font support
- LTTB downsampling for large plotly traces (max 5000 points per trace)
"""

import html
import os
import re
from datetime import datetime
from typing import Optional

import numpy as np
import plotly.io as pio
import plotly.offline

from .results import AnalysisResult
from .executive_summary import (
    build_executive_summary,
    get_level_display,
)
from .i18n import t
from .param_conflict import distinct_values, merge_suggestions
from .vehicle_detect import VehicleInfo


def _get_version() -> str:
    try:
        from __init__ import __version__
        return __version__
    except Exception:
        return "unknown"


# Leaflet 1.9.4 with its license. tests/test_offline_report.py pins both
# files to the hashes published with that release.
_LEAFLET_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "vendor", "leaflet")


class ChartRenderError(RuntimeError):
    """A chart could not be turned into an image for the PDF."""


# Maximum data points per trace in plotly charts.
# 2000 points covers a 5-minute flight at ~2.4 Hz visual resolution,
# which is sufficient for tuning diagnosis without bloating report size.
MAX_POINTS_PER_TRACE = 2000


# The only markup analyzers and locale strings put into detail lines.
_DETAIL_TAGS = re.compile(r'&lt;(/?(?:strong|em|b|br|pre))&gt;')


def _safe_detail(text: str) -> str:
    """Escape a detail line, keeping the project's own plain markup.

    Details mix locale markup with values from the log (firmware string,
    mode names, file names), and a log can come from anyone. Escaping here,
    where they all meet, covers every source at once. Lines already escaped
    by their author are decoded first, so they are not escaped twice.
    """
    escaped = html.escape(html.unescape(str(text)), quote=False)
    return _DETAIL_TAGS.sub(r'<\1>', escaped)


def _format_action_text(text: str) -> str:
    """Format action_text for HTML display with line breaks before step markers.

    Analyzer action_text is a plain string with steps like "1. xxx2. xxx".
    This function html-escapes the text and inserts <br> before step markers
    so numbered/circled steps render on separate lines.
    """
    if not text:
        return ""
    escaped = html.escape(text)
    # Insert <br> before Arabic-numbered steps: "1. " "2. " etc.
    # Require space after dot to avoid matching decimals like "0.8".
    escaped = re.sub(r'(?<=.)(\d+\. )', r'<br>\1', escaped)
    # Chinese enumeration comma: "1、" "2、"
    escaped = re.sub(r'(?<=.)(\d+、)', r'<br>\1', escaped)
    # Insert <br> before circled-number steps: ① ② ③ etc.
    escaped = re.sub(r'(?<=.)([①②③④⑤⑥⑦⑧⑨⑩])', r'<br>\1', escaped)
    return escaped


def lttb_downsample(x: np.ndarray, y: np.ndarray,
                    threshold: int) -> tuple[np.ndarray, np.ndarray]:
    """
    Largest Triangle Three Buckets (LTTB) downsampling.

    Reduces the number of data points while preserving visual shape.
    Peaks and valleys are retained, making the downsampled plot
    visually indistinguishable from the original.

    Uses numpy vectorized operations for the inner loop.

    Args:
        x: x-axis values (e.g. timestamps)
        y: y-axis values
        threshold: target number of output points

    Returns:
        (x_out, y_out) downsampled arrays
    """
    n = len(x)
    if n <= threshold or threshold < 3:
        return x, y

    out_idx = np.empty(threshold, dtype=np.int64)
    out_idx[0] = 0
    out_idx[-1] = n - 1

    bucket_size = (n - 2) / (threshold - 2)
    a_idx = 0

    for i in range(1, threshold - 1):
        bucket_start = int((i - 1) * bucket_size) + 1
        bucket_end = min(int(i * bucket_size) + 1, n)
        next_start = int(i * bucket_size) + 1
        next_end = min(int((i + 1) * bucket_size) + 1, n - 1)

        avg_x = np.mean(x[next_start:next_end + 1])
        avg_y = np.mean(y[next_start:next_end + 1])

        # Vectorized area calculation for all points in current bucket
        bx = x[bucket_start:bucket_end]
        by = y[bucket_start:bucket_end]
        ax, ay = x[a_idx], y[a_idx]
        areas = np.abs((ax - avg_x) * (by - ay) - (ax - bx) * (avg_y - ay))

        max_idx = bucket_start + np.argmax(areas)
        out_idx[i] = max_idx
        a_idx = max_idx

    return x[out_idx], y[out_idx]


def downsample_figure(fig, max_points: int = MAX_POINTS_PER_TRACE):
    """
    Downsample all scatter/scattergl traces in a plotly Figure in-place.
    Only affects traces with more than max_points data points.
    Histogram, bar, and other non-scatter traces are left untouched.
    """
    for trace in fig.data:
        trace_type = getattr(trace, 'type', '')
        if trace_type not in ('scatter', 'scattergl'):
            continue

        y = getattr(trace, 'y', None)
        if y is None:
            continue

        y_arr = np.asarray(y, dtype=float)
        n = len(y_arr)
        if n <= max_points:
            continue

        x = getattr(trace, 'x', None)
        if x is None:
            x_arr = np.arange(n, dtype=float)
        else:
            x_arr = np.asarray(x, dtype=float)
            # Plotly draws a trace as far as its shorter array goes. Past
            # that, downsampling indexed off the end of the other one, and
            # one such figure cost the whole report.
            if len(x_arr) != n:
                n = min(n, len(x_arr))
                x_arr, y_arr = x_arr[:n], y_arr[:n]

        # Handle NaN: LTTB doesn't work with NaN, so mask them
        valid = np.isfinite(y_arr)
        if not np.all(valid):
            # For traces with NaN gaps, downsample valid segments
            x_arr = x_arr[valid]
            y_arr = y_arr[valid]
            n = len(y_arr)
            if n <= max_points:
                trace.x = x_arr
                trace.y = y_arr
                continue

        new_x, new_y = lttb_downsample(x_arr, y_arr, max_points)
        trace.x = new_x
        trace.y = new_y

    # After downsampling to ≤5000 points, switch from WebGL (scattergl)
    # to SVG (scatter). SVG renders reliably without WebGL timing issues
    # and 5000 points is well within SVG performance limits.
    has_gl = any(getattr(t, 'type', '') == 'scattergl' for t in fig.data)
    if has_gl:
        import plotly.graph_objects as go_mod
        new_fig = go_mod.Figure()
        for trace in fig.data:
            props = trace.to_plotly_json()
            if props.get('type') == 'scattergl':
                props['type'] = 'scatter'
            new_fig.add_trace(props)
        new_fig.layout = fig.layout
        # Replace figure contents in-place
        fig.__dict__.update(new_fig.__dict__)


# Status -> color mapping
STATUS_COLORS = {
    'good': '#5cb85c',
    'warning': '#f0ad4e',
    'critical': '#d9534f',
    'info': '#5bc0de',
}

# A failed analyzer has no verdict, so none of the status colours fit.
FAILED_COLOR = '#6f42c1'


def _status_label(status: str) -> str:
    return t(f"status.{status}")


def _result_style(result: AnalysisResult) -> tuple[str, str]:
    """Colour and label of a result card."""
    if result.failed:
        return FAILED_COLOR, t("status.failed")
    color = STATUS_COLORS.get(result.status, STATUS_COLORS['info'])
    return color, _status_label(result.status)


def _action_status_tag(status: str) -> str:
    """Mark an action item that comes from a warning or critical result."""
    if status not in ("warning", "critical"):
        return ""
    return (f' <span class="status-badge" style="background-color: '
            f'{STATUS_COLORS[status]};">{_status_label(status)}</span>')


class HtmlReportGenerator:
    """
    Generates a standalone HTML flight analysis report.

    Usage:
        gen = HtmlReportGenerator(vehicle_info, results)
        gen.write("report.html")
    """

    def __init__(self, vehicle_info: VehicleInfo,
                 results: list[AnalysisResult],
                 log_filename: str = "",
                 flight_duration: float = 0.0,
                 health_score: int = -1,
                 gps_track: Optional[dict] = None,
                 max_trace_points: int = 2000,
                 static_images: bool = False):
        self.vehicle = vehicle_info
        self.results = results
        self.log_filename = log_filename
        self.flight_duration = flight_duration
        self.health_score = health_score
        # gps_track: {"lat": [...], "lng": [...], "alt": [...], "time_s": [...]}
        self.gps_track = gps_track
        self.max_trace_points = max_trace_points
        self.static_images = static_images

    def generate(self) -> str:
        """Generate the full HTML report as a string."""
        plotly_js = self._get_plotly_js()
        # Build the executive summary first; technical sections render below.
        self.executive = build_executive_summary(
            self.results,
            vehicle_info=self.vehicle,
            health_score=self.health_score,
            flight_duration=self.flight_duration,
        )

        executive_html = self._render_executive_summary()
        sections_html = self._render_sections()
        dashboard_html = self._render_dashboard()
        toc_html = self._render_toc()
        param_export_html = self._render_param_export()
        map_html = self._render_map()
        feedback_html = self._render_feedback()

        return f"""<!DOCTYPE html>
<html lang="{t('_lang_code') if t('_lang_code') != '_lang_code' else 'zh-CN'}">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{t('report.page_title', filename=html.escape(self.log_filename))}</title>
{plotly_js}
{self._get_leaflet_head() if self.gps_track else ''}
<style>
{self._get_css()}
</style>
</head>
<body>

{self._render_header()}

<div class="container">
{executive_html}
{dashboard_html}
{toc_html}
{sections_html}
{param_export_html}
{feedback_html}
</div>

{map_html}

<footer class="footer">
    <p>{t('report.footer', timestamp=datetime.now().strftime('%Y-%m-%d %H:%M:%S'))}</p>
</footer>

</body>
</html>"""

    def write(self, filepath: str):
        """Write the HTML report to a file."""
        content = self.generate()
        os.makedirs(os.path.dirname(os.path.abspath(filepath)) or '.', exist_ok=True)
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(content)

    # ------------------------------------------------------------------
    # Private rendering methods
    # ------------------------------------------------------------------

    @staticmethod
    def _get_plotly_js() -> str:
        """plotly.js as an inline script.

        Reports get opened in the field with no network, and a CDN copy
        could not make them smaller: the fallback has to be embedded
        either way.
        """
        return f"<script>{plotly.offline.get_plotlyjs()}</script>"

    def _render_header(self) -> str:
        """Render the dark header bar."""
        duration_str = self._format_duration(self.flight_duration)
        return f"""
<header class="header">
    <div class="header-content">
        <div style="display:flex;justify-content:space-between;align-items:flex-start">
            <h1>{t('report.title')}</h1>
            <button class="print-btn" onclick="window.print()">{t('report.export_pdf')}</button>
        </div>
        <div class="header-info">
            <span class="tag">{t('report.tag_log', filename=html.escape(self.log_filename))}</span>
            <span class="tag">{html.escape(self.vehicle.vehicle_type)} {html.escape(self.vehicle.firmware_version)}</span>
            <span class="tag">{t('report.tag_duration', duration=duration_str)}</span>
            <span class="tag">{t('report.tag_board', board=html.escape(self.vehicle.board_name))}</span>
        </div>
    </div>
</header>"""

    def _render_executive_summary(self) -> str:
        """Render the executive summary card at the top of the report."""
        es = self.executive
        level_display = get_level_display(es.overall_action_level)
        level_icon = level_display["icon"]
        level_label = level_display["label"]
        css_class = level_display["css_class"]

        # Action items (one card per non-OK analyzer)
        if es.actions:
            items_html = []
            for item in es.actions:
                item_display = get_level_display(item.level)
                items_html.append(f"""
                <div class="exec-action-item exec-action-{item_display['css_class']}">
                    <div class="exec-action-icon">{item.icon}</div>
                    <div class="exec-action-body">
                        <div class="exec-action-title">{html.escape(item.title)}{_action_status_tag(item.source_status)}</div>
                        <div class="exec-action-summary">{html.escape(item.plain_summary)}</div>
                        <div class="exec-action-text">{_format_action_text(item.action_text)}</div>
                    </div>
                </div>""")
            actions_html = '<div class="exec-actions">' + ''.join(items_html) + '</div>'
        else:
            actions_html = ''

        # Counts pill (for engineer view)
        counts = es.counts or {}
        pills = []
        for level_key in (
            "safety_stop", "apply_param", "physical_check",
            "run_autotune", "ok",
        ):
            n = counts.get(level_key, 0)
            if n == 0:
                continue
            d = get_level_display(level_key)
            pills.append(
                f'<span class="exec-pill exec-pill-{d["css_class"]}">'
                f'{d["icon"]} {d["label"]} × {n}</span>'
            )
        pills_html = '<div class="exec-pills">' + ''.join(pills) + '</div>' \
            if pills else ''

        note_text = t('report.intro')

        return f"""
<section class="exec-summary {css_class}">
    <div class="exec-summary-header">
        <div class="exec-summary-icon">{level_icon}</div>
        <div class="exec-summary-text">
            <div class="exec-summary-label">{t('report.conclusion_label', level=level_label)}</div>
            <h2 class="exec-summary-headline">{html.escape(es.headline)}</h2>
        </div>
    </div>
    {pills_html}
    {actions_html}
    <div class="exec-summary-note">
        {note_text}
    </div>
</section>"""

    def _render_dashboard(self) -> str:
        """Render the health dashboard with colored status circles."""
        circles = []
        for result in self.results:
            color, label = _result_style(result)
            circles.append(f"""
            <div class="dash-item">
                <div class="status-circle" style="background-color: {color};"
                     title="{html.escape(result.title)}: {label}"></div>
                <span class="dash-label">{html.escape(result.title)}</span>
            </div>""")

        # Health score badge
        score_html = ""
        if self.health_score >= 0:
            if self.health_score >= 80:
                score_color = "#5cb85c"
            elif self.health_score >= 50:
                score_color = "#f0ad4e"
            else:
                score_color = "#d9534f"
            score_html = f"""
    <div class="score-badge" style="border-color: {score_color};">
        <div class="score-number" style="color: {score_color};">{self.health_score}</div>
        <div class="score-label">{t('report.score_label')}</div>
    </div>"""

        return f"""
<section class="dashboard">
    <h2>{t('report.health_overview')}</h2>
    <div class="dash-flex">
        {score_html}
        <div class="dash-grid">
            {''.join(circles)}
        </div>
    </div>
</section>"""

    def _render_toc(self) -> str:
        """Render table of contents."""
        items = []
        for i, result in enumerate(self.results):
            color, _ = _result_style(result)
            items.append(
                f'<li><a href="#section-{i}">'
                f'<span class="toc-dot" style="background-color: {color};"></span>'
                f'{html.escape(result.title)}</a></li>'
            )
        return f"""
<nav class="toc">
    <h2>{t('report.toc')}</h2>
    <ul>{''.join(items)}</ul>
</nav>"""

    def _render_sections(self) -> str:
        """Render all analysis sections."""
        parts = []
        for i, result in enumerate(self.results):
            parts.append(self._render_section(result, str(i)))
        return '\n'.join(parts)

    def _render_section(self, result: AnalysisResult, anchor: str) -> str:
        """Render a single analysis section as a card."""
        color, label = _result_style(result)

        # Status badge
        badge = (
            f'<span class="status-badge" '
            f'style="background-color: {color};">{label}</span>'
        )

        # Summary
        summary_html = ""
        if result.summary:
            summary_html = f'<p class="summary">{html.escape(result.summary)}</p>'

        # Evidence source (POSITIONING.md: every warning/critical must cite)
        evidence_source_html = ""
        if result.evidence_source and result.status in ("warning", "critical"):
            evidence_source_html = (
                f'<p class="evidence-source">'
                f'<span class="evidence-source-label">{t("report.evidence_label")}</span>'
                f'<span class="evidence-source-text">'
                f'{html.escape(result.evidence_source)}</span></p>'
            )

        # Details
        details_html = ""
        if result.details:
            detail_items = ''.join(
                f'<li>{_safe_detail(d)}</li>' for d in result.details
            )
            details_html = f"""
            <details class="detail-fold">
                <summary>{t('report.details_toggle', count=len(result.details))}</summary>
                <ul class="detail-list">{detail_items}</ul>
            </details>"""

        # Figures (with LTTB downsampling for large traces)
        figures_html = ""
        for fig in result.figures:
            downsample_figure(fig, max_points=self.max_trace_points)
            if self.static_images:
                fig_html = self._render_static_figure(fig)
            else:
                fig_html = pio.to_html(
                    fig, include_plotlyjs=False, full_html=False,
                    config={'responsive': True, 'displayModeBar': True,
                            'modeBarButtonsToRemove': ['lasso2d', 'select2d'],
                            'displaylogo': False}
                )
            figures_html += f'<div class="figure-container">{fig_html}</div>\n'

        # Sub-results
        sub_html = ""
        if result.sub_results:
            sub_parts = []
            for j, sub in enumerate(result.sub_results):
                sub_parts.append(self._render_section(sub, f"{anchor}-{j}"))
            sub_html = '<div class="sub-results">' + '\n'.join(sub_parts) + '</div>'

        return f"""
<section id="section-{anchor}" class="card" style="border-left-color: {color};">
    <div class="card-header">
        <h3>{html.escape(result.title)} {badge}</h3>
    </div>
    <div class="card-body">
        {summary_html}
        {evidence_source_html}
        {details_html}
        {figures_html}
        {sub_html}
    </div>
</section>"""

    @staticmethod
    def _render_static_figure(fig) -> str:
        """Render a Plotly figure as a static PNG ``<img>`` tag.

        Uses ``plotly.io.to_image()`` with the kaleido engine. Raises
        ChartRenderError on failure: the next figure would fail the same
        way, seconds later, and a PDF without its charts reads as complete.
        """
        import base64
        try:
            img_bytes = pio.to_image(fig, format="png", width=800, height=400)
        except Exception as e:
            raise ChartRenderError(" ".join(str(e).split())) from e
        b64 = base64.b64encode(img_bytes).decode()
        return (
            f'<img src="data:image/png;base64,{b64}" '
            f'style="width:100%;max-width:800px" '
            f'alt="chart">'
        )

    # ------------------------------------------------------------------
    # Map (Leaflet + Plotly hover sync)
    # ------------------------------------------------------------------

    @staticmethod
    def _get_leaflet_head() -> str:
        """Leaflet as inline tags, so the map code needs no network."""
        with open(os.path.join(_LEAFLET_DIR, "leaflet.css"),
                  encoding="utf-8") as f:
            css = f.read()
        with open(os.path.join(_LEAFLET_DIR, "leaflet.js"),
                  encoding="utf-8") as f:
            js = f.read()
        return f"<style>{css}</style>\n<script>{js}</script>"

    def _render_param_export(self) -> str:
        """
        Collect param_suggestions from all results and render a unified
        parameter recommendation table + copyable .param text block.

        Copy the .param block into Mission Planner's Full Parameter List.
        """
        unique, conflicts = merge_suggestions(self.results)
        if not unique and not conflicts:
            return ""

        # Conflict warning box (rendered above the table)
        conflict_html = ""
        if conflicts:
            conflict_items = []
            for c in conflicts:
                # Show the first two disagreeing sources as representative
                s1, s2 = distinct_values(c)[:2]
                row_text = t("param_conflict.row",
                             param=c["param"],
                             source1=s1["source"], val1=s1["value"],
                             source2=s2["source"], val2=s2["value"])
                conflict_items.append(
                    f"<li>{html.escape(row_text)}</li>"
                )
            conflict_html = (
                f'<div style="background:#fff3cd; border:1px solid #ffc107;'
                f' border-radius:6px; padding:12px 16px; margin-bottom:1em;">'
                f'<strong style="color:#856404;">'
                f'{t("param_conflict.title")}</strong>'
                f'<p style="color:#856404; margin:6px 0 8px 0;">'
                f'{t("param_conflict.warning")}</p>'
                f'<ul style="color:#856404; margin:0; padding-left:20px;">'
                f'{"".join(conflict_items)}</ul></div>'
            )

        # Build table rows
        rows = ""
        for s in unique:
            current = s.get("current", "")
            suggested = s.get("suggested", "")
            reason = html.escape(str(s.get("reason", "")))
            source = html.escape(str(s.get("source", "")))
            rows += (
                f"<tr>"
                f"<td><code>{html.escape(str(s['param']))}</code></td>"
                f"<td>{html.escape(str(current))}</td>"
                f"<td><b>{html.escape(str(suggested))}</b></td>"
                f"<td>{reason}</td>"
                f"<td>{source}</td>"
                f"</tr>\n"
            )

        # Build .param text (Mission Planner format: NAME,VALUE)
        param_lines = []
        for s in unique:
            suggested = s.get("suggested", "")
            if suggested not in (None, "", "-"):
                param_lines.append(f"{s['param']},{suggested}")
        param_text = "\n".join(param_lines)

        return f"""
<div class="section" id="param-export"
     style="margin-top:2em; border:2px solid #4472c4; border-radius:8px; padding:1.5em;">
  <h2 style="color:#4472c4; margin-top:0;">
    ⚙️ {t('report.param_title', count=len(unique))}
  </h2>
  <p style="color:#666;">
    {t('report.param_instruction')}
  </p>
  {conflict_html}
  <table style="width:100%; border-collapse:collapse; margin:1em 0;">
    <thead>
      <tr style="background:#4472c4; color:white;">
        <th style="padding:8px; text-align:left;">{t('report.param_col_name')}</th>
        <th style="padding:8px; text-align:left;">{t('report.param_col_current')}</th>
        <th style="padding:8px; text-align:left;">{t('report.param_col_suggested')}</th>
        <th style="padding:8px; text-align:left;">{t('report.param_col_reason')}</th>
        <th style="padding:8px; text-align:left;">{t('report.param_col_source')}</th>
      </tr>
    </thead>
    <tbody>
      {rows}
    </tbody>
  </table>
  <details>
    <summary style="cursor:pointer; color:#4472c4; font-weight:bold;">
      📋 {t('report.param_copyable')}
    </summary>
    <pre style="background:#f5f5f5; padding:1em; border-radius:4px; margin-top:0.5em;
                user-select:all; font-size:13px;">{html.escape(param_text)}</pre>
    <p style="color:#999; font-size:12px;">
      {t('report.param_copy_hint')}
    </p>
  </details>
</div>
"""

    def _render_feedback(self) -> str:
        """
        Render the in-report feedback channel.

        Pre-fills a structured message with the report context
        (filename / time / vehicle) for copy-paste feedback.
        """
        ctx_lines = [
            t('report.feedback_ctx_log', filename=self.log_filename),
            t('report.feedback_ctx_vehicle', vehicle=self.vehicle.vehicle_type),
            t('report.feedback_ctx_duration', duration=f"{self.flight_duration:.0f}"),
            t('report.feedback_ctx_score', score=self.health_score if self.health_score >= 0 else 'N/A'),
            t('report.feedback_ctx_version', version=_get_version(), date=datetime.now().strftime('%Y-%m-%d')),
        ]
        prefill = "\n".join([
            t('report.feedback_prefill_header'),
            "",
            *ctx_lines,
            "",
            t('report.feedback_prefill_issue'),
            t('report.feedback_prefill_placeholder'),
            "",
            t('report.feedback_prefill_expect'),
            "",
        ])
        # Use HTML attribute escaping (quote=True) for the data-prefill
        # value so any special characters in the filename or vehicle
        # type can't break out of the attribute.
        prefill_attr = html.escape(prefill, quote=True)
        return f"""
<div class="feedback-section">
    <h3>📮 {t('report.feedback_title')}</h3>
    <p class="feedback-hint">
        {t('report.feedback_desc')}
    </p>
    <textarea id="feedback-text">{prefill_attr}</textarea>
    <div class="feedback-actions">
        <button onclick="(function(){{
            var ta = document.getElementById('feedback-text');
            var s = document.getElementById('feedback-status');
            function done() {{
                s.textContent = '{t("report.feedback_copied")}';
                s.classList.add('shown');
                setTimeout(function(){{ s.classList.remove('shown'); }}, 4000);
            }}
            function legacy() {{
                ta.select();
                if (document.execCommand('copy')) done();
            }}
            ta.select();
            if (navigator.clipboard) navigator.clipboard.writeText(ta.value).then(done, legacy);
            else legacy();
        }})()\">{t('report.feedback_copy_btn')}</button>
        <button data-prefill="{prefill_attr}" onclick="document.getElementById('feedback-text').value = this.dataset.prefill" style="background:#999;">{t('report.feedback_reset_btn')}</button>
        <span id="feedback-status" class="feedback-status"></span>
    </div>
</div>
"""

    def _render_map(self) -> str:
        """Render the floating map panel with hover sync JS."""
        if not self.gps_track:
            return ""
        track = self.gps_track
        if len(track.get("lat", [])) < 2:
            return ""

        import json as _json
        track_json = _json.dumps({
            "lat": [round(v, 7) for v in track["lat"]],
            "lng": [round(v, 7) for v in track["lng"]],
            "alt": [round(v, 1) for v in track["alt"]],
            "time_s": [round(v, 2) for v in track["time_s"]],
        })

        # Note: all data here is generated server-side from the log file
        # (trusted source), not from user input, so inline rendering is safe.
        return f"""
<!-- Floating map panel -->
<button class="map-toggle-btn" id="map-toggle" onclick="toggleMap()">
    &#x1F5FA; {t('report.map_title')}
</button>
<div class="map-panel" id="map-panel">
    <div class="map-panel-header">
        <span>{t('report.map_subtitle')}</span>
        <button onclick="toggleMap()" style="background:none;border:none;
                color:#999;cursor:pointer;font-size:16px">&times;</button>
    </div>
    <div id="flight-map" style="width:100%;height:calc(100% - 28px)"></div>
    <div id="map-tile-notice" class="map-tile-notice">{t('report.map_tiles_offline')}</div>
</div>

<script>
(function() {{
    var GPS = {track_json};
    var mapVisible = false;
    var map = null;
    var marker = null;
    var polyline = null;

    window.toggleMap = function() {{
        var panel = document.getElementById('map-panel');
        mapVisible = !mapVisible;
        panel.style.display = mapVisible ? 'flex' : 'none';
        if (mapVisible && !map) initMap();
        if (map) map.invalidateSize();
    }};

    function initMap() {{
        var coords = [];
        for (var i = 0; i < GPS.lat.length; i++) {{
            coords.push([GPS.lat[i], GPS.lng[i]]);
        }}
        map = L.map('flight-map', {{zoomControl: true}});
        L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
            maxZoom: 19,
            attribution: '&copy; OpenStreetMap'
        }}).on('tileerror', function() {{
            document.getElementById('map-tile-notice').style.display = 'block';
        }}).addTo(map);
        polyline = L.polyline(coords, {{
            color: '#409eff', weight: 2.5, opacity: 0.8
        }}).addTo(map);
        map.fitBounds(polyline.getBounds().pad(0.1));
        marker = L.circleMarker(coords[0], {{
            radius: 7, color: '#d9534f', fillColor: '#d9534f',
            fillOpacity: 0.9, weight: 2
        }}).addTo(map);
        L.circleMarker(coords[0], {{
            radius: 5, color: '#5cb85c', fillColor: '#5cb85c',
            fillOpacity: 1, weight: 0
        }}).addTo(map).bindTooltip('{t("report.map_start")}');
        L.circleMarker(coords[coords.length - 1], {{
            radius: 5, color: '#d9534f', fillColor: '#d9534f',
            fillOpacity: 1, weight: 0
        }}).addTo(map).bindTooltip('{t("report.map_end")}');
    }}

    function findTimeIndex(t) {{
        var arr = GPS.time_s;
        var lo = 0, hi = arr.length - 1;
        while (lo < hi) {{
            var mid = (lo + hi) >> 1;
            if (arr[mid] < t) lo = mid + 1;
            else hi = mid;
        }}
        if (lo > 0 && Math.abs(arr[lo-1] - t) < Math.abs(arr[lo] - t))
            lo = lo - 1;
        return lo;
    }}

    window.updateMapPosition = function(timeVal) {{
        if (!map || !marker) return;
        var idx = findTimeIndex(timeVal);
        var lat = GPS.lat[idx], lng = GPS.lng[idx];
        marker.setLatLng([lat, lng]);
        marker.setTooltipContent(
            'T=' + GPS.time_s[idx].toFixed(1) + 's  Alt=' + GPS.alt[idx].toFixed(0) + 'm'
        );
        if (!marker.getTooltip()) marker.bindTooltip('');
    }};

    function attachHoverListeners() {{
        var plots = document.querySelectorAll('.js-plotly-plot');
        plots.forEach(function(el) {{
            el.on('plotly_hover', function(data) {{
                if (data.points && data.points.length > 0) {{
                    var x = data.points[0].x;
                    if (typeof x === 'number') {{
                        updateMapPosition(x);
                        if (!mapVisible) toggleMap();
                    }}
                }}
            }});
        }});
    }}

    if (document.readyState === 'loading') {{
        document.addEventListener('DOMContentLoaded', function() {{
            setTimeout(attachHoverListeners, 200);
        }});
    }} else {{
        setTimeout(attachHoverListeners, 200);
    }}
}})();
</script>"""

    @staticmethod
    def _format_duration(seconds: float) -> str:
        """Format duration in seconds to human-readable string."""
        if seconds <= 0:
            return "N/A"
        m, s = divmod(int(seconds), 60)
        h, m = divmod(m, 60)
        if h > 0:
            return f"{h}h {m}m {s}s"
        elif m > 0:
            return f"{m}m {s}s"
        else:
            return f"{s}s"

    @staticmethod
    def _get_css() -> str:
        """Return the embedded CSS stylesheet."""
        return """
/* Base */
* { margin: 0; padding: 0; box-sizing: border-box; }
body {
    font-family: "PingFang SC", "Microsoft YaHei", "Noto Sans SC",
                 -apple-system, BlinkMacSystemFont, sans-serif;
    background: #f5f6fa;
    color: #333;
    line-height: 1.6;
}

/* Header */
.header {
    background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%);
    color: #fff;
    padding: 24px 0;
}
.header-content {
    max-width: 1200px;
    margin: 0 auto;
    padding: 0 24px;
}
.header h1 {
    font-size: 24px;
    margin-bottom: 12px;
}
.header-info {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
}
.tag {
    background: rgba(255,255,255,0.15);
    padding: 4px 12px;
    border-radius: 4px;
    font-size: 13px;
}

/* Container */
.container {
    max-width: 1200px;
    margin: 0 auto;
    padding: 24px;
}

/* Executive summary card */
.exec-summary {
    background: #fff;
    border-radius: 8px;
    padding: 24px;
    margin-bottom: 24px;
    box-shadow: 0 2px 8px rgba(0,0,0,0.08);
    border-left: 6px solid #5cb85c;
}
.exec-summary.level-safety-stop    { border-left-color: #d9534f; background: #fff5f5; }
.exec-summary.level-apply-param    { border-left-color: #4a90e2; background: #f0f6fc; }
.exec-summary.level-physical-check { border-left-color: #f0ad4e; background: #fffaf0; }
.exec-summary.level-run-autotune   { border-left-color: #9b59b6; background: #faf3fc; }
.exec-summary.level-ok             { border-left-color: #5cb85c; }
/* Legacy v1 classes (kept for backward compatibility) */
.exec-summary.level-stop-flying { border-left-color: #d9534f; background: #fff5f5; }
.exec-summary.level-consult     { border-left-color: #f0ad4e; background: #fffaf0; }
.exec-summary.level-self-check  { border-left-color: #5bc0de; background: #f0faff; }

.exec-summary-header {
    display: flex;
    align-items: center;
    gap: 16px;
    margin-bottom: 12px;
}
.exec-summary-icon {
    font-size: 48px;
    line-height: 1;
}
.exec-summary-text {
    flex: 1;
}
.exec-summary-label {
    font-size: 13px;
    color: #888;
    margin-bottom: 4px;
}
.exec-summary-headline {
    font-size: 20px;
    margin: 0;
    color: #222;
    font-weight: 600;
}

.exec-pills {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    margin: 12px 0;
}
.exec-pill {
    padding: 4px 12px;
    border-radius: 12px;
    font-size: 13px;
    background: #f0f0f0;
}
.exec-pill-level-safety-stop    { background: #ffd5d5; color: #8b0000; }
.exec-pill-level-apply-param    { background: #d6e8f9; color: #1a4f80; }
.exec-pill-level-physical-check { background: #ffe9c4; color: #8b6914; }
.exec-pill-level-run-autotune   { background: #ead4f4; color: #5c2980; }
.exec-pill-level-ok             { background: #d6f0d6; color: #2d6a2d; }
/* Legacy v1 (backward compat) */
.exec-pill-level-stop-flying { background: #ffd5d5; color: #8b0000; }
.exec-pill-level-consult     { background: #ffe9c4; color: #8b6914; }
.exec-pill-level-self-check  { background: #d4ecf4; color: #1f5066; }

.exec-actions {
    margin: 16px 0 12px 0;
    display: flex;
    flex-direction: column;
    gap: 12px;
}
.exec-action-item {
    display: flex;
    gap: 12px;
    padding: 12px 14px;
    border-radius: 6px;
    background: #fafafa;
    border-left: 3px solid #ccc;
}
.exec-action-level-safety-stop    { border-left-color: #d9534f; background: #fff0f0; }
.exec-action-level-apply-param    { border-left-color: #4a90e2; background: #f3f8fd; }
.exec-action-level-physical-check { border-left-color: #f0ad4e; background: #fffaf0; }
.exec-action-level-run-autotune   { border-left-color: #9b59b6; background: #fbf6fd; }
/* Legacy v1 */
.exec-action-level-stop-flying { border-left-color: #d9534f; background: #fff0f0; }
.exec-action-level-consult     { border-left-color: #f0ad4e; background: #fffaf0; }
.exec-action-level-self-check  { border-left-color: #5bc0de; background: #f3faff; }

.exec-action-icon {
    font-size: 22px;
    line-height: 1.4;
    flex-shrink: 0;
}
.exec-action-body { flex: 1; }
.exec-action-title {
    font-weight: 600;
    color: #222;
    margin-bottom: 4px;
}
.exec-action-summary {
    color: #444;
    font-size: 14px;
    margin-bottom: 4px;
}
.exec-action-text {
    color: #666;
    font-size: 13px;
    font-style: italic;
}
.exec-summary-note {
    margin-top: 14px;
    padding-top: 12px;
    border-top: 1px dashed #e0e0e0;
    font-size: 12px;
    color: #888;
    text-align: center;
}

/* Dashboard */
.dashboard {
    background: #fff;
    border-radius: 8px;
    padding: 20px;
    margin-bottom: 20px;
    box-shadow: 0 1px 3px rgba(0,0,0,0.1);
}
.dashboard h2 {
    font-size: 16px;
    margin-bottom: 16px;
    color: #555;
}
.dash-flex {
    display: flex;
    align-items: center;
    gap: 24px;
}
.score-badge {
    flex-shrink: 0;
    width: 80px;
    height: 80px;
    border-radius: 50%;
    border: 4px solid #5cb85c;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
}
.score-number {
    font-size: 28px;
    font-weight: 700;
    line-height: 1;
}
.score-label {
    font-size: 10px;
    color: #999;
    margin-top: 2px;
}
.dash-grid {
    display: flex;
    flex-wrap: wrap;
    gap: 20px;
}
.dash-item {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 6px;
    min-width: 70px;
}
.status-circle {
    width: 20px;
    height: 20px;
    border-radius: 50%;
    box-shadow: 0 0 6px rgba(0,0,0,0.15);
}
.dash-label {
    font-size: 12px;
    color: #666;
    text-align: center;
}

/* TOC */
.toc {
    background: #fff;
    border-radius: 8px;
    padding: 16px 20px;
    margin-bottom: 20px;
    box-shadow: 0 1px 3px rgba(0,0,0,0.1);
    position: sticky;
    top: 0;
    z-index: 100;
    max-height: 50vh;
    overflow-y: auto;
}
.toc h2 {
    font-size: 16px;
    margin-bottom: 10px;
    color: #555;
}
.toc ul {
    list-style: none;
    display: flex;
    flex-wrap: wrap;
    gap: 6px 16px;
}
.toc a {
    display: flex;
    align-items: center;
    gap: 6px;
    text-decoration: none;
    color: #333;
    font-size: 13px;
    padding: 2px 0;
}
.toc a:hover { color: #0077cc; }
.toc-dot {
    display: inline-block;
    width: 8px;
    height: 8px;
    border-radius: 50%;
    flex-shrink: 0;
}

/* Card sections */
.card {
    background: #fff;
    border-radius: 8px;
    margin-bottom: 20px;
    box-shadow: 0 1px 3px rgba(0,0,0,0.1);
    border-left: 4px solid #5bc0de;
    overflow: hidden;
}
.card-header {
    padding: 14px 20px;
    border-bottom: 1px solid #f0f0f0;
}
.card-header h3 {
    font-size: 16px;
    display: flex;
    align-items: center;
    gap: 10px;
}
.card-body {
    padding: 16px 20px;
}
.summary {
    font-size: 14px;
    color: #555;
    margin-bottom: 12px;
}

/* Evidence source — cite the authority behind warning/critical */
.evidence-source {
    font-size: 12px;
    margin: 8px 0 12px 0;
    padding: 6px 10px;
    background: #fafafa;
    border-left: 3px solid #c9c9c9;
    color: #666;
}
.evidence-source-label {
    font-weight: 600;
    color: #444;
}
.evidence-source-text {
    font-family: 'Consolas', 'Monaco', monospace;
    color: #555;
}

/* Status badge */
.status-badge {
    display: inline-block;
    color: #fff;
    font-size: 11px;
    padding: 2px 8px;
    border-radius: 3px;
    font-weight: normal;
}

/* Detail fold */
.detail-fold {
    margin-top: 10px;
}
.detail-fold summary {
    cursor: pointer;
    font-size: 13px;
    color: #777;
    margin-bottom: 6px;
}
.detail-list {
    list-style: disc;
    padding-left: 20px;
    font-size: 13px;
    color: #555;
}
.detail-list li { margin-bottom: 4px; }

/* Figures */
.figure-container {
    margin: 12px 0;
}

/* Sub results */
.sub-results {
    margin-top: 12px;
}
.sub-results .card {
    border-left-width: 3px;
    margin-bottom: 12px;
}

/* Footer */
.footer {
    text-align: center;
    padding: 20px;
    color: #999;
    font-size: 12px;
}

/* Feedback channel */
.feedback-section {
    background: #fff;
    border: 1px solid #e0e0e0;
    border-radius: 8px;
    padding: 20px;
    margin: 24px 0;
    box-shadow: 0 1px 3px rgba(0,0,0,0.06);
}
.feedback-section h3 {
    font-size: 16px;
    margin: 0 0 8px 0;
    color: #333;
}
.feedback-section p.feedback-hint {
    font-size: 13px;
    color: #666;
    margin: 0 0 12px 0;
    line-height: 1.6;
}
.feedback-section textarea {
    width: 100%;
    min-height: 100px;
    padding: 10px 12px;
    font-family: inherit;
    font-size: 13px;
    border: 1px solid #ccc;
    border-radius: 4px;
    box-sizing: border-box;
    resize: vertical;
}
.feedback-section .feedback-actions {
    display: flex;
    gap: 8px;
    margin-top: 10px;
    align-items: center;
}
.feedback-section button {
    background: #5cb85c;
    color: #fff;
    border: none;
    padding: 8px 18px;
    font-size: 13px;
    border-radius: 4px;
    cursor: pointer;
    font-family: inherit;
}
.feedback-section button:hover { background: #4cae4c; }
.feedback-section .feedback-status {
    font-size: 12px;
    color: #5cb85c;
    margin-left: 8px;
    transition: opacity .3s;
    opacity: 0;
}
.feedback-section .feedback-status.shown { opacity: 1; }
@media print {
    .feedback-section { display: none !important; }
    .toc { position: static; }
}

/* Print / PDF export button */
.print-btn {
    background: rgba(255,255,255,0.2);
    color: #fff;
    border: 1px solid rgba(255,255,255,0.3);
    padding: 6px 16px;
    border-radius: 4px;
    font-size: 13px;
    cursor: pointer;
    transition: background .2s;
    flex-shrink: 0;
}
.print-btn:hover { background: rgba(255,255,255,0.3); }

/* Print styles */
@media print {
    body { background: #fff; }
    .header { background: #1a1a2e !important; -webkit-print-color-adjust: exact; print-color-adjust: exact; }
    .print-btn, .map-toggle-btn, .map-panel { display: none !important; }
    .container { max-width: 100%; padding: 0 12px; }
    .card { break-inside: avoid; box-shadow: none; border: 1px solid #ddd; }
    .dashboard, .toc { break-inside: avoid; box-shadow: none; }
    .figure-container { break-inside: avoid; }
    .detail-fold[open] { break-inside: avoid; }
    .footer { margin-top: 20px; }
    .status-circle, .toc-dot, .score-badge, .status-badge {
        -webkit-print-color-adjust: exact; print-color-adjust: exact;
    }
}

/* Floating map panel */
.map-toggle-btn {
    position: fixed;
    bottom: 20px;
    right: 20px;
    z-index: 1001;
    background: #409eff;
    color: #fff;
    border: none;
    border-radius: 8px;
    padding: 8px 16px;
    font-size: 14px;
    cursor: pointer;
    box-shadow: 0 2px 12px rgba(0,0,0,0.2);
    transition: background .2s;
}
.map-toggle-btn:hover { background: #337ecc; }
.map-panel {
    display: none;
    flex-direction: column;
    position: fixed;
    bottom: 60px;
    right: 20px;
    width: 420px;
    height: 320px;
    z-index: 1000;
    background: #fff;
    border-radius: 8px;
    box-shadow: 0 4px 20px rgba(0,0,0,0.25);
    overflow: hidden;
}
.map-tile-notice {
    display: none;
    position: absolute;
    left: 8px;
    right: 8px;
    bottom: 22px;
    z-index: 1000;
    padding: 4px 8px;
    border-radius: 4px;
    background: rgba(255,255,255,0.9);
    font-size: 12px;
    color: #666;
}
.map-panel-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 4px 10px;
    background: #f7f8fa;
    border-bottom: 1px solid #eee;
    font-size: 12px;
    color: #666;
}

/* Responsive */
@media (max-width: 768px) {
    .container { padding: 12px; }
    .card-body { padding: 12px; }
    .map-panel { width: 280px; height: 240px; bottom: 50px; right: 10px; }
    .map-toggle-btn { bottom: 10px; right: 10px; }
}
@media (max-width: 480px) {
    .container { padding: 8px; }
    .card-body { padding: 8px; }
    .header h1 { font-size: 18px; }
    .exec-summary-header { flex-direction: column; }
    .dash-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; }
    .score-badge { width: 50px; height: 50px; }
    .score-number { font-size: 20px; }
}
"""
