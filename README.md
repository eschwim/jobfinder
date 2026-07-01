# jobfinder

Polls job boards (Indeed, LinkedIn, Google Jobs, ZipRecruiter, Glassdoor) via
[JobSpy](https://github.com/speedyapply/JobSpy) and notifies you — by
[Pushover](https://pushover.net) push and/or email — when a new posting matches
your criteria: title regex, location, employer blocklist, and salary range.

Pick channels in `config.yaml` (`notify.channels: [pushover, email]`) and put
the matching credentials in `.env` (see `.env.example`; for Gmail use an
[app password](https://myaccount.google.com/apppasswords)). Pushover sends one
push per job, collapsing into a digest above `digest_threshold`; email always
sends one message per run listing every match with links.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
cp .env.example .env               # fill in Pushover and/or SMTP credentials
cp config.example.yaml config.yaml # set your searches and filters
chmod 600 .env
```

## Run once

```bash
.venv/bin/python -m jobfinder --test-notify   # send a test message through each channel
.venv/bin/python -m jobfinder --dry-run       # prints alerts instead of sending them
.venv/bin/python -m jobfinder                 # real notifications
```

State lives in `jobfinder.db` (SQLite): every job already alerted on, plus
per-site health counters. Each posting alerts at most once; delete the file to
reset.

## Run on a schedule (systemd user timer)

```bash
mkdir -p ~/.config/systemd/user
cp systemd/jobfinder.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now jobfinder.timer
loginctl enable-linger $USER   # keep the timer running when logged out
```

Check on it:

```bash
systemctl --user list-timers jobfinder.timer
journalctl --user -u jobfinder.service -e
```

## Notes

- **Scraping caveats**: JobSpy uses reverse-engineered endpoints. LinkedIn
  rate-limits per-IP, so poll gently (the default is every 2 h with modest
  `results_wanted`). If a board changes its site, that scraper breaks until a
  `python-jobspy` update ships — the service sends a low-priority health alert
  after `empty_runs_before_alert` consecutive empty runs so you know to
  `pip install -U python-jobspy`.
- **Unlisted salaries**: many postings carry no salary data. With
  `keep_unlisted: true` they still alert (flagged); set it to `false` to only
  see postings whose stated range overlaps yours.
- **Tests**: `.venv/bin/pytest`

## License

MIT
