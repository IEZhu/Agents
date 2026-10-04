"""The flow editor page, run in Node against a stub DOM and fetch: sign-in, search, views and layout."""
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


# The open item's controls, moved from the row beside its title into the header (issue #163).
ITEM_CONTROLS = ("e-seg", "e-toc", "history", "toggle-upstream", "copy-user", "copy-repo", "delete", "save",
                 "c-seg", "c-toc", "c-toggle")


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
    assert top["#e-actions, #c-actions"]["flex-wrap"] == "nowrap"  # measured on one line
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
    assert top['#main[data-place="row"] > #editor, #main[data-place="row"] > #component'] == {
        "border-top-right-radius": "0"}


# The open item's group ends at the divider and is the tab of its pane (issue #186).
def test_the_group_ends_at_the_divider_as_the_tab_of_the_items_pane():
    css = css_rules(page_parts()[2])
    top = css[""]
    assert top["#item-actions:not(.hidden) + #tabs"] == {"border-left": "1px solid var(--line)", "padding-left": "8px"}
    tab = top["#e-actions, #c-actions"]
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
    assert tinted == {"#e-actions, #c-actions", "#e-actions::before, #c-actions::before",
                      "#e-actions::after, #c-actions::after", "#editor, #component"}
    # Forced colors replace the tint and the bars; an outline keeps the shapes.
    assert top["nav, main > section, #e-actions, #c-actions, .list-tabs button.active::after"] == {
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
        ("flows", "Flows"), ("agents", "Agents"), ("rules", "Rules"), ("skills", "Skills"), ("implants", "Implants")]


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
