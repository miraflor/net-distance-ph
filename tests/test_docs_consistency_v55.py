"""Static cross-check of v55 documentation against the actual CLI and aggregation code."""
from pathlib import Path

from net_distance_ph.population_weighted import parse_args

ROOT = Path(__file__).resolve().parents[1]


def read(path):
    return (ROOT / path).read_text(encoding="utf-8")


def test_current_default_and_explicit_upper_levels():
    args = parse_args([])
    assert args.level == "all"
    assert args.median_selection == "balanced"
    assert parse_args(["--level", "province"]).level == "province"
    assert parse_args(["--level", "all"]).level == "all"
    assert parse_args(["--median-selection", "one"]).median_selection == "one"


def test_home_and_readme_prioritize_population_weighted():
    for path in ("README.md", "docs/index.md"):
        doc = read(path)
        assert "net-distance-ph population-weighted" in doc
        assert "town" in doc.lower() and "balanced" in doc.lower()
        assert "legacy" in doc.lower()
        assert "province" in doc.lower()


def test_method_preserves_distances_and_coverage_distinction():
    method = read("docs/population_weighted.md")
    for text in ("conditional", "full census", "one snapped origin",
                 "greedy", "1-swap", "project-defined", "origin-to-road",
                 "province_equivalent/", "HUC", "--level all"):
        assert text in method
    assert "globally optimal" in method
    assert "not added" in method or "does not include" in method or "do not add" in method


def test_site_navigation_has_current_method_and_literature():
    nav = read("mkdocs.yml")
    assert "population_weighted.md" in nav
    assert "literature.md" in nav
    for path in ("docs/index.md", "docs/outputs.md", "docs/methodology.md"):
        assert "population_weighted.md" in read(path)
    lit = read("docs/literature.md")
    for doi in ("10.1007/BF01386390", "10.1287/opre.12.3.450",
                "10.1287/opre.13.3.462", "10.1287/opre.16.5.955"):
        assert doi in lit


def test_legacy_and_population_distances_are_distinct():
    for name in ("docs/methodology.md", "docs/pipeline.md", "docs/outputs.md"):
        assert "legacy" in read(name).lower()
    for name in ("docs/index.md", "docs/population_weighted.md", "docs/outputs.md"):
        assert "reachable_pair_probability" in read(name)
