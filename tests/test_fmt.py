"""Markdown -> Telegram HTML."""
import sys
from pathlib import Path

import pytest

from telegram_bridge import fmt


def h(s):
    return fmt.to_html(s)


def test_escapes_html_specials():
    assert h("a < b && c > d") == "a &lt; b &amp;&amp; c &gt; d"
    assert h("<script>alert(1)</script>") == "&lt;script&gt;alert(1)&lt;/script&gt;"


def test_bold_italic_strike():
    assert h("**bold** and __bold2__") == "<b>bold</b> and <b>bold2</b>"
    assert h("*it* and _it2_") == "<i>it</i> and <i>it2</i>"
    assert h("***both***") == "<b><i>both</i></b>"
    assert h("~~gone~~") == "<s>gone</s>"
    assert h("(**in parens**), *x*.") == "(<b>in parens</b>), <i>x</i>."


@pytest.mark.parametrize("s", [
    "my_file_name.py", r"C:\Temp\x\my_file_name.py", "see /tmp/a_b_c/d_e.txt now",
    "snake_case_name and other_name", "2 * 3 * 4", "a*b*c", "https://x.com/a_b_c?d=1",
    "__init__.py is a file", "MAX_VALUE_HERE", "5 * (3 + 4) = 35",
])
def test_no_false_emphasis(s):
    out = h(s)
    assert "<i>" not in out and "<b>" not in out, out


def test_underscore_emphasis_at_word_edges_only():
    assert h("use _this_ one") == "use <i>this</i> one"
    assert h("a _b_.") == "a <i>b</i>."


def test_inline_code_protects_content():
    assert h("run `x < y && **z**` now") == "run <code>x &lt; y &amp;&amp; **z**</code> now"
    assert h("`a_b_c` and `*x*`") == "<code>a_b_c</code> and <code>*x*</code>"


def test_fenced_code_block():
    src = "before\n```python\nif a < b and c > d:\n    print('&')\n```\nafter **x**"
    out = h(src)
    assert out == ("before\n<pre><code class=\"language-python\">if a &lt; b and c &gt; d:\n    print('&amp;')"
                   "</code></pre>\nafter <b>x</b>")


def test_fenced_block_without_language_and_unterminated():
    assert h("```\nx<y\n```") == "<pre><code>x&lt;y</code></pre>"
    assert h("```\nx<y") == "<pre><code>x&lt;y</code></pre>"
    assert "<b>" not in h("```\n**not bold**\n```")


def test_links():
    assert h("[go](https://a.com/x_y?a=1&b=2)") == '<a href="https://a.com/x_y?a=1&amp;b=2">go</a>'
    assert h("[**b**](https://a.com)") == '<a href="https://a.com"><b>b</b></a>'
    assert h("[w](https://en.wikipedia.org/wiki/A_(b))") == '<a href="https://en.wikipedia.org/wiki/A_(b)">w</a>'
    assert "<a" not in h("[bad](javascript:alert(1))")     # unsafe scheme stays text
    assert '"' not in h('[x](https://a.com/"q)').replace('href="', "").replace('">', "")


def test_headings_bullets_rules():
    assert h("# Title\n## Sub **x**") == "<b>Title</b>\n<b>Sub x</b>"
    assert h("- a\n* b\n  + c\n1. d") == "- a\n• b\n  • c\n1. d"
    assert h("* **bold** item") == "• <b>bold</b> item"
    assert h("text\n---\nmore") == "text\n――――――\nmore"


def test_unicode_and_czech_untouched():
    assert h("Příliš žluťoučký kůň — 3 < 4") == "Příliš žluťoučký kůň — 3 &lt; 4"


def test_misnested_falls_back_to_plain_escaped():
    out = h("*a **b* c**")
    assert fmt._balanced(out)


def test_balanced_helper():
    assert fmt._balanced("<b>x</b><i>y</i>") and fmt._balanced("<pre><code>a</code></pre>")
    assert not fmt._balanced("<b><i>x</b></i>") and not fmt._balanced("<b>x")


# ---- splitting
def test_short_text_single_chunk():
    assert fmt.render_chunks("hello **w**") == [("hello <b>w</b>", "hello **w**")]
    assert fmt.render_chunks("") == []


def test_long_text_chunks_fit_and_are_balanced():
    text = "\n\n".join(f"Paragraph {i} with **bold {i}**, `code <{i}>` and _it_ & more." for i in range(400))
    chunks = fmt.render_chunks(text)
    assert len(chunks) > 1
    for html, plain in chunks:
        assert len(html) <= 4096 and len(plain) <= 4096 and fmt._balanced(html)
    assert "".join(p for _, p in chunks).count("Paragraph") == 400   # nothing lost


def test_code_block_spanning_split_is_closed_and_reopened():
    code = "\n".join(f"x{i} = a < b & c" for i in range(1500))
    chunks = fmt.render_chunks(f"intro\n```py\n{code}\n```\nend")
    assert len(chunks) >= 3
    for html, plain in chunks:
        assert len(html) <= 4096 and fmt._balanced(html)
    assert chunks[0][0].startswith("intro")
    assert all("<pre><code" in html for html, _ in chunks[:-1])
    assert chunks[1][0].startswith('<pre><code class="language-py">')   # reopened with the language
    assert chunks[-1][0].endswith("end")
    allcode = "".join(h_ for h_, _ in chunks)
    assert allcode.count("x1499 = a &lt; b &amp; c") == 1


def test_escape_heavy_text_is_resplit_to_fit():
    text = "&<>" * 4000      # escaping triples/quintuples the size
    chunks = fmt.render_chunks(text)
    assert len(chunks) >= 3 and all(len(h_) <= 4096 for h_, _ in chunks)
    assert "".join(p for _, p in chunks) == text


def test_very_long_single_line_is_cut():
    chunks = fmt.render_chunks("word " * 5000)
    assert len(chunks) >= 6 and all(len(h_) <= 4096 for h_, _ in chunks)
    chunks = fmt.render_chunks("x" * 20000)
    assert all(len(h_) <= 4096 for h_, _ in chunks) and sum(len(p) for _, p in chunks) == 20000


def test_with_footer_appends_or_adds_chunk():
    c = fmt.with_footer([("a", "a")], "· 42% context")
    assert c == [("a\n\n· 42% context", "a\n\n· 42% context")]
    full = [("x" * 4090, "x" * 4090)]
    c = fmt.with_footer(full, "· 42% context")
    assert len(c) == 2 and c[1] == ("· 42% context", "· 42% context")
    assert fmt.with_footer([("a", "a")], "") == [("a", "a")]


def test_preview_closes_open_fence():
    html, plain = fmt.preview_html("```py\n" + "x<y\n" * 300, 500)
    assert fmt._balanced(html) and html.endswith("…") and plain.count("```") == 2
    html, _ = fmt.preview_html("**bold** " * 100, 500)
    assert fmt._balanced(html)
