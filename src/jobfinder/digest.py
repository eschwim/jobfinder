"""Daily digest: grade the day's matches against the resume via the Claude API
and email them grouped into strong/weak/no match."""

from __future__ import annotations

import html
import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import anthropic

from .config import AppConfig
from .filters import Job, yearly_salary_range
from .notify import EmailChannel, NotifyError, location_str, salary_str
from .store import Store

log = logging.getLogger("jobfinder.digest")

CATEGORIES = ("strong", "weak", "none")
CATEGORY_TITLES = {"strong": "Strong match", "weak": "Weak match", "none": "No match"}
# Job descriptions are occasionally enormous; the skills section is never
# this deep into one.
MAX_DESC_CHARS = 8000

_SYSTEM = """You are screening job postings for a candidate. Compare each \
posting's required and desired skills, seniority, and domain against the \
candidate's resume, and grade how well the candidate's demonstrated experience \
matches what the posting asks for:

- "strong": the candidate clearly meets the core requirements; their \
experience is directly relevant and they could credibly apply today.
- "weak": meaningful overlap in some required skills, but notable gaps in \
others (missing core technologies, domain, or seniority mismatch).
- "none": the posting's requirements mostly do not match the candidate's \
experience.

Judge on substance, not keywords: equivalent technologies count (e.g. any \
config-management or any major cloud), and skills implied by senior \
infrastructure roles may be credited where reasonable.

Additionally, estimate each job's total annual compensation (ETC) in USD: \
base salary plus expected annual bonus, annualized equity value (RSUs or \
options), and any significant other benefits (401k match, unusually valuable \
perks). Anchor on figures stated in the posting, then fill the gaps from \
public knowledge of the employer (size, stage, known compensation practices \
— e.g. big-tech RSU norms vs startup option grants), the role's seniority, \
and location-adjusted market rates. Report etc_min_usd/etc_max_usd as a \
realistic yearly range — the range should widen with uncertainty, not narrow. \
Set etc_confidence 0-100 for how well the posting plus your knowledge of \
this specific employer support the estimate (80+: posting states total-comp \
components or the employer's comp bands are widely known; 40-79: partial \
figures or a well-understood employer; below 40: mostly generic market \
inference). In etc_note, name the main components and assumptions in one \
short clause. Grade every job you are given, by its id."""

_SCHEMA = {
    "type": "object",
    "properties": {
        "assessments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "category": {"type": "string", "enum": list(CATEGORIES)},
                    "reason": {
                        "type": "string",
                        "description": "One sentence: the decisive skill overlaps or gaps",
                    },
                    "etc_min_usd": {
                        "type": "integer",
                        "description": "Estimated total annual compensation, low end, USD/yr",
                    },
                    "etc_max_usd": {
                        "type": "integer",
                        "description": "Estimated total annual compensation, high end, USD/yr",
                    },
                    "etc_confidence": {
                        "type": "integer",
                        "description": "0-100: how well the posting + employer knowledge support the estimate",
                    },
                    "etc_note": {
                        "type": "string",
                        "description": "One short clause: main components/assumptions behind the estimate",
                    },
                },
                "required": ["id", "category", "reason", "etc_min_usd",
                             "etc_max_usd", "etc_confidence", "etc_note"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["assessments"],
    "additionalProperties": False,
}


class DigestError(Exception):
    pass


@dataclass
class DigestResult:
    matches: int = 0
    strong: int = 0
    weak: int = 0
    none: int = 0


@dataclass
class Assessment:
    """Claude's per-job grade plus its estimated-total-compensation guess."""
    category: str = "none"
    reason: str = ""
    etc_min: int | None = None
    etc_max: int | None = None
    etc_confidence: int | None = None
    etc_note: str = ""


def _row_job(row: sqlite3.Row) -> Job:
    return Job(id=row["id"], title=row["title"] or "", company=row["company"] or "",
               site=row["site"] or "", url=row["url"] or "",
               location=row["location"], is_remote=bool(row["is_remote"]),
               min_amount=row["min_amount"], max_amount=row["max_amount"],
               interval=row["salary_interval"], currency=row["currency"],
               salary_source=row["salary_source"], date_posted=row["date_posted"],
               description=row["description"])


def _jobs_block(jobs: list[Job]) -> str:
    parts = []
    for job in jobs:
        desc = (job.description or "(no description available — grade on the title)")
        parts.append(f"<job id={job.id!r}>\n"
                     f"Title: {job.title}\n"
                     f"Company: {job.company}\n"
                     f"Location: {location_str(job)}\n"
                     f"Description:\n{desc[:MAX_DESC_CHARS]}\n"
                     f"</job>")
    return "\n\n".join(parts)


def _to_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def evaluate_matches(resume_text: str, jobs: list[Job], model: str,
                     api_key: str) -> dict[str, Assessment]:
    """Grade jobs against the resume; returns {job_id: Assessment}."""
    client = anthropic.Anthropic(api_key=api_key)
    prompt = (f"<resume>\n{resume_text}\n</resume>\n\n"
              f"Grade the following {len(jobs)} job posting(s) against the resume:"
              f"\n\n{_jobs_block(jobs)}")
    try:
        with client.messages.stream(
            model=model,
            max_tokens=16000,
            thinking={"type": "adaptive"},
            output_config={"format": {"type": "json_schema", "schema": _SCHEMA}},
            system=_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        ) as stream:
            message = stream.get_final_message()
    except anthropic.APIError as exc:
        raise DigestError(f"Claude API request failed: {exc}") from exc

    if message.stop_reason == "refusal":
        raise DigestError("Claude declined to grade the postings (refusal)")
    if message.stop_reason == "max_tokens":
        raise DigestError("Claude response was truncated (max_tokens)")
    text = next((b.text for b in message.content if b.type == "text"), "")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise DigestError(f"could not parse Claude's response as JSON: {exc}") from exc

    graded = {}
    for entry in data.get("assessments", []):
        if entry.get("category") not in CATEGORIES:
            continue
        confidence = _to_int(entry.get("etc_confidence"))
        if confidence is not None:
            confidence = max(0, min(100, confidence))
        graded[str(entry.get("id"))] = Assessment(
            category=entry["category"],
            reason=entry.get("reason", ""),
            etc_min=_to_int(entry.get("etc_min_usd")),
            etc_max=_to_int(entry.get("etc_max_usd")),
            etc_confidence=confidence,
            etc_note=str(entry.get("etc_note") or ""),
        )
    return graded


def etc_str(assessment: Assessment) -> str:
    """'ETC 250,000–350,000 USD/yr (confidence 65%)' or 'ETC not estimated'."""
    lo, hi = assessment.etc_min, assessment.etc_max
    if lo is None and hi is None:
        return "ETC not estimated"
    if lo is not None and hi is not None and lo != hi:
        amount = f"{lo:,.0f}–{hi:,.0f}"
    else:
        amount = f"{(lo if lo is not None else hi):,.0f}"
    text = f"ETC {amount} USD/yr"
    if assessment.etc_confidence is not None:
        text += f" (confidence {assessment.etc_confidence}%)"
    return text


def _sorted_by_max_salary(jobs: list[Job]) -> list[Job]:
    """Max yearly salary descending; jobs with no salary data last."""
    def key(job: Job):
        lo, hi = yearly_salary_range(job)
        amount = hi if hi is not None else lo
        return (amount is None, -(amount or 0))
    return sorted(jobs, key=key)


def digest_bodies(groups: dict[str, list[Job]],
                  graded: dict[str, Assessment]) -> tuple[str, str]:
    """Plain-text and HTML digest bodies, one section per category."""
    text_parts, html_parts = [], []
    for category in CATEGORIES:
        jobs = groups[category]
        title = f"{CATEGORY_TITLES[category]} ({len(jobs)})"
        text_parts.append(f"{title}\n{'=' * len(title)}")
        html_parts.append(f"<h2>{html.escape(title)}</h2>")
        if not jobs:
            text_parts.append("(none)\n")
            html_parts.append("<p><em>(none)</em></p>")
            continue
        html_parts.append("<ul>")
        for job in jobs:
            unlisted = job.min_amount is None and job.max_amount is None
            summary = f"{location_str(job)} — {salary_str(job, unlisted)} — via {job.site}"
            assessment = graded.get(job.id, Assessment())
            etc_line = etc_str(assessment)
            if assessment.etc_note:
                etc_line += f" — {assessment.etc_note}"
            text_parts.append(f"- {job.title} @ {job.company} — {summary}\n"
                              f"  {assessment.reason}\n  {etc_line}\n  {job.url}\n")
            html_parts.append(
                f'<li><a href="{html.escape(job.url or "")}">'
                f"{html.escape(job.title)} @ {html.escape(job.company)}</a>"
                f" — {html.escape(summary)}<br>"
                f"<em>{html.escape(assessment.reason)}</em><br>"
                f"{html.escape(etc_line)}</li>")
        html_parts.append("</ul>")
    text = "\n".join(text_parts)
    body = "<html><body>" + "\n".join(html_parts) + "</body></html>"
    return text, body


def _email_channel(cfg: AppConfig) -> EmailChannel:
    return EmailChannel(cfg.smtp_host, cfg.smtp_port, cfg.smtp_user,
                        cfg.smtp_password, cfg.email_from, cfg.email_to)


def send_digest(cfg: AppConfig, store: Store, config_dir: Path) -> DigestResult:
    """Run one digest cycle: grade the past 24h of matches, email the summary.

    Raises DigestError when a precondition is missing (API key, resume, SMTP)
    or the grading call fails; successful digests are recorded in the store."""
    resume_path = Path(cfg.digest.resume_path)
    if not resume_path.is_absolute():
        resume_path = config_dir / resume_path
    if not resume_path.is_file():
        raise DigestError(f"resume not found: {resume_path}")
    if not cfg.anthropic_api_key:
        raise DigestError("ANTHROPIC_API_KEY is not set in the environment")
    try:
        channel = _email_channel(cfg)
    except NotifyError as exc:
        raise DigestError(str(exc)) from exc

    since = datetime.now(timezone.utc) - timedelta(hours=24)
    jobs = [_row_job(r) for r in store.recent_matches(since=since)]

    if not jobs:
        try:
            channel.send("jobfinder daily digest: no new matches",
                         "No new matching jobs in the past 24 hours.")
        except NotifyError as exc:
            raise DigestError(str(exc)) from exc
        store.record_digest(0, 0, 0, 0)
        return DigestResult()

    graded = evaluate_matches(resume_path.read_text(), jobs,
                              cfg.digest.model, cfg.anthropic_api_key)
    groups: dict[str, list[Job]] = {c: [] for c in CATEGORIES}
    for job in jobs:
        category = graded.get(job.id, Assessment()).category
        groups[category].append(job)
    for category in CATEGORIES:
        groups[category] = _sorted_by_max_salary(groups[category])

    result = DigestResult(matches=len(jobs), strong=len(groups["strong"]),
                          weak=len(groups["weak"]), none=len(groups["none"]))
    subject = (f"jobfinder daily digest: {result.matches} job(s) — "
               f"{result.strong} strong / {result.weak} weak / {result.none} no match")
    text, html_body = digest_bodies(groups, graded)
    try:
        channel.send(subject, text, html_body)
    except NotifyError as exc:
        raise DigestError(str(exc)) from exc
    store.record_digest(result.matches, result.strong, result.weak, result.none)
    log.info("digest sent: %s", subject)
    return result
