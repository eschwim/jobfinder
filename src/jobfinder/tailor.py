"""Tailor the resume to a specific job posting via the Claude API and render
it through a user-editable HTML template for print-to-PDF."""

from __future__ import annotations

import html as html_mod
import logging
import re
from datetime import date
from pathlib import Path

import anthropic
import markdown as md

from .config import TailorConfig
from .filters import Job

log = logging.getLogger("jobfinder.tailor")

MAX_DESC_CHARS = 8000
_DEFAULT_TEMPLATE = Path(__file__).parent / "resources" / "resume-template.html"

_SYSTEM = """You tailor an existing resume to a specific job posting.

Hard rules — the tailored resume must stay truthful:
- Every claim must be traceable to the source resume. Never invent employers, \
titles, dates, technologies, metrics, or accomplishments.
- Allowed: reorder bullets within a position, reword using the posting's \
vocabulary where the meaning is identical, emphasize the most relevant \
experience, and trim or omit the least relevant content to keep the resume \
to roughly one page.
- Employment positions must stay in exactly the source resume's order (most \
recent first) with their titles and dates unchanged — never reorder, merge, \
or re-date positions, even to surface more relevant experience.
- The summary may be rewritten to speak to this role, but only from facts \
present in the resume.
- Keep contact information verbatim.

Output ONLY the tailored resume as Markdown, keeping the same heading \
structure as the source resume — no preamble, no explanation, no code fences."""


class TailorError(Exception):
    pass


def _strip_resume_images(text: str) -> str:
    """Drop embedded data-URI icon definitions and their uses — they waste
    tokens and would break rendering if echoed back."""
    text = re.sub(r"^\[image\d+\]:\s*<data:[^>]*>\s*$", "", text,
                  flags=re.MULTILINE)
    text = re.sub(r"!\[[^\]]*\]\[image\d+\]", "", text)
    return text


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n", "", text)
        text = re.sub(r"\n```\s*$", "", text)
    return text.strip()


def tailor_resume(resume_text: str, job: Job, model: str, api_key: str) -> str:
    """One Claude call: the resume rewritten for this posting, as Markdown."""
    desc = job.description or "(no description available — tailor to the title and company)"
    prompt = (f"<resume>\n{_strip_resume_images(resume_text)}\n</resume>\n\n"
              f"Tailor the resume above for this job posting:\n\n"
              f"<job>\n"
              f"Title: {job.title}\n"
              f"Company: {job.company}\n"
              f"Location: {job.location or 'n/a'}\n"
              f"Description:\n{desc[:MAX_DESC_CHARS]}\n"
              f"</job>")
    client = anthropic.Anthropic(api_key=api_key)
    try:
        with client.messages.stream(
            model=model,
            max_tokens=16000,
            thinking={"type": "adaptive"},
            system=_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        ) as stream:
            message = stream.get_final_message()
    except anthropic.APIError as exc:
        raise TailorError(f"Claude API request failed: {exc}") from exc

    if message.stop_reason == "refusal":
        raise TailorError("Claude declined to tailor the resume (refusal)")
    if message.stop_reason == "max_tokens":
        raise TailorError("Claude response was truncated (max_tokens)")
    text = _strip_fences(
        next((b.text for b in message.content if b.type == "text"), ""))
    if not text:
        raise TailorError("Claude returned an empty resume")
    return text


def resolve_template(tailor_cfg: TailorConfig, config_dir: Path) -> str:
    """The user's template if it exists, else the packaged default."""
    path = Path(tailor_cfg.template_path)
    if not path.is_absolute():
        path = config_dir / path
    if path.is_file():
        return path.read_text()
    return _DEFAULT_TEMPLATE.read_text()


_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_PHONE_RE = re.compile(r"\+?\d[\d().-]{6,}\d")


def _contact_field_html(field: str) -> str:
    """One contact field as HTML: markdown links become anchors, a bare email
    becomes a mailto link, anything else is escaped text."""
    link = _MD_LINK_RE.fullmatch(field)
    if link:
        return (f'<a href="{html_mod.escape(link.group(2))}">'
                f"{html_mod.escape(link.group(1))}</a>")
    if _EMAIL_RE.fullmatch(field):
        return f'<a href="mailto:{html_mod.escape(field)}">{html_mod.escape(field)}</a>'
    return html_mod.escape(field)


def _format_contact(markdown_text: str) -> str:
    """Rewrite the contact line (first early line containing an email) as a
    styled HTML block, so its fields don't render as one space-mashed string.

    The source resume delimits contact fields with runs of 2+ spaces (left
    behind where icons were stripped); fall back to token extraction if a
    generation normalized the whitespace."""
    lines = markdown_text.splitlines()
    for i, line in enumerate(lines[:8]):
        if line.lstrip().startswith("#") or not _EMAIL_RE.search(line):
            continue
        text = line.strip()
        fields = [f.strip() for f in re.split(r"\s{2,}", text) if f.strip()]
        if len(fields) < 2:  # whitespace got normalized: extract known tokens
            spans = sorted(
                (m.span() for pattern in (_MD_LINK_RE, _EMAIL_RE, _PHONE_RE)
                 for m in pattern.finditer(text)))
            spans = [s for i, s in enumerate(spans)  # drop overlaps (email in link)
                     if not any(s[0] >= o[0] and s[1] <= o[1]
                                for o in spans[:i] + spans[i + 1:])]
            fields, cursor = [], 0
            for start, end in spans:
                lead = text[cursor:start].strip(" \t·|,-")
                if lead:
                    fields.append(lead)
                fields.append(text[start:end])
                cursor = end
            tail = text[cursor:].strip(" \t·|,-")
            if tail:
                fields.append(tail)
        if len(fields) < 2:
            return markdown_text  # nothing splittable; leave the line alone
        sep = ' <span class="sep">·</span> '
        lines[i] = ('<p class="contact">'
                    + sep.join(_contact_field_html(f) for f in fields)
                    + "</p>")
        return "\n".join(lines)
    return markdown_text


_BOLD_LINE_RE = re.compile(r"^\*\*(.+?)\*\*\s*$")


def _promote_headings(markdown_text: str) -> str:
    """Turn full-line bold "headers" into real Markdown headings.

    The source resume marks structure with bold paragraphs (**SUMMARY**,
    **Meta**) rather than # headings, so everything renders as cramped <p>
    tags and the template's heading styles never apply. Promote: the first
    bold line (the name) to h1, ALL-CAPS bold lines (section headers) to h2,
    and remaining full-line bolds (employers/degrees) to h3. Inline bold and
    real headings are untouched."""
    lines = markdown_text.splitlines()
    name_seen = False
    for i, line in enumerate(lines):
        m = _BOLD_LINE_RE.match(line.strip())
        if not m:
            continue
        text = m.group(1).strip()
        if not name_seen:
            lines[i] = f"# {text}"
            name_seen = True
        elif text.upper() == text:
            lines[i] = f"## {text}"
        else:
            lines[i] = f"### {text}"
    return "\n".join(lines)


def render_resume_html(markdown_text: str, template_text: str, job: Job) -> str:
    """Render tailored markdown into the template via plain placeholder
    substitution (the template is user-edited — no Jinja, no compile errors)."""
    if "{{resume}}" not in template_text:
        raise TailorError("template is missing the required {{resume}} placeholder")
    prepared = _promote_headings(_format_contact(markdown_text))
    resume_html = md.markdown(prepared, extensions=["extra"])
    out = template_text.replace("{{resume}}", resume_html)
    out = out.replace("{{job_title}}", html_mod.escape(job.title or ""))
    out = out.replace("{{company}}", html_mod.escape(job.company or ""))
    out = out.replace("{{date}}", date.today().isoformat())
    return out
