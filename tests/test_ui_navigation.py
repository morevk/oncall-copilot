"""The three browser surfaces share one stable application header."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SURFACES = {
    "Control Center": (
        ROOT / "packages/oncall-lab/src/oncall_lab/static/index.html",
        ROOT / "packages/oncall-lab/src/oncall_lab/static/app.css",
        "Control center",
    ),
    "Incidents": (
        ROOT / "packages/oncall-service/src/oncall_service/static/index.html",
        ROOT / "packages/oncall-service/src/oncall_service/static/app.css",
        "Incidents",
    ),
    "Evaluations": (
        ROOT / "packages/oncall-eval/src/oncall_eval/dashboard/static/index.html",
        ROOT / "packages/oncall-eval/src/oncall_eval/dashboard/static/app.css",
        "Evaluations",
    ),
}


def test_every_ui_has_the_same_product_navigation_in_the_same_order() -> None:
    for surface, (html_path, _css_path, active_label) in SURFACES.items():
        html = html_path.read_text(encoding="utf-8")
        nav = re.search(r'<nav class="product-nav".*?</nav>', html, re.DOTALL)

        assert nav is not None, f"{surface} has no product navigation"
        labels = re.findall(r">([^<>]+)</a>", nav.group())
        assert labels == ["Control center", "Incidents", "Evaluations"]
        assert re.search(
            rf'class="active" aria-current="page"[^>]*>{re.escape(active_label)}</a>',
            nav.group(),
        ), f"{surface} does not identify its active navigation item"
        assert '<span class="brand-copy"><strong>On-Call Copilot</strong>' in html
        assert '<div class="topbar-context">' in html


def test_every_ui_uses_the_same_header_geometry() -> None:
    required_geometry = (
        "--bg: #0b0d10",
        "radial-gradient(circle at 68% -20%, rgba(185, 244, 90, 0.055), transparent 35%)",
        "height: 68px",
        "grid-template-columns: minmax(200px, 1fr) auto minmax(200px, 1fr)",
        "gap: 20px",
        "padding: 0 22px",
        "background: rgba(11, 13, 16, 0.94)",
        "backdrop-filter: blur(12px)",
        "box-shadow: 0 0 10px rgba(185, 244, 90, 0.25)",
        ".product-brand",
        ".brand-copy",
        ".product-nav",
        ".topbar-context",
        "@media (max-width: 820px)",
        "@media (max-width: 520px)",
    )

    for surface, (_html_path, css_path, _active_label) in SURFACES.items():
        css = css_path.read_text(encoding="utf-8")
        missing = [token for token in required_geometry if token not in css]
        assert not missing, f"{surface} header drifted: missing {missing}"
