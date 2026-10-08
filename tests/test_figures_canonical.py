"""Rendering-level attacks on a stored figure (found by an adversarial review, 2026-10-08): every one of these
changes what a reader SEES while the values and mark geometry stay correct. The script is agent-written, so the
re-render (F4) would reproduce them: the static check F3 must catch them on its own."""

import re

import pytest

from custom_harness.figures import svg as S
from custom_harness.figures.audit import check_svg

VALS = {"a": 0.91, "b": 0.42}
BASE = S.bar_chart(VALS, [("ours", "a"), ("base", "b")], title="Accuracy", y_label="acc")
LINE = S.line_chart(VALS, [("ours", ["a", "b"])], x_labels=["s1", "s2"], title="Per seed")
TITLE = '<text class="title" x="320.00" y="20.00" text-anchor="middle">Accuracy'


def _swap_value_label_x(s: str) -> str:
    xs = re.findall(r'class="value" data-ref="(\w)" x="([\d.]+)"', s)
    return (s.replace(f'data-ref="a" x="{xs[0][1]}"', 'data-ref="a" x="XA"')
             .replace(f'data-ref="b" x="{xs[1][1]}"', f'data-ref="b" x="{xs[0][1]}"')
             .replace('x="XA"', f'x="{xs[1][1]}"'))


def _reverse_tick_y(s: str) -> str:
    ys = re.findall(r'<text class="tick" x="[\d.]+" y="([\d.]+)"', s)
    for i, y in enumerate(ys):
        s = s.replace(f'class="tick" x="62.00" y="{y}"', f'class="tick" x="62.00" y="Y{i}"', 1)
    for i, y in enumerate(reversed(ys)):
        s = s.replace(f'y="Y{i}"', f'y="{y}"')
    return s


FAKE_BAR = "".join(f'<line class="axis" x1="400" y1="{y}" x2="560" y2="{y}" stroke="#2a6fdb"/>' for y in range(60, 90))
ATTACKS = {
    "value labels moved": _swap_value_label_x(BASE),
    "tick labels moved (axis flipped)": _reverse_tick_y(BASE),
    "nested svg rescales the marks": BASE.replace(
        '<g class="marks">', '<svg viewBox="0 -400 640 800" width="640" height="400"><g class="marks">').replace(
        "</g>\n</svg>", "</g></svg>\n</svg>"),
    "extra axis lines draw a fake bar": BASE.replace('<g class="marks">', '<g class="marks">' + FAKE_BAR),
    "bar hidden with fill none": BASE.replace('fill="#2a6fdb"', 'fill="none"', 1),
    "title with class tick": BASE.replace("<title>Accuracy</title>", '<title class="tick">Acc 0.99</title>'),
    "root viewBox crops the chart": BASE.replace('viewBox="0 0 640 400"', 'viewBox="0 0 640 30"'),
    "marks moved out of the svg namespace": BASE.replace('<g class="marks">', '<g class="marks" xmlns="">'),
    "svg without namespace": BASE.replace(' xmlns="http://www.w3.org/2000/svg"', "", 1),
    "line chart point moved off its line": LINE.replace('class="link" points="', 'class="link" points="0,0 ', 1),
}
NUMBERS = ["0．99", "0,99", "0·99", "99％", "99 percent", "10⁻³", "０.９９",
           "٠.٩٩"]


def test_the_untouched_figures_pass():
    assert check_svg(BASE.encode(), VALS) == [] and check_svg(LINE.encode(), VALS) == []


@pytest.mark.parametrize("name", sorted(ATTACKS))
def test_rendering_attack_is_caught(name):
    bad = ATTACKS[name]
    assert bad != BASE and bad != LINE  # the attack is in place
    assert check_svg(bad.encode(), VALS), name


@pytest.mark.parametrize("text", NUMBERS, ids=ascii)
def test_number_formats_in_free_text_are_caught(text):
    bad = BASE.replace(TITLE, TITLE.replace("Accuracy", text))
    assert bad != BASE
    assert any("free text" in p for p in check_svg(bad.encode(), VALS)), text


def test_a_category_name_with_a_quote_round_trips():
    s = S.bar_chart(VALS, [('say "hi"', "a"), ("b & c", "b")], title="t")
    assert check_svg(s.encode(), VALS) == []
