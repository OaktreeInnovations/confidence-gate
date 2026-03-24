"""HTML template for comprehensive PDF test run reports.

Renders a complete HTML document with inline CSS, designed for
Playwright page.pdf() conversion to A4 format.
"""

from __future__ import annotations

from datetime import datetime, timezone
from html import escape


def render_report_html(
    test_run: dict,
    test_case: dict | None,
    user: dict | None,
    org: dict | None,
    telemetry: dict | None,
    profile: dict | None,
    screenshots: dict[int, str],
) -> str:
    """Render a comprehensive test run report as HTML.

    Args:
        test_run: TestRun document from MongoDB.
        test_case: TestCase document (may be None if deleted).
        user: User document for triggered_by (may be None).
        org: Organization document (may be None).
        telemetry: RunTelemetry document from execution_telemetry (optional).
        profile: ExecutionProfile document from execution_profiles (optional).
        screenshots: Dict of step_number → base64 data URI string.

    Returns:
        Complete HTML document string.
    """
    # Build per-step confidence lookup from telemetry
    step_confidence: dict[int, dict] = {}
    if telemetry:
        for sc in telemetry.get("confidence", {}).get("per_step", []):
            step_confidence[sc["step"]] = sc

    sections = [
        _css(),
        _header(test_run, test_case, org),
        _executive_summary(test_run, user, telemetry),
        _test_case_details(test_case),
    ]

    if telemetry and telemetry.get("environment"):
        sections.append(_environment(telemetry["environment"]))

    sections.append(_step_results(test_run, screenshots, step_confidence))

    if telemetry:
        sections.append(_execution_metrics(telemetry))

    if profile:
        sections.append(_historical_reliability(profile))

    sections.append(_footer())

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Test Run Report – {escape(test_run.get('test_case_title', 'Unknown'))}</title>
{sections[0]}
</head>
<body>
{''.join(sections[1:])}
</body>
</html>"""


# ---------------------------------------------------------------------------
# CSS
# ---------------------------------------------------------------------------

def _css() -> str:
    return """<style>
@page {
    size: A4;
    margin: 18mm 15mm 22mm 15mm;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    font-size: 11px;
    line-height: 1.5;
    color: #1a1a1a;
}
h1 { font-size: 22px; font-weight: 700; margin-bottom: 4px; }
h2 {
    font-size: 15px; font-weight: 700; color: #111;
    border-bottom: 2px solid #e5e7eb; padding-bottom: 6px; margin-bottom: 12px;
    page-break-after: avoid;
}
h3 { font-size: 12px; font-weight: 600; margin-bottom: 4px; }
.section { margin-bottom: 24px; }
.section-break { page-break-before: always; }

/* Header */
.header { text-align: center; margin-bottom: 28px; padding-bottom: 16px; border-bottom: 3px solid #111; }
.header .subtitle { font-size: 13px; color: #6b7280; margin-top: 2px; }
.header .meta { font-size: 11px; color: #9ca3af; margin-top: 8px; }

/* Status badges */
.badge {
    display: inline-block; padding: 2px 10px; border-radius: 4px;
    font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.5px;
}
.badge-passed { background: #dcfce7; color: #166534; }
.badge-failed { background: #fee2e2; color: #991b1b; }
.badge-error  { background: #fef3c7; color: #92400e; }
.badge-skipped { background: #f3f4f6; color: #6b7280; }
.badge-large { font-size: 14px; padding: 4px 16px; border-radius: 6px; }

/* Summary cards */
.summary-grid {
    display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin-bottom: 16px;
}
.summary-card {
    border: 1px solid #e5e7eb; border-radius: 8px; padding: 12px; text-align: center;
}
.summary-card .value { font-size: 24px; font-weight: 700; }
.summary-card .label { font-size: 10px; color: #6b7280; text-transform: uppercase; letter-spacing: 0.5px; }

/* Tables */
table { width: 100%; border-collapse: collapse; font-size: 10.5px; margin-bottom: 12px; }
th, td { padding: 6px 8px; text-align: left; border-bottom: 1px solid #e5e7eb; }
th { background: #f9fafb; font-weight: 600; font-size: 10px; text-transform: uppercase; letter-spacing: 0.3px; color: #6b7280; }
td { vertical-align: top; }

/* Key-value pairs */
.kv { display: grid; grid-template-columns: 140px 1fr; gap: 4px 12px; margin-bottom: 8px; }
.kv dt { font-weight: 600; color: #6b7280; font-size: 10.5px; }
.kv dd { font-size: 10.5px; }

/* Step cards */
.step-card {
    border: 1px solid #e5e7eb; border-radius: 8px; margin-bottom: 16px; overflow: hidden;
    page-break-inside: avoid;
}
.step-header {
    display: flex; align-items: center; gap: 8px;
    padding: 8px 12px; background: #f9fafb; border-bottom: 1px solid #e5e7eb;
}
.step-num { font-weight: 700; font-size: 12px; color: #374151; }
.step-duration { margin-left: auto; font-size: 10px; color: #9ca3af; }
.step-body { padding: 12px; }
.step-field { margin-bottom: 8px; }
.step-field-label { font-size: 10px; font-weight: 600; color: #6b7280; text-transform: uppercase; letter-spacing: 0.3px; margin-bottom: 2px; }
.step-field-value { font-size: 10.5px; line-height: 1.5; }
.step-screenshot { margin-top: 8px; }
.step-screenshot img { max-width: 100%; border: 1px solid #e5e7eb; border-radius: 4px; }
.step-error {
    background: #fef2f2; border: 1px solid #fecaca; border-radius: 4px;
    padding: 6px 10px; margin-top: 6px; font-size: 10.5px; color: #991b1b;
}
.step-meta { display: flex; gap: 16px; margin-top: 6px; font-size: 10px; color: #9ca3af; }

/* Tags */
.tag {
    display: inline-block; padding: 1px 8px; border-radius: 4px;
    background: #eff6ff; color: #1e40af; font-size: 10px; margin-right: 4px;
}

/* Progress bar */
.progress-bar { height: 8px; background: #e5e7eb; border-radius: 4px; overflow: hidden; margin: 6px 0; }
.progress-fill { height: 100%; border-radius: 4px; }
.progress-pass { background: #22c55e; }
.progress-fail { background: #ef4444; }

/* Metrics */
.metrics-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; margin-bottom: 16px; }
.metric-card {
    border: 1px solid #e5e7eb; border-radius: 8px; padding: 10px; text-align: center;
}
.metric-value { font-size: 18px; font-weight: 700; }
.metric-label { font-size: 10px; color: #6b7280; }

/* Footer */
.footer {
    margin-top: 32px; padding-top: 12px; border-top: 1px solid #e5e7eb;
    text-align: center; font-size: 9px; color: #9ca3af;
}
</style>"""


# ---------------------------------------------------------------------------
# Section renderers
# ---------------------------------------------------------------------------

def _header(run: dict, tc: dict | None, org: dict | None) -> str:
    title = escape(run.get("test_case_title", "Unknown Test Case"))
    status = run.get("status", "unknown")
    badge_cls = f"badge-{status}" if status in ("passed", "failed", "error") else "badge-skipped"

    project = escape(run.get("project_name", ""))
    org_name = escape(org.get("name", "")) if org else ""
    run_date = _fmt_datetime(run.get("completed_at") or run.get("created_at"))

    meta_parts = []
    if project:
        meta_parts.append(project)
    if org_name:
        meta_parts.append(org_name)
    meta_parts.append(run_date)

    return f"""
<div class="header">
    <div style="font-size:10px;color:#9ca3af;text-transform:uppercase;letter-spacing:1px;margin-bottom:8px;">Test Execution Report</div>
    <h1>{title}</h1>
    <div style="margin-top:8px;">
        <span class="badge badge-large {badge_cls}">{escape(status)}</span>
    </div>
    <div class="meta">{' &middot; '.join(meta_parts)}</div>
</div>"""


def _executive_summary(run: dict, user: dict | None, telemetry: dict | None = None) -> str:
    total = run.get("total_steps", 0)
    passed = run.get("passed_steps", 0)
    failed = run.get("failed_steps", 0)
    errored = total - passed - failed
    duration = run.get("duration_ms", 0)
    pass_rate = round(passed / total * 100, 1) if total > 0 else 0

    triggered_by = "Unknown"
    if user:
        triggered_by = escape(user.get("display_name") or user.get("email", "Unknown"))

    pass_color = "#22c55e" if pass_rate >= 80 else "#f59e0b" if pass_rate >= 50 else "#ef4444"

    error_msg = run.get("error_message", "")
    error_html = ""
    if error_msg:
        error_html = f'<div class="step-error" style="margin-top:12px;"><strong>Error:</strong> {escape(error_msg[:500])}</div>'

    pass_width = round(passed / total * 100, 1) if total > 0 else 0
    fail_width = round((failed + errored) / total * 100, 1) if total > 0 else 0

    # Confidence score from telemetry
    confidence_html = ""
    if telemetry:
        conf = telemetry.get("confidence", {})
        overall = conf.get("overall")
        recommendation = conf.get("recommendation", "")
        total_retries = telemetry.get("total_retries", 0)
        if overall is not None:
            conf_pct = round(overall * 100, 1)
            conf_color = "#22c55e" if overall >= 0.85 else "#f59e0b" if overall >= 0.60 else "#ef4444"
            rec_color = "#22c55e" if recommendation == "reliable" else "#f59e0b" if recommendation == "flaky" else "#ef4444"
            confidence_html = f"""
    <div style="margin-top:12px;padding:10px 12px;border:1px solid #e5e7eb;border-radius:8px;background:#f9fafb;">
        <div style="display:flex;align-items:center;gap:24px;flex-wrap:wrap;">
            <div style="text-align:center;">
                <div style="font-size:22px;font-weight:700;color:{conf_color};">{conf_pct}%</div>
                <div style="font-size:10px;color:#6b7280;text-transform:uppercase;letter-spacing:0.4px;">AI Confidence</div>
            </div>
            <div style="text-align:center;">
                <div style="font-size:14px;font-weight:700;color:{rec_color};text-transform:uppercase;">{escape(recommendation)}</div>
                <div style="font-size:10px;color:#6b7280;text-transform:uppercase;letter-spacing:0.4px;">Verdict</div>
            </div>
            <div style="text-align:center;">
                <div style="font-size:18px;font-weight:700;">{total_retries}</div>
                <div style="font-size:10px;color:#6b7280;text-transform:uppercase;letter-spacing:0.4px;">Total Retries</div>
            </div>
        </div>
    </div>"""

    return f"""
<div class="section">
    <h2>Executive Summary</h2>
    <div class="summary-grid">
        <div class="summary-card">
            <div class="value" style="color:{pass_color}">{pass_rate}%</div>
            <div class="label">Pass Rate</div>
        </div>
        <div class="summary-card">
            <div class="value">{total}</div>
            <div class="label">Total Steps</div>
        </div>
        <div class="summary-card">
            <div class="value" style="color:#22c55e">{passed}</div>
            <div class="label">Passed</div>
        </div>
        <div class="summary-card">
            <div class="value" style="color:#ef4444">{failed + errored}</div>
            <div class="label">Failed / Error</div>
        </div>
    </div>
    <div class="progress-bar">
        <div class="progress-fill progress-pass" style="width:{pass_width}%;display:inline-block;"></div><div class="progress-fill progress-fail" style="width:{fail_width}%;display:inline-block;"></div>
    </div>
    {confidence_html}
    <dl class="kv" style="margin-top:12px;">
        <dt>Duration</dt><dd>{_fmt_duration(duration)}</dd>
        <dt>Triggered By</dt><dd>{triggered_by}</dd>
        <dt>Started</dt><dd>{_fmt_datetime(run.get('started_at'))}</dd>
        <dt>Completed</dt><dd>{_fmt_datetime(run.get('completed_at'))}</dd>
        <dt>Test Type</dt><dd>{escape(run.get('test_type', 'ui')).upper()}</dd>
        <dt>Run ID</dt><dd style="font-family:monospace;font-size:10px;">{escape(str(run.get('_id', '')))}</dd>
    </dl>
    {error_html}
</div>"""


def _test_case_details(tc: dict | None) -> str:
    if not tc:
        return '<div class="section"><h2>Test Case Details</h2><p style="color:#9ca3af;">Test case data unavailable.</p></div>'

    desc = escape(tc.get("description", "") or "No description provided.")
    prereqs = escape(tc.get("prerequisites", "") or "None")
    priority = tc.get("priority", "medium")
    tags = tc.get("tags", [])
    base_url = escape(tc.get("base_url", "") or "N/A")
    test_data = tc.get("test_data", {})

    priority_colors = {
        "critical": "#991b1b", "high": "#c2410c",
        "medium": "#92400e", "low": "#6b7280",
    }
    p_color = priority_colors.get(priority, "#6b7280")

    tags_html = "".join(f'<span class="tag">{escape(t)}</span>' for t in tags) if tags else '<span style="color:#9ca3af;">None</span>'

    test_data_html = ""
    if test_data:
        rows = "".join(
            f"<tr><td style='font-weight:600;'>{escape(k)}</td><td>{escape(str(v))}</td></tr>"
            for k, v in test_data.items()
        )
        test_data_html = f"""
        <h3 style="margin-top:12px;">Test Data</h3>
        <table><thead><tr><th>Key</th><th>Value</th></tr></thead><tbody>{rows}</tbody></table>"""

    return f"""
<div class="section">
    <h2>Test Case Details</h2>
    <dl class="kv">
        <dt>Description</dt><dd>{desc}</dd>
        <dt>Prerequisites</dt><dd>{prereqs}</dd>
        <dt>Priority</dt><dd><span style="color:{p_color};font-weight:600;text-transform:uppercase;">{escape(priority)}</span></dd>
        <dt>Tags</dt><dd>{tags_html}</dd>
        <dt>Base URL</dt><dd style="font-family:monospace;font-size:10px;">{base_url}</dd>
    </dl>
    {test_data_html}
</div>"""


def _environment(env: dict) -> str:
    rows = [
        ("Browser", f"{env.get('browser_name', 'N/A')} {env.get('browser_version', '')}"),
        ("Viewport", f"{env.get('viewport_width', '?')} x {env.get('viewport_height', '?')}"),
        ("Platform", env.get("platform", "N/A")),
        ("Headless", "Yes" if env.get("headless") else "No"),
        ("Worker", env.get("worker_hostname", "N/A")),
        ("Timezone", env.get("timezone", "N/A")),
    ]
    items = "".join(f"<dt>{escape(k)}</dt><dd>{escape(str(v))}</dd>" for k, v in rows)
    return f"""
<div class="section">
    <h2>Environment</h2>
    <dl class="kv">{items}</dl>
</div>"""


def _step_results(run: dict, screenshots: dict[int, str], step_confidence: dict[int, dict] | None = None) -> str:
    results = run.get("results", [])
    if not results:
        return '<div class="section section-break"><h2>Step-by-Step Results</h2><p style="color:#9ca3af;">No step results available.</p></div>'

    step_confidence = step_confidence or {}
    cards = []
    for r in results:
        step_num = r.get("step_number", 0)
        r_status = r.get("status", "unknown")
        badge_cls = f"badge-{r_status}" if r_status in ("passed", "failed", "error", "skipped") else "badge-skipped"

        action = escape(r.get("action", "N/A"))
        expected = escape(r.get("expected", ""))
        actual = escape(r.get("actual", ""))
        error_msg = r.get("error_message", "")
        duration = r.get("duration_ms", 0)
        retry_count = r.get("retry_count", 0)
        heal_attempts = r.get("ai_heal_attempts", 0)
        v_mode = r.get("verification_mode", "")

        # Error block
        error_html = ""
        if error_msg:
            error_html = f'<div class="step-error"><strong>Error:</strong> {escape(error_msg[:500])}</div>'

        # Screenshot
        screenshot_html = ""
        if step_num in screenshots:
            screenshot_html = f"""
            <div class="step-screenshot">
                <img src="{screenshots[step_num]}" alt="Step {step_num} screenshot" />
            </div>"""

        # Per-step confidence + risk factors from telemetry
        conf_html = ""
        sc = step_confidence.get(step_num)
        if sc:
            conf_val = sc.get("confidence")
            risk_factors = sc.get("risk_factors", [])
            if conf_val is not None:
                conf_pct = round(conf_val * 100, 1)
                conf_color = "#22c55e" if conf_val >= 0.85 else "#f59e0b" if conf_val >= 0.60 else "#ef4444"
                risk_html = ""
                if risk_factors:
                    items = "".join(f'<span style="background:#fef3c7;color:#92400e;padding:1px 6px;border-radius:3px;font-size:9.5px;margin-right:4px;">{escape(f)}</span>' for f in risk_factors)
                    risk_html = f'<div style="margin-top:4px;">{items}</div>'
                conf_html = f"""
                <div style="margin-top:6px;display:flex;align-items:center;gap:8px;flex-wrap:wrap;">
                    <span style="font-size:10px;color:#6b7280;font-weight:600;text-transform:uppercase;letter-spacing:0.3px;">Confidence:</span>
                    <span style="font-weight:700;color:{conf_color};font-size:11px;">{conf_pct}%</span>
                    {risk_html}
                </div>"""

        # Meta info
        meta_parts = []
        if retry_count > 0:
            meta_parts.append(f"Retries: {retry_count}")
        if heal_attempts > 0:
            meta_parts.append(f"AI Heals: {heal_attempts}")
        if v_mode:
            meta_parts.append(f"Verification: {v_mode}")
        meta_html = f'<div class="step-meta">{"&nbsp;&middot;&nbsp;".join(meta_parts)}</div>' if meta_parts else ""

        cards.append(f"""
        <div class="step-card">
            <div class="step-header">
                <span class="step-num">Step {step_num}</span>
                <span class="badge {badge_cls}">{escape(r_status)}</span>
                <span class="step-duration">{_fmt_duration(duration)}</span>
            </div>
            <div class="step-body">
                <div class="step-field">
                    <div class="step-field-label">Action</div>
                    <div class="step-field-value">{action}</div>
                </div>
                <div class="step-field">
                    <div class="step-field-label">Expected</div>
                    <div class="step-field-value">{expected or '<span style="color:#9ca3af;">—</span>'}</div>
                </div>
                <div class="step-field">
                    <div class="step-field-label">Actual</div>
                    <div class="step-field-value">{actual or '<span style="color:#9ca3af;">—</span>'}</div>
                </div>
                {error_html}
                {screenshot_html}
                {conf_html}
                {meta_html}
            </div>
        </div>""")

    return f"""
<div class="section section-break">
    <h2>Step-by-Step Results</h2>
    {''.join(cards)}
</div>"""


def _execution_metrics(tel: dict) -> str:
    steps = tel.get("steps", [])
    total_ai = tel.get("total_ai_calls", 0)
    total_ai_ms = tel.get("total_ai_latency_ms", 0)
    total_retries = tel.get("total_retries", 0)
    flake_indicators = tel.get("flake_indicators", [])

    # Build confidence lookup
    per_step_conf: dict[int, float] = {}
    for sc in tel.get("confidence", {}).get("per_step", []):
        per_step_conf[sc["step"]] = sc.get("confidence", 0.0)

    # Summary metrics
    overall_conf = tel.get("confidence", {}).get("overall")
    conf_card = ""
    if overall_conf is not None:
        conf_color = "#22c55e" if overall_conf >= 0.85 else "#f59e0b" if overall_conf >= 0.60 else "#ef4444"
        conf_card = f"""<div class="metric-card">
            <div class="metric-value" style="color:{conf_color};">{round(overall_conf * 100, 1)}%</div>
            <div class="metric-label">Run Confidence</div>
        </div>"""

    metrics_html = f"""
    <div class="metrics-grid">
        <div class="metric-card">
            <div class="metric-value">{total_ai}</div>
            <div class="metric-label">AI Calls</div>
        </div>
        <div class="metric-card">
            <div class="metric-value">{_fmt_duration(total_ai_ms)}</div>
            <div class="metric-label">AI Latency</div>
        </div>
        <div class="metric-card">
            <div class="metric-value">{total_retries}</div>
            <div class="metric-label">Total Retries</div>
        </div>
        {conf_card}
    </div>"""

    # Per-step timing table (with confidence column)
    if steps:
        rows = []
        for s in steps:
            sn = s.get("step_number", 0)
            timings = s.get("timings", {})
            conf_val = per_step_conf.get(sn)
            if conf_val is not None:
                conf_color = "#22c55e" if conf_val >= 0.85 else "#f59e0b" if conf_val >= 0.60 else "#ef4444"
                conf_cell = f'<span style="color:{conf_color};font-weight:600;">{round(conf_val * 100, 1)}%</span>'
            else:
                conf_cell = "—"
            # Healing indicator
            healing_used = any(
                a.get("healing_attempted") for a in s.get("attempts", [])
            )
            heal_cell = '<span style="color:#f59e0b;">&#x2713;</span>' if healing_used else "—"
            rows.append(f"""<tr>
                <td>{sn}</td>
                <td>{_fmt_duration(timings.get('context_capture_ms', 0))}</td>
                <td>{_fmt_duration(timings.get('code_generation_ms', 0))}</td>
                <td>{_fmt_duration(timings.get('code_execution_ms', 0))}</td>
                <td>{_fmt_duration(timings.get('verification_ms', 0))}</td>
                <td><strong>{_fmt_duration(timings.get('total_ms', 0))}</strong></td>
                <td>{s.get('total_attempts', 1)}</td>
                <td>{conf_cell}</td>
                <td>{heal_cell}</td>
            </tr>""")

        timing_table = f"""
        <h3>Per-Step Timing Breakdown</h3>
        <table>
            <thead><tr>
                <th>Step</th><th>Context</th><th>AI Gen</th><th>Execution</th><th>Verify</th><th>Total</th><th>Attempts</th><th>Confidence</th><th>Healed</th>
            </tr></thead>
            <tbody>{''.join(rows)}</tbody>
        </table>"""
    else:
        timing_table = ""

    # Flake indicators
    flake_html = ""
    if flake_indicators:
        items = "".join(f"<li>{escape(f)}</li>" for f in flake_indicators)
        flake_html = f'<h3 style="margin-top:12px;">Flake Indicators</h3><ul style="padding-left:20px;font-size:10.5px;">{items}</ul>'

    return f"""
<div class="section section-break">
    <h2>Execution Metrics</h2>
    {metrics_html}
    {timing_table}
    {flake_html}
</div>"""


def _historical_reliability(prof: dict) -> str:
    pass_rate = prof.get("pass_rate", 0)
    flake_rate = prof.get("flake_rate", 0)
    error_rate = prof.get("error_rate", 0)
    median_ms = prof.get("median_duration_ms", 0)
    p95_ms = prof.get("p95_duration_ms", 0)
    total_runs = prof.get("total_runs", 0)
    weakest = prof.get("weakest_step")
    trend = prof.get("reliability_trend", 0)

    trend_icon = "&#x2191;" if trend > 0 else "&#x2193;" if trend < 0 else "&#x2194;"
    trend_color = "#22c55e" if trend > 0 else "#ef4444" if trend < 0 else "#6b7280"

    metrics = f"""
    <div class="metrics-grid">
        <div class="metric-card">
            <div class="metric-value" style="color:{'#22c55e' if pass_rate >= 0.8 else '#ef4444'}">{round(pass_rate * 100, 1)}%</div>
            <div class="metric-label">Pass Rate ({total_runs} runs)</div>
        </div>
        <div class="metric-card">
            <div class="metric-value" style="color:{'#22c55e' if flake_rate < 0.1 else '#f59e0b'}">{round(flake_rate * 100, 1)}%</div>
            <div class="metric-label">Flake Rate</div>
        </div>
        <div class="metric-card">
            <div class="metric-value" style="color:{trend_color}">{trend_icon}</div>
            <div class="metric-label">Reliability Trend</div>
        </div>
    </div>
    <dl class="kv">
        <dt>Median Duration</dt><dd>{_fmt_duration(median_ms)}</dd>
        <dt>P95 Duration</dt><dd>{_fmt_duration(p95_ms)}</dd>
        <dt>Error Rate</dt><dd>{round(error_rate * 100, 1)}%</dd>
        <dt>Weakest Step</dt><dd>{'Step ' + str(weakest) if weakest else 'None'}</dd>
    </dl>"""

    # Per-step reliability table
    step_profiles = prof.get("step_profiles", [])
    step_table = ""
    if step_profiles:
        rows = []
        for sp in step_profiles:
            sp_pass = sp.get("pass_rate", 0)
            row_color = "" if sp_pass >= 0.9 else ' style="background:#fef2f2;"' if sp_pass < 0.7 else ' style="background:#fffbeb;"'
            failures = sp.get("most_common_failures", [])
            fail_text = ", ".join(f"{f.get('reason', '?')} ({f.get('count', 0)}x)" for f in failures[:2]) if failures else "—"
            rows.append(f"""<tr{row_color}>
                <td>{sp.get('step_number', '?')}</td>
                <td>{round(sp_pass * 100, 1)}%</td>
                <td>{round(sp.get('retry_rate', 0) * 100, 1)}%</td>
                <td>{sp.get('avg_attempts', 1):.1f}</td>
                <td style="font-size:9.5px;">{escape(fail_text)}</td>
            </tr>""")
        step_table = f"""
        <h3 style="margin-top:12px;">Per-Step Reliability</h3>
        <table>
            <thead><tr><th>Step</th><th>Pass Rate</th><th>Retry Rate</th><th>Avg Attempts</th><th>Common Failures</th></tr></thead>
            <tbody>{''.join(rows)}</tbody>
        </table>"""

    return f"""
<div class="section section-break">
    <h2>Historical Reliability</h2>
    {metrics}
    {step_table}
</div>"""


def _footer() -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    return f"""
<div class="footer">
    Generated by Qualora &middot; {now}
</div>"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fmt_duration(ms: int | float | None) -> str:
    """Format milliseconds into human-readable duration."""
    if ms is None or ms == 0:
        return "—"
    ms = int(ms)
    if ms < 1000:
        return f"{ms}ms"
    seconds = ms / 1000
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes = int(seconds // 60)
    secs = seconds % 60
    return f"{minutes}m {secs:.0f}s"


def _fmt_datetime(dt) -> str:
    """Format a datetime (or ISO string) for display."""
    if dt is None:
        return "—"
    if isinstance(dt, str):
        try:
            dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return dt
    if isinstance(dt, datetime):
        return dt.strftime("%b %d, %Y %H:%M:%S UTC")
    return str(dt)
