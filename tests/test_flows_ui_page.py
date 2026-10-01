"""The flow editor page, run in Node against a stub DOM and fetch: automatic sign-in and search."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")
PAGE = ROOT / "src" / "daemon" / "flows_ui.html"


def run(scenario):
    completed = subprocess.run([NODE, str(ROOT / "tests" / "flows_ui_page_harness.mjs"),
                                str(ROOT / "src" / "daemon" / "flows_ui.html"), scenario],
                               capture_output=True, text=True, timeout=60, check=True)
    return json.loads(completed.stdout)


pytestmark = pytest.mark.skipif(NODE is None, reason="needs node")


def test_first_visit_signs_in_without_showing_the_sign_in_page():
    result = run("signs_in")
    assert result["posts"] == [{}]
    assert not result["signin_shown"] and not result["main_hidden"]


def test_concurrent_401s_share_one_attempt_and_are_retried():
    result = run("concurrent")
    assert result["concurrent_posts"] == 1
    assert result["answers"] == ["ok", "ok", "ok"]
    assert not result["signin_shown"]


def test_refused_sign_in_shows_the_command_once():
    result = run("refused")
    assert result["posts"] == [{}]
    assert result["signin_shown"] and result["main_hidden"]


def test_a_cookie_that_never_arrives_does_not_loop():
    result = run("cookie_lost")
    assert result["posts"] == [{}]
    assert result["signin_shown"]


def test_a_used_code_falls_back_to_automatic_sign_in():
    result = run("used_code")
    assert result["posts"] == [{"code": "used-code"}, {}]
    assert not result["signin_shown"] and not result["main_hidden"]


@pytest.fixture(scope="module")
def search():
    return run("search")


def test_search_matches_all_terms_case_insensitively_and_orders_name_matches_first(search):
    assert search["alpha"]["list"][1:] == ["Personal", "Alpha plan", "Personal", "Beta [in text]"]
    assert search["alpha"]["count"] == "2 of 3"
    assert search["and_terms"]["list"][1:] == ["Personal", "Alpha plan"]
    assert search["and_terms"]["count"] == "1 of 3"
    assert search["rules_logging"]["list"] == ["Rules (2/3 on)", "Logging style", "Security [in text]", "Third [in text]"]
    assert search["rules_logging"]["count"] == "3 of 3"
    assert search["rules_and"]["list"][1:] == ["Security [in text]", "Third [in text]"]


def test_empty_query_leaves_the_list_unchanged(search):
    assert search["cleared"] == search["initial"]
    assert search["initial"]["count"] == ""
    assert search["escape"]["query"] == "" and search["escape"]["count"] == ""
    assert search["escape"]["list"][1:] == ["Security", "Logging style", "Third"]


def test_flows_are_found_by_their_text_within_the_shown_category(search):
    assert search["needle_user"]["list"][1:] == ["github.com/o/r", "Repo flow [in text]"]
    assert search["needle_user"]["count"] == "1 of 3"
    assert search["needle_system"]["list"][1:] == ["Built-in", "Review [in text]"]
    assert search["needle_system"]["count"] == "1 of 1"
    assert search["none"]["list"][1:] == ["No matches"] and search["none"]["count"] == "0 of 3"


def test_queries_are_kept_per_tab(search):
    assert search["rules_query_empty"] == ""
    assert search["flows_query_kept"] == "user"
    assert search["rules_query_kept"] == "zzz"


def test_the_open_item_stays_open_while_filtered_out_and_scroll_is_kept(search):
    assert search["opened"] == {"title": "rule-a", "hidden": False}
    assert search["filtered_out"]["list"][1:] == ["No matches"]
    assert search["filtered_out"]["title"] == "rule-a" and not search["filtered_out"]["hidden"]
    assert search["scroll_kept"] == 120


def test_slash_focuses_the_search_box_except_while_typing(search):
    assert search["slash"] == {"prevented": True, "focused": 1, "typing_prevented": False}


def page_parts():
    html = PAGE.read_text(encoding="utf-8")
    script = re.search(r'<script nonce="\{\{NONCE\}\}">([\s\S]*?)</script>', html).group(1)
    style = re.search(r'<style nonce="\{\{NONCE\}\}">([\s\S]*?)</style>', html).group(1)
    return html, script, style


def test_only_the_list_and_the_detail_pane_scroll():
    html, _, style = page_parts()
    assert re.search(r"body \{[^}]*height: 100dvh[^}]*overflow: hidden", style)
    assert re.search(r"main \{[^}]*flex: 1[^}]*min-height: 0[^}]*overflow: hidden", style)
    assert re.search(r"#items \{[^}]*overflow-y: auto", style)
    assert re.search(r"main > section \{[^}]*overflow-y: auto", style)
    assert re.search(r"grid-template-rows: minmax\(0, 40%\) minmax\(0, 1fr\)", style)
    assert "vh" not in re.sub(r"100(d?)vh", "", style)  # no fixed viewport-height textareas
    assert html.index('id="search"') < html.index('id="items"')


def test_the_page_keeps_its_content_security_policy():
    html, script, _ = page_parts()
    assert not re.search(r"\son[a-z]+\s*=", html)
    assert not re.search(r"(src|href)=[\"']https?:", html)
    assert "<style nonce" in html and html.count("{{NONCE}}") == 2
    completed = subprocess.run([NODE, "--check", "-"], input=script, capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr


def test_a_scope_prefix_is_not_part_of_the_name(search):
    assert search["prefix"]["list"][1:] == ["No matches"]


def test_a_new_query_starts_at_the_top_of_the_list(search):
    assert search["scroll_on_query"] == 0


def test_typing_while_a_tab_loads_does_not_render_stale_data(search):
    assert search["while_loading"] == {"list": ["Loading…"], "count": "", "query": "sec"}
    assert search["after_loading"]["list"][0] == "Skills (2/3 on)"
    assert search["after_loading"]["count"] == "3 of 3"


def test_name_matches_of_every_group_come_before_text_only_matches(search):
    assert search["global_order"]["list"][1:] == ["github.com/o/r", "Repo flow", "Personal", "Beta [in text]"]
    assert search["global_order"]["count"] == "2 of 3"


def test_an_empty_search_shows_one_empty_state(search):
    assert search["system_none"]["list"][1:] == ["No matches"]


# --- Markdown view -----------------------------------------------------------------------

def render(text):
    completed = subprocess.run([NODE, str(ROOT / "tests" / "flows_ui_page_harness.mjs"), str(PAGE), "render"],
                               input=text, capture_output=True, text=True, timeout=60, check=True)
    return json.loads(completed.stdout)


def html_of(text):
    return render(text)["html"]


def test_headings_paragraphs_and_inline_formatting_are_rendered():
    assert html_of("# Title\n\nHello **bold**, *it*, _it2_, ~~gone~~, ***both*** and `code`.\n") == (
        '<h1 id="md-title">Title</h1><p>Hello <strong>bold</strong>, <em>it</em>, <em>it2</em>, <del>gone</del>, '
        "<strong><em>both</em></strong> and <code>code</code>.</p>")
    assert html_of("Title\n=====\n\nSub\n---\n") == '<h1 id="md-title">Title</h1><h2 id="md-sub">Sub</h2>'
    assert "<em>" not in html_of("snake_case_name and 2 * 3 * 4")
    assert html_of("line one  \nline two\\\nline three\nsoft") == "<p>line one<br>line two<br>line three soft</p>"


def test_lists_nest_and_carry_task_items():
    assert html_of("- a\n  - b\n- c\n") == "<ul><li><p>a</p><ul><li><p>b</p></li></ul></li><li><p>c</p></li></ul>"
    assert html_of("1. one\n   - nested\n2. two\n") == (
        "<ol><li><p>one</p><ul><li><p>nested</p></li></ul></li><li><p>two</p></li></ol>")
    assert html_of('3. three\n') == '<ol start="3"><li><p>three</p></li></ol>'
    tasks = html_of("- [ ] open\n- [x] done\n")
    assert 'class="md-task"' in tasks and "☐ open" in tasks and "☑ done" in tasks
    assert html_of("- a\nlazy continuation\n") == "<ul><li><p>a lazy continuation</p></li></ul>"


def test_code_quotes_rules_and_tables_are_rendered():
    assert html_of("```js\nlet a = <b>;\n  indented\n```\n") == "<pre><code>let a = &lt;b&gt;;\n  indented</code></pre>"
    assert html_of("    indented code\n") == "<pre><code>indented code</code></pre>"
    assert html_of("> quoted **x**\n> more\n\n---\n") == "<blockquote><p>quoted <strong>x</strong> more</p></blockquote><hr>"
    table = html_of("| a | b | c |\n|:--|:-:|--:|\n| 1 | 2 | 3 |\n| x \\| y |\n")
    assert table == (
        '<div class="md-table"><table><thead><tr><th>a</th><th class="md-center">b</th><th class="md-right">c</th></tr></thead>'
        '<tbody><tr><td>1</td><td class="md-center">2</td><td class="md-right">3</td></tr>'
        '<tr><td>x | y</td><td class="md-center"></td><td class="md-right"></td></tr></tbody></table></div>')
    assert "<table>" not in html_of("a | b\n---\n")  # one delimiter cell for two header cells


def test_only_http_and_anchor_links_become_links():
    html = html_of("[ok](https://example.com/a?b=1) [here](#My-Heading) <https://auto.example> https://bare.example/x.\n")
    assert '<a href="https://example.com/a?b=1" target="_blank" rel="noopener noreferrer">ok</a>' in html
    assert '<a href="#My-Heading">here</a>' in html
    assert '<a href="https://auto.example" target="_blank" rel="noopener noreferrer">https://auto.example</a>' in html
    assert 'href="https://bare.example/x"' in html and html.endswith(".</p>")
    for hostile in ("javascript:alert(1)", "data:text/html,x", "vbscript:x", "file:///etc/passwd", "../other.md", "//evil.example"):
        rendered = html_of(f"[label]({hostile})")
        assert "<a" not in rendered and "href" not in rendered and "label" in rendered, hostile
    assert "<img" not in html_of("![alt text](https://example.com/p.png)") and "alt text" in html_of("![alt text](x.png)")


def test_html_in_documents_stays_inert_text():
    hostile = '<script>alert(1)</script> <img src=x onerror=alert(1)> **<b onclick="x">b</b>**'
    html = html_of(f"# {hostile}\n\n{hostile}\n\n- {hostile}\n\n> {hostile}\n\n| {hostile} |\n|---|\n| {hostile} |\n\n`{hostile}`\n")
    for tag in ("<script", "<img", "<b ", "<b>"):
        assert tag not in html, tag
    assert "&lt;script&gt;" in html
    attrs = re.findall(r"<[a-z0-9]+ ([^>]*)>", html)
    assert all(not re.search(r"\bon[a-z]+=", a) for a in attrs)


def test_frontmatter_is_a_collapsed_metadata_section_not_body_text():
    result = render("---\npersona: tester\nflow: x\n---\n# Doc\n")
    assert result["meta"] == "persona: tester\nflow: x"
    assert result["html"] == ('<details class="md-meta"><summary>Metadata</summary><pre>persona: tester\nflow: x</pre></details>'
                              '<h1 id="md-doc">Doc</h1>')
    assert render("---\nnot meta\n---\n")["meta"] is None  # a rule, text and a rule


def test_heading_ids_are_prefixed_and_unique():
    result = render("# A b\n\n## Same\n\n## Same\n\n### Same?\n\n## Тест\n")
    assert [h["id"] for h in result["headings"]] == ["md-a-b", "md-same", "md-same-1", "md-same-2", "md-тест"]
    assert [h["level"] for h in result["headings"]] == [1, 2, 2, 3, 2]


def test_the_page_script_never_writes_markup_strings():
    _, script, _ = page_parts()
    assert not re.search(r"innerHTML|outerHTML|insertAdjacentHTML|document\.write|\.cssText|setAttribute\(\s*[\"']style", script)
    assert not re.search(r"\.style\.\w+\s*=", script)


def test_real_documents_render_with_a_table_of_contents():
    flows = ROOT / "flows"
    for name in ("pr-review.md", "issue-agent.md"):
        result = render((flows / name).read_text(encoding="utf-8"))
        assert len(result["headings"]) >= 5, name
        assert "<script" not in result["html"]
    skill = next((ROOT / "agents").glob("*/skills/*.md"), None) or next((ROOT / "skills").rglob("*.md"), None)
    if skill:
        assert render(skill.read_text(encoding="utf-8"))["html"]


@pytest.fixture(scope="module")
def ui():
    return run("ui")


def test_a_flow_opens_rendered_with_a_table_of_contents(ui):
    opened = ui["opened"]
    assert opened["panes_hidden"] and not opened["hidden"] and opened["seg"] == [True, False]
    assert opened["toc"] == ["md-toc-item md-l1:Doc", "md-toc-item md-l2:One", "md-toc-item md-l2:Two"]
    assert not opened["toc_hidden"] and not opened["toc_button_hidden"]
    assert opened["html"].startswith('<details class="md-meta">') and "persona: tester" in opened["html"]


def test_a_contents_entry_scrolls_to_its_heading(ui):
    assert ui["heading_scrolled"] == [1, 0]


def test_the_contents_can_be_hidden_revealed_and_the_choice_is_remembered(ui):
    assert ui["hidden"]["toc_hidden"] and ui["hidden"]["hide_label"] == "Keep open"
    assert ui["hidden"]["stored"] == [["agents-ui-toc-hidden", "1"]]
    assert ui["remembered"]["toc_hidden"]  # another document keeps the choice
    assert not ui["revealed"]["toc_hidden"] and ui["revealed"]["stored"] == [["agents-ui-toc-hidden", "0"]]


def test_a_document_without_headings_has_no_contents(ui):
    assert ui["plain"]["no_toc"] and ui["plain"]["toc"] == [] and ui["plain"]["toc_button_hidden"]
    assert ui["plain"]["html"] == "<p>just text, no headings</p>"


def test_source_shows_the_textarea_and_rendered_shows_unsaved_edits(ui):
    assert ui["source"] == {"panes_hidden": False, "view_hidden": True, "seg": [False, True]}
    assert ui["edited"]["html"].startswith('<h1 id="md-changed">Changed</h1>')
    assert "&lt;b&gt;x&lt;/b&gt;" in ui["edited"]["html"]
    assert ui["dirty_save_disabled"] is False and ui["edited"]["save_disabled"] is False


def test_saving_from_source_keeps_the_text_the_revision_and_the_view(ui):
    assert ui["put"] == {"id": "user:doc", "content": "# Changed\n\n## Fresh\n\n<b>x</b>\n", "scope": "user",
                         "override": False, "expected_revision": "rev1"}
    assert ui["after_save"] == {"panes_hidden": False, "save_disabled": True, "content": "# Changed\n\n## Fresh\n\n<b>x</b>\n"}


def test_a_save_conflict_still_shows_both_texts_in_source(ui):
    conflict = ui["conflict"]
    assert not conflict["panes_hidden"] and not conflict["side_hidden"]
    assert "changed elsewhere" in conflict["notice"] and conflict["content"] == "# Mine\n"
    assert conflict["save_disabled"] is False


def test_a_component_body_is_rendered_with_source_on_demand(ui):
    skill = ui["skill"]
    assert skill["toc"] == ["md-toc-item md-l1:Skill A", "md-toc-item md-l2:Use"] and skill["source_hidden"]
    assert "&lt;script&gt;x&lt;/script&gt;" in skill["html"] and "<script" not in skill["html"]
    assert ui["skill_source"] == {"view_hidden": True, "source_hidden": False}


@pytest.mark.parametrize("scenario, first_hidden, after_toolbar, stored", [
    ("ui_narrow", True, False, [["agents-ui-toc-hidden", "0"]]),
    ("ui_narrow_stored", False, True, [["agents-ui-toc-hidden", "1"]]),
    ("ui_nostorage", False, True, []),
])
def test_contents_default_to_hidden_on_narrow_screens_and_survive_missing_storage(scenario, first_hidden, after_toolbar, stored):
    result = run(scenario)
    assert result["opened"]["toc_hidden"] is first_hidden
    assert result["after_toolbar"]["toc_hidden"] is after_toolbar
    assert result["stored_after"] == stored


def test_the_contents_styles_hide_with_classes_and_reveal_on_hover():
    _, _, style = page_parts()
    assert re.search(r"\.mdview\.toc-hidden \.md-edge:hover ~ \.md-toc[^{]*\{[^}]*transform: none", style)
    assert re.search(r"\.mdview \{[^}]*flex: 1[^}]*overflow: hidden", style)
    assert re.search(r"\.md-toc, \.md \{[^}]*overflow-y: auto", style)
    assert re.search(r"@media \(max-width: 760px\) \{[^@]*\.mdview \{[^}]*minmax\(0, 35%\)", style)


def test_hostile_or_huge_input_neither_crashes_nor_stalls():
    deep = render("- " * 3000 + "x")  # nesting is capped; the rest is shown as text
    assert "<pre>" in deep["html"] and "x" in deep["html"]
    assert "<pre>" in render("1. - > " * 900 + "x")["html"]
    for hostile in ("_a " * 20000, "**a " * 20000, "[a " * 20000, "*a " * 20000):
        assert render(hostile)["html"].startswith("<p>")  # the harness call has a 60 s timeout
    many = render("# a\n" * 5000)
    assert many["headings"][-1]["id"] == "md-a-4999" and len({h["id"] for h in many["headings"]}) == 5000


def test_empty_headings_and_a_byte_order_mark():
    assert render("#\n\n# Real\n")["headings"][0]["text"] == ""
    assert render("\ufeff---\npersona: x\n---\n# T\n")["meta"] is None  # the server needs `---` first


def test_heading_text_and_ids_drop_underscore_emphasis_but_keep_snake_case():
    result = render("# _Title_ and __bold__\n\n## my_snake_case word\n")
    assert [h["text"] for h in result["headings"]] == ["Title and bold", "my_snake_case word"]
    assert [h["id"] for h in result["headings"]] == ["md-title-and-bold", "md-my_snake_case-word"]


def test_heading_text_follows_what_the_heading_shows():
    result = render("# 2 * 3 * 4\n\n# <https://x.test>\n\n# `a_b` and *em* [link](https://y.test)\n")
    assert [h["text"] for h in result["headings"]] == ["2 * 3 * 4", "https://x.test", "a_b and em link"]
    assert result["headings"][0]["id"] == "md-2-3-4"


def test_an_existing_personal_copy_opens_in_source():
    _, script, _ = page_parts()
    assert 'openFlow(existing.id, null, undefined, "source")' in script


def test_bare_urls_with_many_closing_parentheses_stay_linear():
    html = html_of("see https://x.test/a(b)" + ")" * 60000 + " end")
    assert 'href="https://x.test/a(b)"' in html and html.endswith(")" * 60000 + " end</p>")


def test_hiding_the_contents_moves_focus_to_the_toolbar_button(ui):
    assert ui["hidden"]["focus_moved"] == 1


def test_image_alt_text_with_many_unmatched_openers_stays_fast():
    html = html_of("![" + "_a " * 20000 + "](x.png) and ![alt *em*](y.png)")
    assert html.startswith("<p>") and "alt em" in html


def test_a_long_code_span_with_a_leading_space_stays_fast():
    assert html_of("` " + "x " * 30000 + "y`").startswith("<p><code>")
    assert html_of("` a `") == "<p><code>a</code></p>"


def test_atx_headings_and_whitespace_heavy_lines_stay_fast():
    assert html_of("# a" + " " * 60000 + "x").startswith("<h1")
    assert [h["text"] for h in render("# Title ##\n\n## C#\n\n### x #y\n\n#\n\n####### no\n")["headings"]] == ["Title", "C#", "x #y", ""]
    for hostile in ("-" + " " * 60000 + "x", "| a |\n|" + "- " * 20000, "a" + " " * 60000 + "\n=="):
        render(hostile)  # the harness call has a 60 s timeout


def test_frontmatter_follows_the_server_contract():
    assert render('---\n"quoted key": 1\n---\n# T\n')["meta"] == '"quoted key": 1'
    assert render("---\n# comment\n  indented: 1\n---\n")["meta"] == "# comment\n  indented: 1"
    assert render("---\npersona: x\n...\n# T\n")["meta"] is None  # `...` does not close it
    assert render("---\n- a\n- b\n---\n")["meta"] is None  # a list is not a mapping
    assert render("---\nplain text\n---\n")["meta"] is None


def test_many_short_lines_in_one_paragraph_stay_linear():
    html = html_of("a\n" * 40000)
    assert html.startswith("<p>a a a") and html_of("x  \ny") == "<p>x<br>y</p>"


def test_unmatched_backticks_in_link_labels_and_emphasis_stay_fast():
    for hostile in ("[" + "` " * 20000 + "](https://x.test)", "[a`](b) " * 8000, "*" + "`a" * 20000 + "*",
                    "".join("`" * n + " " for n in range(1, 400))):
        assert render(hostile)["html"].startswith("<p>")  # the harness call has a 60 s timeout
    assert html_of("[`a`](https://x.test)") == '<p><a href="https://x.test" target="_blank" rel="noopener noreferrer"><code>a</code></a></p>'


def test_a_long_unmatched_backtick_run_in_a_link_label_is_skipped_whole():
    assert render("[" + "`" * 50000 + "](https://x.test) tail")["html"].startswith("<p>")
