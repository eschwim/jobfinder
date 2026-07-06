"""Notification delivery: Pushover and/or email, chosen via notify.channels in config."""

from __future__ import annotations

import html
import logging
import smtplib
from email.message import EmailMessage

import requests

from .config import AppConfig
from .filters import Job, Match, yearly_salary_range

PUSHOVER_API = "https://api.pushover.net/1/messages.json"
# Pushover hard limits: message 1024 chars, title 250.
MAX_MESSAGE = 1024
MAX_TITLE = 250

log = logging.getLogger(__name__)


class NotifyError(Exception):
    pass


def salary_str(job: Job, unlisted: bool) -> str:
    lo, hi = yearly_salary_range(job)
    if unlisted or (lo is None and hi is None):
        return "salary unlisted"
    currency = job.currency or ""
    if lo and hi and lo != hi:
        text = f"{lo:,.0f}–{hi:,.0f} {currency}/yr"
    else:
        text = f"{(lo or hi):,.0f} {currency}/yr"
    if job.salary_source and job.salary_source != "direct_data":
        text += f" ({job.salary_source})"
    return text.strip()


def location_str(job: Job) -> str:
    # Remote postings are often tagged with a hiring-hub city ("New York, NY"),
    # which reads as misleading in an alert — label them plainly as remote.
    if job.is_remote:
        return "Remote"
    return job.location or "location n/a"


def _match_summary(m: Match) -> str:
    job = m.job
    return f"{job.title} @ {job.company} — {location_str(job)} — {salary_str(job, m.salary_unlisted)}"


def email_bodies(matches: list[Match]) -> tuple[str, str]:
    """Plain-text and HTML bodies listing every match with a link to the posting."""
    text_lines, html_lines = [], []
    for m in matches:
        summary = _match_summary(m)
        text_lines.append(f"- {summary}\n  {m.job.url}")
        link = html.escape(m.job.url or "")
        html_lines.append(
            f'<li><a href="{link}">{html.escape(m.job.title)} @ {html.escape(m.job.company)}</a>'
            f" — {html.escape(summary.split(' — ', 1)[1])}</li>"
        )
    text = "\n".join(text_lines)
    body = "<html><body><ul>" + "\n".join(html_lines) + "</ul></body></html>"
    return text, body


class PushoverChannel:
    name = "pushover"

    def __init__(self, token: str | None, user: str | None):
        if not (token and user):
            raise NotifyError("pushover channel enabled but PUSHOVER_TOKEN/PUSHOVER_USER "
                              "are not set in .env")
        self.token = token
        self.user = user

    def _push(self, title: str, message: str, url: str | None = None,
              url_title: str | None = None, priority: int = 0) -> None:
        payload = {
            "token": self.token,
            "user": self.user,
            "title": title[:MAX_TITLE],
            "message": message[:MAX_MESSAGE],
            "priority": priority,
        }
        if url:
            payload["url"] = url
        if url_title:
            payload["url_title"] = url_title
        resp = requests.post(PUSHOVER_API, data=payload, timeout=30)
        if resp.status_code != 200:
            raise NotifyError(f"Pushover returned {resp.status_code}: {resp.text[:200]}")

    def alert_matches(self, matches: list[Match], digest_threshold: int) -> None:
        if len(matches) <= digest_threshold:
            for m in matches:
                job = m.job
                parts = [location_str(job),
                         salary_str(job, m.salary_unlisted),
                         f"via {job.site}"]
                self._push(
                    title=f"{job.title} @ {job.company}",
                    message=" | ".join(parts),
                    url=job.url or None,
                    url_title="View posting",
                )
        else:
            lines = [f"• {_match_summary(m)}" for m in matches]
            self._push(title=f"{len(matches)} new job matches", message="\n".join(lines))

    def test_message(self) -> None:
        self._push(title="jobfinder test", message="Notifications are working.")

    def health_alert(self, site: str, empty_runs: int) -> None:
        self._push(
            title="jobfinder: scraper may be broken",
            message=f"{site} has returned 0 results for {empty_runs} consecutive runs. "
                    "The site may have changed its endpoints — check for a python-jobspy update.",
            priority=-1,
        )


class EmailChannel:
    name = "email"

    def __init__(self, host: str, port: int, username: str | None,
                 password: str | None, sender: str | None, recipient: str | None):
        if not (username and password):
            raise NotifyError("email channel enabled but SMTP_USER/SMTP_PASSWORD "
                              "are not set in .env")
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.sender = sender or username
        self.recipient = recipient or username

    def _send(self, subject: str, text: str, html_body: str | None = None) -> None:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = self.sender
        msg["To"] = self.recipient
        msg.set_content(text)
        if html_body:
            msg.add_alternative(html_body, subtype="html")
        try:
            smtp_cls = smtplib.SMTP_SSL if self.port == 465 else smtplib.SMTP
            with smtp_cls(self.host, self.port, timeout=30) as server:
                if smtp_cls is smtplib.SMTP:
                    server.starttls()
                server.login(self.username, self.password)
                server.send_message(msg)
        except (smtplib.SMTPException, OSError) as exc:
            raise NotifyError(f"SMTP send failed: {exc}") from exc

    def alert_matches(self, matches: list[Match], digest_threshold: int) -> None:
        # Email always goes out as one message per run; the digest threshold is
        # a push-notification concern.
        text, html_body = email_bodies(matches)
        plural = "es" if len(matches) != 1 else ""
        self._send(f"jobfinder: {len(matches)} new job match{plural}", text, html_body)

    def test_message(self) -> None:
        self._send("jobfinder test", "Notifications are working.")

    def health_alert(self, site: str, empty_runs: int) -> None:
        self._send(
            "jobfinder: scraper may be broken",
            f"{site} has returned 0 results for {empty_runs} consecutive runs. "
            "The site may have changed its endpoints — check for a python-jobspy update.",
        )


def build_channels(cfg: AppConfig) -> list:
    channels = []
    for name in cfg.notify.channels:
        if name == "pushover":
            channels.append(PushoverChannel(cfg.pushover_token, cfg.pushover_user))
        elif name == "email":
            channels.append(EmailChannel(cfg.smtp_host, cfg.smtp_port, cfg.smtp_user,
                                         cfg.smtp_password, cfg.email_from, cfg.email_to))
    return channels


class Notifier:
    def __init__(self, channels: list, dry_run: bool = False):
        self.channels = channels
        self.dry_run = dry_run

    def _dispatch(self, method: str, *args) -> None:
        failed = []
        for channel in self.channels:
            try:
                getattr(channel, method)(*args)
            except Exception as exc:
                log.error("%s channel failed: %s", channel.name, exc)
                failed.append(channel.name)
        if failed and len(failed) == len(self.channels):
            raise NotifyError(f"all notification channels failed: {', '.join(failed)}")

    def alert_matches(self, matches: list[Match], digest_threshold: int) -> None:
        if not matches:
            return
        if self.dry_run:
            for m in matches:
                log.info("[dry-run] would notify: %s | %s", _match_summary(m), m.job.url)
            return
        self._dispatch("alert_matches", matches, digest_threshold)

    def health_alert(self, site: str, empty_runs: int) -> None:
        if self.dry_run:
            log.info("[dry-run] would send health alert: %s empty for %d runs", site, empty_runs)
            return
        self._dispatch("health_alert", site, empty_runs)

    def test(self) -> None:
        self._dispatch("test_message")
