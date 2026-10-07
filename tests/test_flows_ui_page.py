"""The flow editor page, run in Node against a stub DOM and fetch: sign-in, search, views, layout and sync."""
import json
import os
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


# The open item's controls, moved from the row beside its title into the header (issue #163).
ITEM_CONTROLS = ("e-seg", "e-toc", "history", "toggle-upstream", "copy-user", "copy-repo", "delete", "save",
                 "c-seg", "c-toc", "c-toggle", "h-seg", "h-toc", "h-file")


def test_the_open_items_controls_sit_in_the_header_between_the_version_and_the_tabs():
    html, _, _ = page_parts()
    header = html[html.index("<header>"):html.index("</header>")]
    main = html[html.index('<main id="main">'):html.index("</main>")]
    group = re.search(r'<div id="item-actions"[^>]*>', header).group(0)
    assert 'role="group"' in group and re.search(r'aria-label="[^"]+"', group) and 'class="hidden"' in group
    start, end = header.index('id="item-actions"'), header.index('id="tabs"')
    assert header.index('id="version"') < start
    for name in ITEM_CONTROLS:
        assert html.count(f'id="{name}"') == 1, name
        assert start < header.index(f'id="{name}"') < end, name
        assert f'id="{name}"' not in main, name
    assert 'id="title"' in main and 'id="c-title"' in main  # the title stays above the document
    assert "primary" not in re.search(r'<button id="new"[^>]*>', html).group(0)
    assert "primary" in re.search(r'<button id="save"[^>]*>', html).group(0)


def css_rules(style):
    """The stylesheet as {media condition, "" at the top level: {selector: {property: value}}}."""
    text, rules, i = re.sub(r"/\*[\s\S]*?\*/", "", style), {}, 0

    def add(media, body):
        for selector, declarations in re.findall(r"([^{}]+)\{([^{}]*)\}", body):
            rule = rules.setdefault(media, {}).setdefault(" ".join(selector.split()), {})
            for declaration in declarations.split(";"):
                if ":" in declaration:
                    name, value = declaration.split(":", 1)
                    rule[name.strip()] = " ".join(value.split())

    while (at := text.find("@media", i)) >= 0:
        add("", text[i:at])
        start = text.index("{", at)
        depth, end = 1, start + 1
        while depth:
            depth += {"{": 1, "}": -1}.get(text[end], 0)
            end += 1
        add(" ".join(text[at + len("@media"):start].split()), text[start + 1:end - 1])
        i = end
    add("", text[i:])
    return rules


def test_the_group_goes_where_it_fits_as_measured_not_by_breakpoints():
    assert run("place") == {"on_pane": "tab", "past_corner": "inline", "after_version": "inline",
                            "too_wide": "row", "narrow": "stack"}
    # Opening a flow and resizing the window place the group again, on the group and on #main.
    assert run("ui_place") == {"opened": ["tab", "tab"], "inline": ["inline", "inline"], "row": ["row", "row"],
                               "stack": ["stack", "stack"], "back": ["tab", "tab"]}
    _, script, style = page_parts()
    # So do the buttons that change the group's width.
    assert re.search(r"view\.apply = \(\) => \{[\s\S]*?schedulePlacement\(\);", script)
    relabels = re.findall(r'\$\("toggle-upstream"\)\.textContent = "[^"]+";(\n\s*schedulePlacement\(\);)?', script)
    assert relabels == ["\n  schedulePlacement();", "\n  schedulePlacement();"], relabels
    css = css_rules(style)
    assert not [selector for media, rules in css.items() if media for selector in rules if "item-actions" in selector]


def test_the_header_gives_each_place_its_layout():
    css = css_rules(page_parts()[2])
    top = css[""]
    assert {"flex": "1 1 0", "min-width": "0", "justify-content": "flex-end"}.items() <= top["#item-actions"].items()
    assert top["#history"] == {"width": "8em"}
    assert top["#tabs"] == {"flex-wrap": "wrap"}
    assert top["#e-actions, #c-actions, #h-actions"]["flex-wrap"] == "nowrap"  # measured on one line
    # Labels never wrap, so the group cannot shrink below the width it needs while it is measured.
    assert top["#item-actions button, #item-actions select"] == {"padding": "4px 6px", "white-space": "nowrap"}
    # In its own row a tab starts right of the pane's rounded corner and of its own joint: on the
    # pane's straight top edge.
    assert top[":root"]["--tab-start"] == "calc(var(--list-width) + var(--edge) + var(--r-block) + var(--r-joint))"
    place = '#item-actions[data-place="{}"]'.format
    assert top[place("inline")] == {"justify-content": "flex-start", "margin-bottom": "4px"}
    assert top[f'{place("row")}, {place("stack")}'] == {"order": "1", "flex-basis": "100%"}
    assert top[place("row")] == {"margin-left": "var(--tab-start)"}
    assert top[place("stack")] == {"margin": "4px 0"}
    assert top[f'{place("row")} + #tabs, {place("stack")} + #tabs'] == {"border-left": "0", "padding-left": "0"}
    assert top[f'{place("row")} > div, {place("stack")} > div'] == {"flex-wrap": "wrap"}
    # After the version, or with the list between, the tab keeps the tint but is not joined.
    assert top[f'{place("inline")} > div, {place("stack")} > div'] == {"padding-bottom": "8px",
                                                                       "border-radius": "var(--r-tab)"}
    joints = ", ".join(f"{place(name)} > div::{side}" for name in ("inline", "stack") for side in ("before", "after"))
    assert top[f'{joints}, {place("row")} > div::after'] == {"display": "none"}
    # In its own row the tab continues the pane's right edge.
    assert top['#main[data-place="row"] > #editor, #main[data-place="row"] > #component, #main[data-place="row"] > #hist'] == {
        "border-top-right-radius": "0"}


# The open item's group ends at the divider and is the tab of its pane (issue #186).
def test_the_group_ends_at_the_divider_as_the_tab_of_the_items_pane():
    css = css_rules(page_parts()[2])
    top = css[""]
    assert top["#item-actions:not(.hidden) + #tabs"] == {"border-left": "1px solid var(--line)", "padding-left": "8px"}
    tab = top["#e-actions, #c-actions, #h-actions"]
    assert {"justify-content": "flex-end", "background": "var(--tint)",
            "border-radius": "var(--r-tab) var(--r-tab) 0 0"}.items() <= tab.items()
    # The tab's padding matches the other header items' margins: it reaches down to the pane where they
    # leave the gap to the panels, and the header keeps its height when an item opens or closes.
    assert tab["padding"] == "8px 8px var(--edge)" and top["header > *"]["margin"] == "8px 0 var(--edge)"
    assert top["#item-actions"]["margin"] == "0" and top["main"]["padding"] == "0 var(--edge) var(--edge)"
    # Only the tab, its joints and the open item's pane are tinted: the version, the section tabs,
    # New flow, the list, the welcome pane and the sign-in screen keep the normal colors.
    tinted = {selector for media in css.values() for selector, rule in media.items()
              if any("var(--tint)" in value for value in rule.values())}
    assert tinted == {"#e-actions, #c-actions, #h-actions", "#e-actions::before, #c-actions::before, #h-actions::before",
                      "#e-actions::after, #c-actions::after, #h-actions::after", "#editor, #component, #hist"}
    # Forced colors replace the tint and the bars; an outline keeps the shapes.
    assert top["nav, main > section, #e-actions, #c-actions, #h-actions, .list-tabs button.active::after"] == {
        "outline": "1px solid transparent", "outline-offset": "-1px"}
    # In the narrow layout the list sits between the header and the pane, and wrapped header rows
    # stay close; the "stack" place shapes the tab.
    narrow = css["(max-width: 760px)"]
    assert narrow["header > *"] == {"margin": "4px 0"}
    assert narrow["main"]["padding-top"] == "8px"


def test_the_list_starts_as_far_below_the_search_field_as_the_field_starts_below_the_panel_top():
    top = css_rules(page_parts()[2])[""]
    assert top[".search"] == {"flex": "none", "position": "relative", "padding": "11px 11px 0"}
    assert top["#items"]["padding"] == "11px"
    # The match count sits at the field's right end instead of reserving a row under it.
    assert {"position": "absolute", "top": "11px", "bottom": "0"}.items() <= top["#search-count"].items()
    assert top[".search input"]["padding-right"] == "6em"
    # User and System are tabs, not buttons: no frame, and a bar under the shown one.
    assert top[".search:has(#search-count:empty) input"] == {"padding-right": "8px"}  # room only while it shows
    assert top["#search::-webkit-search-cancel-button"] == {"display": "none"}  # Esc clears instead
    html, script, _ = page_parts()
    assert re.search(r'<div class="search">\s*<input type="search" id="search"[^>]*>\s*<span id="search-count"', html)
    # User and System are tabs, not buttons: no frame, and a bar under the shown one.
    assert {"position": "relative", "border": "0", "background": "none"}.items() <= top[".list-tabs button"].items()
    assert "margin-top" not in top[".list-tabs"] and top[".list-tabs"]["margin-bottom"] == "8px"
    assert top[".list-tabs button.active::after"]["background"] == "var(--accent)"
    assert 'seg.className = "list-tabs"' in script


def test_every_corner_radius_is_a_token_and_no_two_tokens_share_a_value():
    _, _, style = page_parts()
    tokens = dict(re.findall(r"--(r-[a-z-]+): ([^;]+);", re.search(r":root \{([^}]*)\}", style).group(1)))
    assert len(tokens) >= 15
    assert len(set(tokens.values())) == len(tokens), tokens
    declarations = re.findall(r"(border(?:-[a-z]+)*-radius): ([^;}]+)", style)
    assert len(declarations) >= 15
    for name, value in declarations:
        for token, other in re.findall(r"var\(--([a-z-]+)\)|(\S+)", value):
            assert token in tokens or other == "0", (name, value)  # 0 only where the tab joins its pane


def _contrast(first, second):
    def luminance(color):
        channels = [int(color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
        return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]
    light, dark = sorted((luminance(first), luminance(second)), reverse=True)
    return (light + 0.05) / (dark + 0.05)


def test_text_on_the_tint_keeps_a_contrast_of_at_least_4_5_in_both_themes():
    _, _, style = page_parts()
    themes = re.findall(r":root \{([^}]*)\}", style)
    assert len(themes) == 2  # light, and dark under prefers-color-scheme
    for theme in themes:
        colors = dict(re.findall(r"--([a-z-]+): (#[0-9a-f]{6})", theme))
        for name in ("text", "muted"):
            assert _contrast(colors[name], colors["tint"]) >= 4.5, (name, colors[name], colors["tint"])


@pytest.fixture(scope="module")
def panes():
    return run("ui_panes")


def test_the_header_shows_the_controls_of_the_open_item_only(panes):
    welcome = {"shown": ["welcome"], "group": False, "e_actions": False, "c_actions": False}
    flow = {"shown": ["editor"], "group": True, "pane": "editor", "e_actions": True, "c_actions": False}
    rule = {"shown": ["component"], "group": True, "pane": "component", "e_actions": False, "c_actions": True}
    assert panes["start"] == {**welcome, "pane": None}
    assert panes["flow"] == flow
    assert panes["rules"] == {**welcome, "pane": "welcome"}
    assert panes["rule"] == {**rule, "title": "skill-a"}
    assert panes["switched"] == 1
    assert (panes["flow_described_by"], panes["rule_described_by"]) == ("title", "c-title")  # the item's name
    assert panes["back_to_flows"] == {**welcome, "pane": "welcome"}
    assert panes["deleted"] == {**welcome, "pane": "welcome", "deletes": 1}


def test_a_delete_that_answers_late_closes_only_the_deleted_flow(panes):
    assert panes["late_delete_same_flow"] == {"shown": ["welcome"], "group": False, "pane": "welcome",
                                              "e_actions": False, "c_actions": False}
    assert panes["late_delete_other_flow"] == {"shown": ["editor"], "group": True, "pane": "editor",
                                               "e_actions": True, "c_actions": False, "title": "plain"}
    assert panes["late_delete_rule"] == {"shown": ["component"], "group": True, "pane": "component", "e_actions": False,
                                         "c_actions": True, "title": "skill-a",
                                         "deletes": ["user:doc", "user:doc", "user:doc", "user:plain"]}


def test_a_flow_that_loads_after_sign_in_was_lost_shows_no_controls():
    result = run("ui_signout")
    assert result["signed_out"] == {"signin": True, "main_hidden": True}
    assert result["late"]["title"] == "doc"  # the late response did arrive and was applied
    assert result["late"]["group"] is False and result["after"]["group"] is False


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


@pytest.mark.parametrize("scenario, first_hidden, after_header_button, stored", [
    ("ui_narrow", True, False, [["agents-ui-toc-hidden", "0"]]),
    ("ui_narrow_stored", False, True, [["agents-ui-toc-hidden", "1"]]),
    ("ui_nostorage", False, True, []),
])
def test_contents_default_to_hidden_on_narrow_screens_and_survive_missing_storage(scenario, first_hidden, after_header_button, stored):
    result = run(scenario)
    assert result["opened"]["toc_hidden"] is first_hidden
    assert result["after_header_button"]["toc_hidden"] is after_header_button
    assert result["stored_after"] == stored


def test_the_contents_styles_hide_with_classes_and_reveal_on_hover():
    _, _, style = page_parts()
    hover = r"\.mdview\.toc-hidden:not\(\.toc-dismissed\) "
    assert re.search(hover + r"\.md-edge:hover ~ \.md-toc[^{]*\{[^}]*transform: none", style)
    assert re.search(hover + r"\.md-toc:hover[^{]*\{[^}]*transform: none", style)
    assert not re.search(r"\.mdview\.toc-hidden \.md-(edge|toc):hover", style)  # every hover reveal yields to a dismissal
    assert re.search(r"\.mdview\.toc-hidden \.md-toc:focus-within[^{]*\{[^}]*transform: none", style)  # focus does not
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


def test_hiding_the_contents_moves_focus_to_the_header_button(ui):
    assert ui["hidden"]["focus_moved"] == 1


def test_hiding_from_the_panel_waits_for_a_new_hover_to_reveal_it(ui):
    assert ui["opened"]["dismissed"] is False and ui["hidden"]["dismissed"] is True
    assert ui["dismissal"] == {
        "over_panel": True, "over_document": False, "hidden_again": True,
        "kept_open": {"toc_hidden": False, "dismissed": False},  # pinning ends a dismissal the pointer never ended
        "header_hidden": {"toc_hidden": True, "dismissed": False},  # the pointer is on the header, not on the panel
    }
    assert ui["skill_dismissal"] == {"toc_hidden": True, "dismissed": True, "over_panel": True, "over_edge": False}


def test_image_alt_text_with_many_unmatched_openers_stays_fast():
    html = html_of("![" + "_a " * 20000 + "](x.png) and ![alt *em*](y.png)")
    assert html.startswith("<p>")  # past the work budget the later image may stay literal


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


def test_image_alt_text_keeps_literal_punctuation_and_drops_real_markers():
    assert html_of("![2 * 3 and ~home~](x.png)") == "<p>2 * 3 and ~home~</p>"
    assert html_of("![alt *em* `code`](y.png)") == "<p>alt em code</p>"
def test_a_persona_save_cannot_cancel_a_navigation_that_is_still_loading():
    steps = run("persona_race")
    assert steps["opened"] == {"title": "Alpha", "save_disabled": False, "reset_disabled": True}
    assert steps["saving"]["save_disabled"] and steps["saving"]["reset_disabled"]
    # The stale save answered while Beta loads: actions stay disabled, Alpha is not restored.
    assert steps["stale_save_answered"] == {"title": "Alpha", "save_disabled": True, "reset_disabled": True}
    assert steps["after_navigation"] == {"title": "Beta", "save_disabled": False, "reset_disabled": True}


# --- Agents tab ----------------------------------------------------------------------------

@pytest.fixture(scope="module")
def agents():
    return run("agents")


def test_the_agents_tab_comes_right_after_flows():
    html, _, _ = page_parts()
    assert re.findall(r'<button data-tab="([a-z]+)"[^>]*>([A-Za-z]+)</button>', html) == [
        ("flows", "Flows"), ("agents", "Agents"), ("rules", "Rules"), ("skills", "Skills"), ("implants", "Implants"),
        ("history", "History")]


def test_the_agents_tab_lists_display_names_with_their_roles_in_id_order(agents):
    listed = agents["list"]
    assert listed["list"] == ["Agents (3)", "Alpha Agent", "Beta Agent", "Gamma Agent"]
    assert listed["roles"] == ["Plans releases", "Draws screens", "gamma_agent"]  # the ID when the role is empty
    assert listed["new_hidden"] is True


def test_agents_are_found_by_a_routing_keyword_in_text_and_by_name(agents):
    assert agents["keyword"] == {"list": ["Agents (3)", "Beta Agent [in text]"], "count": "1 of 3", "query": "wireframe"}
    assert agents["rules_query"] == "" and agents["query_kept"] == agents["keyword"]
    assert agents["by_name"] == {"list": ["Agents (3)", "Gamma Agent"], "count": "1 of 3", "query": "gamma_agent"}


def test_an_agent_shows_its_facts_and_prompt_without_a_switch(agents):
    alpha = agents["alpha"]
    assert (alpha["title"], alpha["meta"], alpha["description"]) == ("Alpha Agent", "alpha_agent", "Plans releases")
    assert alpha["facts"] == [["Tone", "Calm"], ["Trigger command", "/alpha"], ["Aliases", "/old_alpha, /legacy_alpha"],
                              ["Routing keywords", "release"], ["Core skills", "skill-a"],
                              ["Capable skills", "skill-b, skill-c"], ["Preferred implants", "implant-x"]]
    assert alpha["tags"] == ["dt", "dd"]
    assert alpha["hidden"] == {"toggle": True, "notice": True, "warning": True, "facts": False}
    view = alpha["view"]
    assert view["toc"] == ["md-toc-item md-l1:Identity", "md-toc-item md-l1:Protocol"] and not view["hidden"]
    assert view["html"].startswith('<h2 id="md-identity">')  # no Metadata block: the body has no frontmatter
    assert "&lt;script&gt;x&lt;/script&gt;" in view["html"] and "<script" not in view["html"]
    assert alpha["source_hidden"] and alpha["body"] == "## Identity\n\nAlpha body <script>x</script>\n\n## Protocol\n\nSteps\n"
    assert agents["alpha_source"] == {"view_hidden": True, "source_hidden": False}
    assert agents["component_writes"] == []  # the hidden switch does nothing for an agent


def test_rows_without_a_value_are_left_out(agents):
    gamma = agents["gamma"]
    assert gamma["facts"] == [["Tone", "Dry"], ["Trigger command", "/gamma"]]  # no Aliases, keywords or skills
    assert gamma["description"] == "" and gamma["view"]["no_toc"]


def test_another_tab_shows_its_switch_and_notice_again_and_hides_the_facts(agents):
    skill = agents["skill"]
    assert (skill["title"], skill["meta"]) == ("skill-a", "declared by alpha_agent (core)")
    assert skill["hidden"] == {"toggle": False, "notice": False, "warning": False, "facts": True}


def test_the_persona_picker_keeps_the_short_agent_listing(agents):
    assert agents["agent_requests"] == ["/ui/api/agents?with_content=1", "/ui/api/agents?with_content=1", "/ui/api/agents"]
    assert agents["persona_options"] == ["Alpha Agent (alpha_agent) = alpha_agent", "Beta Agent (beta_agent) = beta_agent",
                                         "Gamma Agent (gamma_agent) = gamma_agent"]


def test_an_open_agent_has_its_view_controls_in_the_header_without_a_switch(agents):
    assert agents["list"]["header"] == {"shown": ["welcome"], "group": False, "pane": "welcome",
                                        "e_actions": False, "c_actions": False}
    assert agents["alpha"]["header"] == {"shown": ["component"], "group": True, "pane": "component", "e_actions": False,
                                         "c_actions": True, "seg": ["Rendered", "Source"], "toc": True}
    assert agents["alpha"]["hidden"]["toggle"]  # the switch shares the group and stays hidden for an agent


# --- library sync (#170): the header chip, the Sync page, its wizard and the editor's notice ---

def run_sync(scenario):
    """A sync scenario; times print in UTC, so "14:05" means 14:05Z."""
    completed = subprocess.run([NODE, str(ROOT / "tests" / "flows_ui_page_harness.mjs"), str(PAGE), scenario],
                               capture_output=True, text=True, timeout=60, check=True,
                               env={**os.environ, "TZ": "UTC"})
    return json.loads(completed.stdout)


def test_the_chip_says_the_state_in_words():
    result = run_sync("sync_chip")
    states = {name: value and value["text"] for name, value in result["states"].items()}
    assert states == {
        "none": None, "unknown": None, "off": "Sync off", "synced": "Synced 2m ago", "just_now": "Synced just now",
        "hours": "Synced 3h ago", "stale": "Synced 2d ago", "pending": "3 pending", "syncing": "Syncing…",
        "loop_syncing": "Syncing…", "offline": "Offline, retry 14:05", "paused": "Paused",
        "attention": "Needs attention", "waiting": "Needs attention", "conflicts": "2 conflicts",
        "one_conflict": "1 conflict"}
    tones = {name: value and value["tone"] for name, value in result["states"].items()}
    assert (tones["synced"], tones["stale"], tones["offline"], tones["pending"]) == ("ok", "warn", "warn", "busy")
    assert result["started_off"] == {"text": "Sync off", "hidden": False, "tone": "off", "label": "Library sync: Sync off"}


def test_the_chip_opens_the_sync_page_in_place_of_the_list_and_a_tab_leaves_it():
    result = run_sync("sync_chip")
    opened = result["opened"]
    assert opened["page_shown"] and opened["main_hidden"] and opened["chip"]["pressed"] == "true"
    assert opened["tabs"] == ["false"] * 6 and opened["title_focused"] == 1
    assert result["closed"] == {"chip": {**opened["chip"], "pressed": "false"}, "page_hidden": True, "main_hidden": False,
                                "tabs": ["true", "false", "false", "false", "false", "false"]}
    assert result["tab"] == {"page_hidden": True, "main_hidden": False,
                             "tabs": ["false", "false", "true", "false", "false", "false"]}


def test_the_status_is_polled_every_30_seconds_only_while_the_tab_is_visible():
    result = run_sync("sync_poll")
    assert result["start"] == {"reads": 1, "timers": 1}
    assert result["after_30s"] == {"reads": 2, "timers": 1}
    assert result["hidden"] == {"reads": 2, "timers": 0} and result["hidden_after_30s"] == {"reads": 2}
    assert result["visible"] == {"reads": 3, "timers": 1}  # read again at once when shown
    assert result["visible_after_30s"] == {"reads": 4, "timers": 1}


@pytest.fixture(scope="module")
def wizard():
    return run_sync("sync_wizard")


def test_the_wizard_signs_in_to_github_with_a_device_code(wizard):
    assert wizard["connect"]["steps"] == ["*1. Connect", "2. Repository", "3. Identity", "4. What syncs", "5. Preview"]
    assert wizard["connect"]["buttons"] == ["Sign in to GitHub", "Use this SSH URL"]
    assert wizard["code"] == {"code": "WDJB-MJHT", "href": "https://github.com/login/device", "rel": "noopener noreferrer",
                              "target": "_blank", "polls_waiting": 1}
    assert wizard["still_waiting"] == {"polls_waiting": 1, "code_shown": 1}  # pending: polls again after the interval
    assert wizard["repository"]["alert"] == "Signed in to GitHub as @octocat."
    assert wizard["repository"]["checked"] == ["library:octocat/agents-library"]  # an existing library is the default


def test_the_wizard_creates_the_repository_and_asks_for_the_identity(wizard):
    assert wizard["identity"]["alert"] == "Created the private repository octocat/my-library."
    assert wizard["identity"]["label"] == "mac-1a2b"  # the suggested label, never the hostname
    assert wizard["identity_refused"]["alert"] == {"text": "Enter a name for commits.", "className": "notice error"}
    setups = [call for call in wizard["calls"] if call[1] == "/ui/api/sync/setup"]
    assert setups == [["POST", "/ui/api/sync/setup", {"name": "Owner", "email": "owner@example.com", "label": "mac-1a2b",
                                                       "github": "octocat/my-library"}]]


def test_the_wizard_checks_access_and_offers_to_add_a_missing_deploy_key(wizard):
    assert wizard["access"]["headings"] == ["Access"]
    assert wizard["access"]["buttons"] == ["Start over", "Add this machine's key on GitHub", "Check access"]
    assert wizard["key_added"]["text"] == "added this machine's deploy key to octocat/my-library"
    assert wizard["scope"]["alert"] == "Access works; the repository is empty."


def test_the_wizard_scope_is_all_on_by_default_and_including_needs_the_upload_list(wizard):
    boxes = {name: (checked, disabled) for name, checked, disabled in wizard["scope"]["boxes"]}
    assert boxes == {"Personal flows": (True, False), "Persona choices": (True, False),
                     "Saved versions (history)": (True, False), "Component switches": (True, False),
                     "github.com/o/r": (True, False), "repos/home-1f2e": (False, True),  # machine-local
                     "Ask before uploading the flows of a repository that is new to the library": (False, False)}
    assert wizard["excluded"]["alert"]["text"] == "history no longer syncs; its files stay on every machine."
    assert wizard["include_asks"]["buttons"][:2] == ["Upload them", "Cancel"]
    assert wizard["included"]["alert"]["text"] == "history syncs again."
    scopes = [call[2] for call in wizard["calls"] if call[:2] == ["PUT", "/ui/api/sync/scopes"]]
    assert scopes == [{"exclude": ["history"]}, {"include": ["history"]}, {"include": ["history"], "confirm": "inc1"}]
    assert ["PUT", "/ui/api/sync/settings", {"ask_new_repositories": True}] in wizard["calls"]


def test_the_wizard_preview_lists_the_plan_and_start_sends_its_hash(wizard):
    text = wizard["preview"]["text"]
    for part in ("Upload this library to the empty repository.", "Upload (1)", "user:a  common/a.md (120 B)",
                 "Not uploaded: possible credentials found by the scanner (1)", "common/secret.md (github_token)",
                 "github.com/o/r: 2 files, new to the library"):
        assert part in text, part
    assert wizard["preview"]["buttons"] == ["Back", "Prepare again", "Start sync"]
    assert ["POST", "/ui/api/sync/start", {"confirm": "h123", "confirm_private": False}] in wizard["calls"]
    assert wizard["started"]["headings"] == ["Status", "Machines", "Conflicts", "What syncs", "Access", "Identity",
                                             "Activity", "Disconnect"]
    assert wizard["started"]["alert"]["text"] == "Sync started: sent 1 file, received 0."
    # Every request went to the daemon; the device code never reached the page.
    assert all(path.startswith("/ui/api/sync") for _, path, _ in wizard["calls"])


def test_the_ssh_wizard_confirms_the_host_key_and_the_privacy_it_cannot_check():
    result = run_sync("sync_wizard_ssh")
    assert result["identity"]["steps"][1] == "2. Repository (skipped)"
    assert result["bad_label"]["text"].startswith("The machine label must be")
    assert result["host_keys"]["radios"] == ["SHA256:aaaa", "SHA256:bbbb"]
    assert result["no_choice"]["text"] == "Choose the fingerprint that matches the host's."
    assert result["key"]["key"].startswith("ssh-ed25519 ") and "Copy" in result["key"]["buttons"]
    assert "Start sync (disabled)" in result["unsure"]["buttons"]  # until the owner confirms privacy
    assert "Only I can read this repository." in result["unsure"]["text"]
    assert "Start sync" in result["confirmed"]
    assert result["calls"][1][2]["trust_host_key"] == "SHA256:bbbb"
    assert ["POST", "/ui/api/sync/start", {"confirm": "h9", "confirm_private": True}] in result["calls"]
    # A plan that changed meanwhile is not started; the preview is prepared again.
    assert result["changed"]["alert"]["text"].startswith("the preview changed")
    assert result["calls"][-1] == ["GET", "/ui/api/sync/preview", None]


@pytest.fixture(scope="module")
def dashboard():
    return run_sync("sync_dashboard")


def test_the_sync_page_shows_every_section_once_sync_runs(dashboard):
    page = dashboard["dashboard"]
    assert page["headings"] == ["Status", "Machines", "Conflicts", "What syncs", "Access", "Identity", "Activity",
                                "Disconnect"]
    for part in ("Repositoryoctocat/agents-library", "Branchmain", "Next fetchin 4 min", "(2m ago)", "Sync now", "Pause"):
        assert part in page["status"], part
    assert "mac-1a2b (this machine)" in page["machines"] and page["machine_buttons"] == 1  # no Remove for this one
    assert "Received from laptop: user:tool" in page["activity"]
    assert "Non-Markdown files changed (scripts): common/tool.py" in page["activity"]
    assert "github.com/o/r" in page["scopes"] and "Exclude" in page["scopes"] and "Approve" in page["scopes"]
    assert "GitHub: @octocat" in page["access"] and "Regenerate key" in page["access"]
    assert dashboard["chip"]["text"] == "2 conflicts"


def test_the_sync_page_sends_each_action_to_the_daemon(dashboard):
    keep, mine = "20261005T120000000000Z-aaaaaaaaaa", "20261005T120100000000Z-bbbbbbbbbb"
    assert dashboard["actions"] == [
        ["POST", "/ui/api/sync/machines/remove", {"id": 2}],
        ["POST", "/ui/api/sync/conflicts/resolve", {"id": keep, "action": "keep"}],
        ["POST", "/ui/api/sync/conflicts/resolve", {"id": mine, "action": "mine"}],
        ["POST", "/ui/api/sync/conflicts/resolve", {"id": keep, "action": "dismiss"}],
        ["GET", f"/ui/api/sync/conflict?id={mine}", None],
        ["PUT", "/ui/api/sync/scopes", {"exclude": ["repos/abc"]}],
        ["PUT", "/ui/api/sync/scopes", {"approve": ["repos/def"]}],
        ["PUT", "/ui/api/sync/scopes", {"approve": ["repos/def"], "confirm": "ap1"}],
        ["PUT", "/ui/api/sync/settings", {"paused": True}],
        ["POST", "/ui/api/sync/run", {}],
        ["PUT", "/ui/api/sync/settings", {"name": "Owner Two", "email": "owner@example.com", "label": "mac-1a2b"}],
        ["POST", "/ui/api/sync/check", {}],
        ["POST", "/ui/api/sync/key/regenerate", {}],
        ["POST", "/ui/api/sync/github/forget", {}],
    ]
    assert "Approving repos/def uploads 1 file:" in dashboard["approve_asks"]
    assert dashboard["sync_now"]["text"] == "Synced: sent 1 file, received 0."
    assert dashboard["revoke"] == ["https://github.com/settings/applications"]


def test_a_conflict_opens_side_by_side_with_the_current_text(dashboard):
    # Not a flow's text: both versions read-only on the Sync page.
    assert dashboard["compare"] == [["Current", '{"persona": "a"}', True], ["Yours, kept by sync", '{"persona": "b"}', True]]
    # A flow's text: the editor, current text on the left, the kept version in the split pane.
    opened = dashboard["open_flow"]
    assert opened["page_hidden"] and opened["title"] == "doc" and opened["view_source"]
    assert not opened["side_hidden"] and opened["side"] == "# Mine, kept\n"
    assert opened["side_label"].startswith("Your version, kept by sync on 2026-10-05 12:00")
    assert opened["content"].startswith("---\npersona: tester")


def test_disconnect_says_what_happened_to_the_deploy_key_and_returns_to_the_wizard(dashboard):
    assert dashboard["disconnected"]["alert"]["text"] == (
        "Sync is off on this machine. Removed this machine's deploy key (Agents-Core mac-1a2b) from octocat/agents-library.")
    assert dashboard["disconnected"]["steps"][0] == "*1. Connect" and dashboard["disconnected"]["chip"]["text"] == "Sync off"


def test_a_flow_that_a_sync_updated_while_open_says_so_and_keeps_unsaved_text():
    result = run_sync("sync_notice")
    assert result["other_flow"]["hidden"] and result["same_revision"]["hidden"]  # the revision decides
    assert result["updated"] == {"hidden": False, "text": "Updated from laptop, desk at 14:02.",
                                 "content": "# My unsaved text\n"}
    conflict = result["save_conflict"]
    assert conflict["notice"] == ("Sync updated this flow from laptop, desk at 14:02 while you edited it. "
                                  "Your text is kept on the left.")
    assert conflict["content"] == "# My unsaved text\n" and conflict["side"] == "# Doc from laptop\n"
    assert conflict["side_label"].startswith("Updated from laptop, desk by sync")
    assert conflict["sync_notice"]["hidden"]
    assert result["again"] == {"hidden": False, "text": "Updated from laptop at 14:03."}
    assert result["reloaded"]["hidden"] and result["reloaded"]["content"] == "# Doc again\n"


def test_the_sync_page_keeps_the_header_budget_and_the_csp():
    html, script, style = page_parts()
    header = html[html.index("<header>"):html.index("</header>")]
    # The chip sits beside the version, before the item's controls; it is a button with words.
    assert header.index('id="version"') < header.index('id="sync-chip"') < header.index('id="item-actions"')
    assert re.search(r'<button id="sync-chip"[^>]*aria-controls="sync-page"', header)
    css = css_rules(style)[""]
    # Under the version, in the 32 px its line had: no width taken from the item's controls, so a
    # built-in flow's controls keep the first row at 1250 px (measured in Chromium, not committed).
    assert {"flex-direction": "column", "height": "32px", "margin-right": "auto"}.items() <= css["#brand"].items()
    assert css["header h1"]["line-height"] == "18px" and css["#sync-chip"]["line-height"] == "12px"
    assert css["#sync-chip"]["white-space"] == "nowrap"
    assert css["#sync-page"]["overflow-y"] == "auto"  # it scrolls by itself, like the list
    # The browser never calls GitHub: every request goes through api() to the page's own origin.
    assert "fetch(" not in script.replace('fetch("/ui/api/session"', "").replace("await fetch(path, init)", "")
    assert "github.com" not in script.lower()
    # The placement counts the chip: a tab never covers it.
    assert re.search(r"versionEnd: \$\(\"brand\"\)", script)
    assert run("place") == {"on_pane": "tab", "past_corner": "inline", "after_version": "inline",
                            "too_wide": "row", "narrow": "stack"}


# --- the review round of #170: focus, busy controls, fresh reads, names and robustness -----------

def test_a_rebuilt_part_gives_the_focus_back_to_the_same_control():
    result = run_sync("sync_focus")
    assert result["same_name"] == [0, 1, 0]  # "Remove linux-9f9f", not the first "Remove"
    assert result["same_field"] == [0, 1, 0]  # a switch found by its label
    assert result["fallback"] == [0, 1]  # Pause became Resume: the part's heading
    assert result["outside"] == [0, 1]  # focus elsewhere is left alone (the heading count stays 1)


@pytest.fixture(scope="module")
def more():
    return run_sync("sync_more")


def test_a_busy_page_keeps_the_focus_and_pause_still_answers(more):
    assert more["busy"] == {"sync_now": ["true", False], "pause": [None, False]}  # aria-disabled, never disabled
    assert more["paused_while_busy"] == [["PUT", "/ui/api/sync/settings", {"paused": True}]]
    assert more["after"] == {"sync_now": [None]}


def test_polls_take_the_scanned_status_and_the_page_reads_fresh_when_it_opens(more):
    assert more["fresh_on_open"] == 1 and more["fresh_on_poll"] == 0


def test_a_failed_poll_says_the_status_is_unavailable(more):
    assert more["unavailable"]["text"] == "Sync status unavailable" and more["unavailable"]["tone"] == "warn"
    assert more["available_again"]["text"] == "2 conflicts"


def test_list_buttons_have_names_of_their_own_and_foreign_keys_stay(more):
    names = more["names"]
    assert len(names) == len(set(names))  # two records of one file differ in their time
    assert {"Remove linux-9f9f", "Open the conflict on user:doc, recorded 2026-10-05 12:00:00",
            "Open the conflict on user:doc, recorded 2026-10-05 12:00:07",
            "Use my version of user:doc (persona), recorded 2026-10-05 12:01:00",
            "Approve repos/def", "Copy the public key"} <= set(names)
    assert more["remove_buttons"] == ["Remove linux-9f9f"]  # neither this machine nor a key sync did not add
    assert "Other deploy keys" in more["machines"] and "CI deploy, read-only" in more["machines"]


def test_an_interval_set_elsewhere_shows_in_the_list(more):
    assert more["interval"] == {"options": ["1", "2", "3", "5", "10", "15", "30", "60"], "value": "3"}


def test_bytes_that_are_not_text_say_so(more):
    assert more["binary"] == ["(not text: binary content)", "(not text: binary content)"]


def test_regenerate_key_asks_what_github_will_do_and_refuses_while_signed_out(more):
    assert more["regenerate_question"] == ("Create a new key for this machine? Agents-Core adds it to "
                                           "octocat/agents-library on GitHub, then removes the old one.")
    assert more["signed_out"]["posts"] == 0
    assert more["signed_out"]["alert"]["text"].startswith("Sign in to GitHub first")
    assert more["tab_focused"] == 1  # leaving the page moves the focus to the tab that shows


def test_a_reload_at_the_host_key_step_shows_the_fingerprints_again():
    result = run_sync("sync_hostkey")
    assert result["fingerprints"]["radios"] == ["SHA256:aaaa", "SHA256:bbbb"]
    assert result["fingerprints"]["buttons"] == ["Trust this key"]
    assert result["calls"] == [["POST", "/ui/api/sync/setup", {"again": True}],
                               ["POST", "/ui/api/sync/setup", {"again": True, "trust_host_key": "SHA256:bbbb"}]]
    assert result["trusted"]["key"].startswith("ssh-ed25519 ") and "Check access" in result["trusted"]["buttons"]


@pytest.fixture(scope="module")
def round2():
    return run_sync("sync_round2")


def test_the_wizard_starts_from_the_settings_not_from_a_status_a_scan_old(round2):
    assert round2["wizard_from"] == {"stale_synced": ["connect", None], "stale_off": ["access", "ssh"],
                                     "github": ["access", "github"]}


def test_a_poll_never_replaces_a_fresh_read_that_is_still_pending(round2):
    assert round2["before"] == "2 conflicts" and round2["poll_timers"] == 1
    assert round2["while_fresh"] == {"reads": 1, "chip": "2 conflicts"}  # the fresh read only; no poll was sent
    assert round2["after_fresh"] == {"chip": "Synced 2m ago", "timers": 1}  # its answer, then the next poll


def test_this_machine_is_marked_under_a_title_sync_did_not_give_it(round2):
    machines = round2["machines"]
    assert "laptop key (this machine)" in machines and "GitHub lists no machine" not in machines
    assert "This machine's key is on GitHub as laptop key, a title Agents-Core did not give it" in machines
    assert round2["remove_buttons"] == 0


def test_text_fields_take_no_typing_while_a_request_runs(round2):
    assert round2["busy_field"] == {"read_only": True, "aria": "true"}
    assert round2["idle_field"] == {"read_only": False, "aria": None}


def test_a_choice_made_while_a_request_runs_goes_back():
    result = run_sync("sync_busy_radio")
    assert result["while_busy"] == [["SHA256:aaaa", True], ["SHA256:bbbb", False]]
    assert result["sent"][-1] == {"again": True, "trust_host_key": "SHA256:aaaa"}


def test_a_github_setup_that_stopped_before_its_host_keys_is_set_up_again():
    result = run_sync("sync_hostkey_github")
    assert result["stopped"]["buttons"] == ["Set up again"]  # nothing to confirm on GitHub
    assert "stopped before GitHub's host keys were stored" in result["stopped"]["text"]
    assert result["calls"] == [["POST", "/ui/api/sync/setup", {"again": True}]]
    assert "added this machine's deploy key to octocat/agents-library" in result["again"]["text"]
    assert result["again"]["buttons"] == ["Start over", "Check access"]


def test_a_flow_that_sync_deleted_while_open_says_so():
    result = run_sync("sync_notice")
    assert result["deleted"] == {"hidden": False, "reload_hidden": True,
                                 "text": "Deleted on desk at 14:04 by sync. Your text is still here; copy it to keep it."}
    assert result["deleted_save"] == {"content": "# Kept text\n", "notice": (
        "Sync deleted this flow on desk at 14:04 while you edited it. Your text is kept here; copy it to keep it.")}


# --- the landing page (#188) ------------------------------------------------------------------

@pytest.fixture(scope="module")
def landing():
    return run("landing")


def test_ui_opens_the_landing_page_in_place_of_the_list_and_the_item(landing):
    assert landing["start"] == {"landing": True, "main": False, "active_tabs": [], "item_actions": False,
                                "new_flow": False, "reads": ["/ui/api/overview", "/ui/api/stats"]}


def test_each_block_renders_from_the_api(landing):
    cards = landing["cards"]
    assert "routes each request" in cards["about"]
    assert landing["links"] == [
        ["GitHub", "https://github.com/IEZhu/Agents", "noopener noreferrer"],
        ["README", "https://github.com/IEZhu/Agents#readme", "noopener noreferrer"],
        ["Documentation", "https://github.com/IEZhu/Agents/blob/HEAD/docs/README.md", "noopener noreferrer"],
        ["Issues", "https://github.com/IEZhu/Agents/issues", "noopener noreferrer"]]
    assert cards["stats"].startswith("16 answers in 30 days") and "Today 4 · last 7 days 10" in cards["stats"]
    assert "software_engineer · 9" in cards["stats"] and "Agents · 12" in cards["stats"]
    assert "running for 3h 12m." in cards["stats"] and "Not counted: gone (missing)." in cards["stats"]
    # One bar per day, its level a share of the busiest day.
    assert len(landing["chart"]) == 30 and landing["chart"][10] == "8" and landing["chart"][29] == "4"
    assert landing["chart"].count("0") == 23
    assert landing["app_states"] == ["connected", "none", "configured", "none"]
    assert "● connected" in cards["apps"] and "○ configured" in cards["apps"] and "– not set up" in cards["apps"]
    assert "claude-code 2.0.14 · last seen 1m ago · 12 requests today" in cards["apps"]
    assert "3 requests today came without an app's name" in cards["apps"]
    assert "45 agents; 6 rules, 70 skills, 57 implants" in cards["how"] and "2 repositories in 30 days" in cards["how"]
    assert "microsoft/harrier-oss-v1-270m · 1.1 GiB" in cards["system"]
    assert "28 built-in, 3 personal, 2 in repositories" in cards["system"]
    assert "/opt/agents2.0 GiBCopy" in cards["system"] and "/opt/agents/logsmissingCopy" in cards["system"]


def test_answers_run_along_the_diagram_one_step_at_a_time(landing):
    assert landing["steps"] == 6 and landing["lit"] == [0] and landing["step_timers"] == 1
    assert landing["lit_after_a_step"] == [1]
    # The first statistics run their three newest answers, oldest first.
    assert re.fullmatch(r"\d\d:\d\d An app → software_engineer → Agents", landing["event_line"])
    assert landing["events_list"] == []


def test_the_page_polls_the_statistics_and_reads_the_overview_when_it_opens(landing):
    assert landing["reads_after_a_poll"] == ["/ui/api/overview", "/ui/api/stats", "/ui/api/stats"]


@pytest.mark.parametrize("scenario", ["landing", "landing_stored_off"])
def test_every_tab_leaves_the_landing_page_and_the_version_opens_it_again(scenario):
    tabs = run(scenario)["tabs"]
    assert [entry["tab"] for entry in tabs] == ["flows", "agents", "rules", "skills", "implants", "history"]
    assert all(entry["left"] and entry["back"] for entry in tabs), tabs


def test_the_choice_sends_ui_to_flows_and_survives_a_reload(landing):
    assert landing["stored"] == [["agents-ui-landing-off", "1"]]
    reloaded = run("landing_stored_off")
    assert reloaded["start"] == {"landing": False, "main": True, "active_tabs": ["flows"], "item_actions": False,
                                 "new_flow": True, "reads": []}


def test_the_landing_page_opens_without_storage():
    result = run("landing_nostorage")
    assert result["start"]["landing"] and result["cards"]["stats"].startswith("16 answers") and result["stored"] == []


def test_with_reduced_motion_the_diagram_stays_still_and_the_answers_are_a_list():
    result = run("landing_reduced")
    assert result["lit"] == [] and result["step_timers"] == 0 and result["lit_after_a_step"] == []
    assert result["event_line"] is None and len(result["events_list"]) == 4
    assert result["events_list"][0].endswith("Claude Code → ux_designer (switch) → Agents")


def test_empty_statistics_and_an_app_that_is_configured_but_not_connected():
    result = run("landing_empty")
    assert result["cards"]["stats"].startswith("0 answers in 30 days")
    assert "No answers in the last 30 days." in result["cards"]["stats"] and set(result["chart"]) == {"0"}
    assert result["app_states"] == ["configured", "none", "configured", "none"]
    assert result["event_line"] == "No answers yet." and result["step_timers"] == 0


def test_the_landing_page_keeps_still_under_reduced_motion_and_stacks_on_narrow_screens():
    html, _, style = page_parts()
    assert 'id="version-link" href="#overview"' in html
    assert re.search(r"\.card \{[^}]*border-radius: var\(--r-card\)", style)
    assert re.search(r"@media \(prefers-reduced-motion: reduce\) \{ \.diagram \.step \{ transition: none; \} \}", style)
    assert re.search(r"@media \(max-width: 760px\) \{ #landing-grid, \.tops \{ grid-template-columns: minmax\(0, 1fr\); \} \}", style)


# --- History (#189) -------------------------------------------------------------------------------

@pytest.fixture(scope="module")
def history():
    return run("history")


def test_the_history_tab_lists_repositories_most_recent_first_with_their_age(history):
    assert history["list"] == {"list": ["History (3)", "agents", "project-a", "gone"], "count": "", "query": ""}
    assert history["chips"] == [["2m"], ["3h"], ["unavailable"]]


def test_a_history_opens_like_a_flow_newest_first_with_its_files(history):
    assert history["pane"] == {"hist": True, "welcome": False, "h_actions": True, "group_pane": "hist", "title": "agents",
                               "meta": "/code/agents · 45 entries · 585.9 KiB", "seg": ["true", "false"]}
    assert history["files"] == [["history.md · 45", "current"], ["history/2026-09.md · 2", "2026-09"]]
    assert history["rendered"] == 20 and history["more"] == "Show earlier entries (25 more)"
    (heading, meta, body), second = history["first"]
    assert re.fullmatch(r"\d\d\.\d\d 12:00 · ux_designer · keep", heading) and second[0].endswith(" · lawyer · keep")
    assert meta.endswith(" · 000000000001") and "Outcome: answer 1" in body and "Tags: #t" in body


def test_contents_list_every_entry_grouped_by_day(history):
    assert history["toc_entries"] == 45
    toc = history["toc"]
    assert toc[:4] == ["md-toc-item md-l1 h-day:Today", "md-toc-item md-l2:12:00 ux_designer",
                       "md-toc-item md-l2:11:00 lawyer", "md-toc-item md-l1 h-day:Yesterday"]
    assert re.fullmatch(r"md-toc-item md-l1 h-day:\d\d\.\d\d\.\d{4}", toc[5])


def test_earlier_entries_load_in_portions_as_the_reader_scrolls_or_picks_them(history):
    assert history["after_scroll"] == {"rendered": 40, "more": "Show earlier entries (5 more)"}
    assert history["after_contents"] == {"rendered": 45, "scrolled": 1, "more_hidden": True}
    assert history["reads"][:3] == ["/ui/api/history?workspace=w-1&limit=20",
                                    "/ui/api/history?workspace=w-1&file=current&before=25&limit=20",
                                    "/ui/api/history?workspace=w-1&file=current&before=5&limit=5"]


def test_source_shows_the_file_and_the_select_opens_an_archive(history):
    assert history["source"] == {"value": "---\nrepo: x\n---\nraw current", "label": "history.md (read-only)",
                                 "shown": True, "rendered_hidden": True}
    assert history["archive"]["rendered"] == 2 and history["archive"]["toc_entries"] == 2
    assert history["reads"][3] == "/ui/api/history?workspace=w-1&limit=20&file=2026-09"


def test_the_search_matches_names_here_and_entry_text_on_the_server(history):
    assert history["search_name"] == {"list": ["History (3)", "project-a"], "count": "1 of 3", "query": "proj"}
    assert history["search_pending"]["list"] == ["History (3)", "Searching…"]
    assert history["search_text"] == {"list": ["History (3)", "project-a [in text]"], "count": "1 of 3", "query": "needle"}


def test_a_repository_whose_directory_is_gone_says_so(history):
    assert history["gone"] == {"title": "gone", "notice": "This repository's directory is gone, so its history cannot be read.",
                               "notice_hidden": False, "seg_hidden": True, "file_hidden": True, "rendered_hidden": True}


def test_a_link_opens_the_repository_at_its_entry(history):
    assert history["link"] == {"rendered": 31, "scrolled": 1, "title": "agents"}
    assert history["reads"][-1] == "/ui/api/history?workspace=w-1&limit=20&entry=00000000001f"


def test_delete_entry_asks_naming_the_entry_then_shows_the_file_again(history):
    delete = history["delete"]
    assert re.fullmatch(r"Delete the entry \d\d\.\d\d 12:00 · ux_designer · keep \(000000000001\) from history\.md\? "
                        r"It cannot be undone from this page\.", delete["confirm"])
    assert [(post["workspace"], post["file"], post["entry"]) for post in delete["posted"]] == [("w-1", "current", "000000000001")]
    assert delete["posted"][0]["time"].endswith("Z")
    assert delete["repos_read_again"] == 1 and delete["reread"] == "/ui/api/history?workspace=w-1&limit=20&file=current"
    assert delete["rendered"] == 20 and delete["first"].endswith(" · lawyer · keep") and "44 entries" in delete["meta"]


def test_the_landing_pages_answers_link_to_their_entries(landing):
    # The diagram's line runs the three newest answers, oldest first, and links the one it shows; a link
    # carries the entry's time too, as ids repeat.
    link = r"#history/w-1/00000000000{}/\d{{4}}-\d\d-\d\dT\d\d%3A\d\d%3A\d\d\.\d+Z"
    [shown] = landing["event_links"]
    assert re.fullmatch(link.format(3), shown)
    reduced = run("landing_reduced")["event_links"]
    assert len(reduced) == 4 and all(re.fullmatch(link.format(n + 1), href) for n, href in enumerate(reduced))
