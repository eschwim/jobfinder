from pathlib import Path
from types import SimpleNamespace

import pytest

import jobfinder.tailor as tailor
from jobfinder.config import TailorConfig
from jobfinder.filters import Job
from jobfinder.tailor import (
    TailorError,
    _format_contact,
    _promote_headings,
    _strip_fences,
    _strip_resume_images,
    render_resume_html,
    resolve_template,
    tailor_resume,
)


def _job(**kwargs) -> Job:
    defaults = dict(id="li-1", title="Site Reliability Engineer", company="Acme",
                    site="linkedin", location="Seattle, WA",
                    description="Kubernetes, Python, on-call, SLOs")
    defaults.update(kwargs)
    return Job(**defaults)


def _fake_anthropic(monkeypatch, text, stop_reason="end_turn"):
    captured = {}

    class FakeStream:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get_final_message(self):
            return SimpleNamespace(
                stop_reason=stop_reason,
                content=[SimpleNamespace(type="text", text=text)])

    class FakeMessages:
        def stream(self, **kwargs):
            captured.update(kwargs)
            return FakeStream()

    class FakeClient:
        def __init__(self, api_key=None):
            captured["api_key"] = api_key
            self.messages = FakeMessages()

    monkeypatch.setattr(tailor.anthropic, "Anthropic", FakeClient)
    return captured


class TestStripHelpers:
    def test_strips_base64_icon_definitions_and_uses(self):
        text = ("**Eric**  \n![][image1]Seattle ![][image2]e@x.com\n\nSKILLS\n\n"
                "[image1]: <data:image/png;base64,iVBORw0KGgo=>\n"
                "[image2]: <data:image/png;base64,AAAA>\n")
        out = _strip_resume_images(text)
        assert "base64" not in out
        assert "![][image1]" not in out
        assert "Seattle" in out and "SKILLS" in out

    def test_strips_code_fences(self):
        assert _strip_fences("```markdown\n# Resume\n```") == "# Resume"
        assert _strip_fences("# Resume") == "# Resume"


class TestTailorResume:
    def test_prompt_contains_resume_and_job(self, monkeypatch):
        captured = _fake_anthropic(monkeypatch, "# Tailored")
        out = tailor_resume("MY RESUME TEXT", _job(), "claude-opus-4-8", "key")
        assert out == "# Tailored"
        assert captured["model"] == "claude-opus-4-8"
        assert captured["api_key"] == "key"
        prompt = captured["messages"][0]["content"]
        assert "MY RESUME TEXT" in prompt
        assert "Site Reliability Engineer" in prompt
        assert "Kubernetes" in prompt
        assert "never invent" in captured["system"].lower()
        # positions must keep source order; only bullets may be reordered
        assert "never reorder, merge, or re-date positions" in captured["system"]
        assert "reorder bullets within a position" in captured["system"]

    def test_resume_icons_stripped_from_prompt(self, monkeypatch):
        captured = _fake_anthropic(monkeypatch, "ok")
        tailor_resume("hi\n[image1]: <data:image/png;base64,AAAA>\n",
                      _job(), "m", "k")
        assert "base64" not in captured["messages"][0]["content"]

    def test_null_description_gets_placeholder(self, monkeypatch):
        captured = _fake_anthropic(monkeypatch, "ok")
        tailor_resume("r", _job(description=None), "m", "k")
        assert "no description available" in captured["messages"][0]["content"]

    def test_long_description_truncated(self, monkeypatch):
        captured = _fake_anthropic(monkeypatch, "ok")
        tailor_resume("r", _job(description="x" * 20000), "m", "k")
        assert len(captured["messages"][0]["content"]) < 15000

    def test_refusal_raises(self, monkeypatch):
        _fake_anthropic(monkeypatch, "", stop_reason="refusal")
        with pytest.raises(TailorError, match="refusal"):
            tailor_resume("r", _job(), "m", "k")

    def test_empty_response_raises(self, monkeypatch):
        _fake_anthropic(monkeypatch, "   ")
        with pytest.raises(TailorError, match="empty"):
            tailor_resume("r", _job(), "m", "k")


CONTACT_MD = ("**Eric Schwimmer**  \n"
              "Seattle, Washington, United States  e@x.com  206-555-0100  "
              "[eric-s](https://linkedin.com/in/eric-s)  "
              "[github.com/e](https://github.com/e)\n\n"
              "**SUMMARY**\n\nStuff.")


class TestFormatContact:
    def test_double_space_fields_split_and_joined(self):
        out = _format_contact(CONTACT_MD)
        line = out.splitlines()[1]
        assert line.startswith('<p class="contact">')
        assert line.count('<span class="sep">·</span>') == 4
        assert 'href="mailto:e@x.com"' in line
        assert 'href="https://linkedin.com/in/eric-s">eric-s</a>' in line
        assert "Seattle, Washington, United States" in line

    def test_source_order_preserved(self):
        line = _format_contact(CONTACT_MD).splitlines()[1]
        seattle = line.index("Seattle")
        email = line.index("mailto:")
        phone = line.index("206-555-0100")
        github = line.index("github.com/e")
        assert seattle < email < phone < github

    def test_single_space_fallback_extracts_tokens_in_order(self):
        md_text = ("**Eric**\n"
                   "Seattle, WA e@x.com 206-555-0100 "
                   "[gh](https://github.com/e)\n\n**SUMMARY**")
        line = _format_contact(md_text).splitlines()[1]
        assert line.startswith('<p class="contact">')
        assert line.index("Seattle, WA") < line.index("mailto:") \
            < line.index("206-555-0100") < line.index("github.com/e")

    def test_document_without_email_left_untouched(self):
        md_text = "**Eric**\nJust a subtitle line\n\n**SUMMARY**\n\nStuff."
        assert _format_contact(md_text) == md_text

    def test_unsplittable_email_line_left_untouched(self):
        # a single-field line (just an email) has nothing to reformat
        md_text = "**Eric**\ne@x.com\n\n**SUMMARY**"
        assert _format_contact(md_text) == md_text

    def test_render_integration_styles_contact(self):
        out = render_resume_html(CONTACT_MD,
                                 "<html><body>{{resume}}</body></html>",
                                 Job(id="1", title="t", company="c"))
        assert '<p class="contact">' in out
        assert out.count('<span class="sep">·</span>') == 4


class TestPromoteHeadings:
    MD = ("**Eric Schwimmer**  \n"
          "Seattle  e@x.com\n\n"
          "**SUMMARY**\n\nAn engineer with **deep** expertise.\n\n"
          "**EXPERIENCE**\n\n**Meta**  \n"
          "Production Engineer (2022 - Present)\n\n"
          "**University of Washington, Bachelor's of Science**\n")

    def test_name_becomes_h1(self):
        out = _promote_headings(self.MD)
        assert out.splitlines()[0] == "# Eric Schwimmer"

    def test_allcaps_sections_become_h2(self):
        out = _promote_headings(self.MD)
        assert "## SUMMARY" in out
        assert "## EXPERIENCE" in out

    def test_mixed_case_bolds_become_h3(self):
        out = _promote_headings(self.MD)
        assert "### Meta" in out
        assert "### University of Washington, Bachelor's of Science" in out

    def test_inline_bold_untouched(self):
        out = _promote_headings(self.MD)
        assert "An engineer with **deep** expertise." in out

    def test_existing_headings_untouched(self):
        md_text = "# Eric\n\n## SUMMARY\n\nStuff."
        assert _promote_headings(md_text) == md_text

    def test_render_integration_emits_headings(self):
        out = render_resume_html(self.MD, "<html>{{resume}}</html>",
                                 Job(id="1", title="t", company="c"))
        assert "<h1>Eric Schwimmer</h1>" in out
        assert "<h2>SUMMARY</h2>" in out
        assert "<h3>Meta</h3>" in out
        assert "<strong>deep</strong>" in out  # inline bold still rendered


class TestRenderResumeHtml:
    TEMPLATE = ("<html><title>{{job_title}} @ {{company}}</title>"
                "<body>{{resume}}<footer>{{date}}</footer></body></html>")

    def test_markdown_rendered_and_placeholders_substituted(self):
        out = render_resume_html("# Eric\n\n- SRE at **Meta**", self.TEMPLATE, _job())
        assert "<h1>Eric</h1>" in out
        assert "<strong>Meta</strong>" in out
        assert "Site Reliability Engineer @ Acme" in out
        assert "{{" not in out

    def test_job_fields_html_escaped(self):
        out = render_resume_html("x", self.TEMPLATE, _job(title="SRE <Staff>",
                                                          company="A&B"))
        assert "SRE &lt;Staff&gt; @ A&amp;B" in out

    def test_missing_resume_placeholder_raises(self):
        with pytest.raises(TailorError, match=r"\{\{resume\}\}"):
            render_resume_html("x", "<html>no placeholder</html>", _job())


class TestResolveTemplate:
    def test_configured_file_wins(self, tmp_path):
        (tmp_path / "custom.html").write_text("CUSTOM {{resume}}")
        cfg = TailorConfig(template_path="custom.html")
        assert resolve_template(cfg, tmp_path) == "CUSTOM {{resume}}"

    def test_absolute_path_used_as_is(self, tmp_path):
        f = tmp_path / "abs.html"
        f.write_text("ABS {{resume}}")
        cfg = TailorConfig(template_path=str(f))
        assert resolve_template(cfg, Path("/nonexistent")) == "ABS {{resume}}"

    def test_missing_file_falls_back_to_packaged_default(self, tmp_path):
        cfg = TailorConfig(template_path="does-not-exist.html")
        text = resolve_template(cfg, tmp_path)
        assert "{{resume}}" in text  # the packaged default is usable

    def test_packaged_default_renders(self):
        text = tailor._DEFAULT_TEMPLATE.read_text()
        out = render_resume_html("# Eric", text, _job())
        assert "<h1>Eric</h1>" in out
        assert "window.print()" in out
