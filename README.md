# SPY ThetaData report generator and publisher for Raspberry Pi

This repository contains a standard-library-only Python pipeline that calls a locally
running ThetaData v3 terminal, gathers SPY stock data, creates a transparent
rule-based market report, validates it, writes `reports/YYYYMMDD.json`, updates
`latest.json`, commits the two paths on `main`, and pushes them to this repository.

Package version: **1.2.0**.

The publisher is intentionally fail-closed. Validation finishes before it accesses
Git. A bad report cannot replace `latest.json`.

## What this package protects

- Only reports for `SPY` with explicit `trading_date`, `session`, `generated_at`,
  `market_timezone`, and `status` fields are accepted.
- `status: test`, test flags, dry-run/mock/sample/synthetic markers, and phrases such
  as `No live market data` are rejected at any nesting level.
- The scheduled session must match the report's `session`.
- `generated_at`, interpreted in `America/New_York`, must resolve to the labeled
  trading date and must fall in the correct premarket or postmarket window.
- Weekends, standard NYSE holidays, and recurring 13:00 ET closes are handled. A
  small override file covers one-off exchange changes announced after deployment.
- Future and stale reports are rejected. The default maximum age is 120 minutes.
- A postmarket report can advance a same-day premarket report. A premarket report
  cannot replace a postmarket report, and an older report cannot replace a newer one.
- Canonically identical content is a successful no-op, so retries do not create
  duplicate commits.
- The checkout must be clean and on `main`. Only the report path and `latest.json`
  are staged. A per-repository lock prevents overlapping publisher processes.
- The push remote must exactly identify `github.com/treethatgoesmoo/spy-market-reports`.
  Credential-bearing HTTPS URLs are rejected.
- Push races are fetched and rebased up to two times. A failure rolls the dedicated,
  initially clean checkout back instead of leaving a half-published report.

Because the requested archive path has no session suffix, the postmarket publication
replaces that day's premarket file. The premarket version remains available in Git
history; `latest.json` contains the newest session.

## Included ThetaData analysis generator

`thetadata_report_generator.py` is the default generator; no separate analysis script
is required. It connects to Theta Terminal v3 at
`http://127.0.0.1:25503/v3` and requests:

- 5-minute SPY bars from `/stock/history/ohlc`;
- up to 30 calendar days of regular-session history.

It calculates moving averages, EMA trend, RSI, ATR, realized volatility, price and
volume momentum. Every contribution to the final bullish/neutral/bearish score is
stored in `analysis.signals`; the result is an explainable rule-based research
summary, not a trained model or investment advice.

Theta Terminal must already be running on the same Pi, and the ThetaData account must
have access to stock OHLC history. The generator refuses non-loopback ThetaData URLs
by default and rejects malformed, incomplete, or stale SPY responses. It does not
request index data; ThetaData STANDARD stock access with FREE index access is the
intended subscription combination.

## Required report contract

The included generator writes one JSON object with at least these fields:

```json
{
  "symbol": "SPY",
  "status": "live",
  "market_timezone": "America/New_York",
  "session": "premarket",
  "trading_date": "2026-11-06",
  "generated_at": "2026-11-06T13:45:00Z"
}
```

`session` is `premarket` or `postmarket`. `generated_at` must have `Z` or an explicit
UTC offset. Additional analysis fields are preserved. The scheduler supplies these
environment variables to the generator as well as the configured command arguments:

```text
MARKET_SESSION=premarket
TRADING_DATE=2026-11-06
REPORT_OUTPUT=/var/lib/spy-report-publisher/incoming/...
```

If you substitute a custom generator, it must add this metadata at report creation time,
using the session and trading date supplied by the runner rather than deriving a date
from UTC:

```python
from datetime import datetime, timezone

report.update({
    "symbol": "SPY",
    "status": "live",
    "market_timezone": "America/New_York",
    "session": session,
    "trading_date": trading_date,  # YYYY-MM-DD supplied by the runner
    "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
})
```

The generator command must accept or otherwise honor an output path. The runner uses
an argument list with `shell=False`; pipes, redirections, and shell expansion are not
performed.

## Files

- `spy_report_publisher.py` — validation and transactional Git publication
- `thetadata_report_generator.py` — local ThetaData collection and SPY analysis
- `market_calendar.py` — dependency-free US equities session calendar
- `schedule_runner.py` — holiday/early-close gate and generator integration
- `config/` — publisher configuration and emergency calendar overrides
- `systemd/` — one service template and three market-time timers
- `fixtures/` and `tests/` — accepted/rejected examples plus unit/integration tests

## Exact Raspberry Pi installation commands

These commands target current Raspberry Pi OS with Python 3.9 or newer and systemd.
Run them from the extracted `spy-report-publisher` package directory. They do **not**
claim or assume that anything is already installed on the Pi.

### Easier setup with Make

On the Raspberry Pi, clone this repository and run the guarded two-stage installer:

```bash
sudo apt-get update
sudo apt-get install -y git make
git clone https://github.com/treethatgoesmoo/spy-market-reports.git
cd spy-market-reports
make bootstrap
```

`bootstrap` runs the tests, installs dependencies and component files, creates or
preserves the deploy key, pins GitHub's published Ed25519 host key, installs the
configuration if it does not already exist, and installs—but does not enable—the
systemd units. It prints the public deploy key and the remaining steps.

Add that public key under the repository's **Settings → Deploy keys**, enable
**Allow write access**, ensure Theta Terminal v3 is running locally, and finish:

```bash
make finish
```

The finishing target verifies the deploy key, clones or validates the dedicated
publication checkout, validates the configuration, makes a real read-only ThetaData
health check for SPY stock history, checks the systemd units, and only then enables
the timers. To substitute a different generator, use `make configure-generator` or
`make configure` before `make finish`.

Useful operations:

```bash
make help            # list targets
make test            # run all tests without changing the Pi
make check-thetadata # verify the local ThetaData terminal and entitlements
make check-schedule  # inspect both market-time gates without publishing
make status          # inspect timers and services
make logs            # show today's service logs
make update          # update code/units without replacing config or credentials
make disable-timers  # stop scheduling without deleting reports or configuration
```

Override `RUN_USER`, `RUN_GROUP`, `PREFIX`, `CONFIG_DIR`, or `REPO_DIR` on the Make
command line when the defaults are not appropriate. Run `make` as the intended login
user, not as `root`; individual privileged installation steps invoke `sudo`.

### Manual installation

First install the operating-system prerequisites and the component:

```bash
set -eu
SPY_RUN_USER="$(id -un)"
SPY_RUN_GROUP="$(id -gn)"

sudo apt-get update
sudo apt-get install -y git openssh-client python3 unzip

sudo install -d -m 0755 /opt/spy-report-publisher
sudo install -m 0755 market_calendar.py spy_report_publisher.py schedule_runner.py thetadata_report_generator.py /opt/spy-report-publisher/

sudo install -d -m 0750 -o "$SPY_RUN_USER" -g "$SPY_RUN_GROUP" /etc/spy-report-publisher
sudo install -d -m 0750 -o "$SPY_RUN_USER" -g "$SPY_RUN_GROUP" /var/lib/spy-report-publisher/incoming
sudo install -d -m 0750 -o "$SPY_RUN_USER" -g "$SPY_RUN_GROUP" /var/log/spy-report-publisher
sudo install -m 0640 -o "$SPY_RUN_USER" -g "$SPY_RUN_GROUP" config/spy-report-publisher.ini.example /etc/spy-report-publisher/publisher.ini
sudo install -m 0640 -o "$SPY_RUN_USER" -g "$SPY_RUN_GROUP" config/calendar-overrides.json /etc/spy-report-publisher/calendar-overrides.json
```

Create a dedicated repository-scoped SSH deploy key. Do not put a personal access
token in a Git remote or configuration file:

```bash
set -eu
SPY_RUN_USER="$(id -un)"
SPY_RUN_GROUP="$(id -gn)"

sudo ssh-keygen -q -t ed25519 -N '' \
  -C 'spy-report-publisher Raspberry Pi deploy key' \
  -f /etc/spy-report-publisher/deploy_key
sudo chown "$SPY_RUN_USER:$SPY_RUN_GROUP" \
  /etc/spy-report-publisher/deploy_key \
  /etc/spy-report-publisher/deploy_key.pub
sudo chmod 0600 /etc/spy-report-publisher/deploy_key
sudo chmod 0644 /etc/spy-report-publisher/deploy_key.pub

printf '%s\n' \
  'github.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl' \
  | sudo tee /etc/spy-report-publisher/known_hosts >/dev/null
sudo chown root:root /etc/spy-report-publisher/known_hosts
sudo chmod 0644 /etc/spy-report-publisher/known_hosts

sudo cat /etc/spy-report-publisher/deploy_key.pub
```

Copy the final command's single public-key line. In GitHub, open **Settings → Deploy
keys → Add deploy key** for `treethatgoesmoo/spy-market-reports`, paste the key, and
enable **Allow write access**. The private key stays on the Pi. The pinned `known_hosts`
entry above is GitHub's published Ed25519 host key; the corresponding fingerprint is
`SHA256:+DiY3wvvV6TuJJhbpZisF/zLDA0zPMSvHdkr4UvCOqU`.

After adding the deploy key, test it and clone a dedicated clean publication checkout:

```bash
set -eu
SPY_RUN_USER="$(id -un)"
SPY_RUN_GROUP="$(id -gn)"
SPY_SSH_COMMAND='/usr/bin/ssh -i /etc/spy-report-publisher/deploy_key -o IdentitiesOnly=yes -o UserKnownHostsFile=/etc/spy-report-publisher/known_hosts -o StrictHostKeyChecking=yes -o BatchMode=yes'

set +e
sudo -u "$SPY_RUN_USER" /usr/bin/ssh \
  -i /etc/spy-report-publisher/deploy_key \
  -o IdentitiesOnly=yes \
  -o UserKnownHostsFile=/etc/spy-report-publisher/known_hosts \
  -o StrictHostKeyChecking=yes \
  -o BatchMode=yes \
  -T git@github.com
SSH_STATUS=$?
set -e
test "$SSH_STATUS" -eq 1

sudo install -d -m 0750 -o "$SPY_RUN_USER" -g "$SPY_RUN_GROUP" /srv/spy-market-reports
sudo -u "$SPY_RUN_USER" env GIT_SSH_COMMAND="$SPY_SSH_COMMAND" \
  git clone git@github.com:treethatgoesmoo/spy-market-reports.git /srv/spy-market-reports
sudo -u "$SPY_RUN_USER" git -C /srv/spy-market-reports config user.name 'SPY Report Publisher'
sudo -u "$SPY_RUN_USER" git -C /srv/spy-market-reports config user.email 'spy-report-publisher@localhost'
sudo -u "$SPY_RUN_USER" git -C /srv/spy-market-reports status --short --branch
```

`ssh -T` returning status 1 after printing the authenticated GitHub identity is
expected because GitHub does not provide interactive shell access. If the destination
checkout already exists, do not reclone it blindly; verify it is the intended clean
checkout on `main`, then set `repo_dir` to that absolute path.

The example configuration already selects the included ThetaData generator under
`/opt/spy-report-publisher`; no generator edit is required. Confirm local ThetaData
access before enabling timers:

```bash
/usr/bin/python3 /opt/spy-report-publisher/thetadata_report_generator.py --health-check
```

To use a different generator instead, edit the configuration:

```bash
sudoedit /etc/spy-report-publisher/publisher.ini
```

Install the service with the current login as the execution account, install the
timers, and verify systemd parsed them:

```bash
set -eu
SPY_RUN_USER="$(id -un)"
SPY_RUN_GROUP="$(id -gn)"

sed \
  -e "s/__RUN_USER__/$SPY_RUN_USER/g" \
  -e "s/__RUN_GROUP__/$SPY_RUN_GROUP/g" \
  systemd/spy-report-publisher@.service \
  | sudo tee /etc/systemd/system/spy-report-publisher@.service >/dev/null
sudo install -m 0644 systemd/spy-report-publisher-premarket.timer /etc/systemd/system/
sudo install -m 0644 systemd/spy-report-publisher-postmarket-early.timer /etc/systemd/system/
sudo install -m 0644 systemd/spy-report-publisher-postmarket-regular.timer /etc/systemd/system/

sudo systemd-analyze verify \
  /etc/systemd/system/spy-report-publisher@.service \
  /etc/systemd/system/spy-report-publisher-premarket.timer \
  /etc/systemd/system/spy-report-publisher-postmarket-early.timer \
  /etc/systemd/system/spy-report-publisher-postmarket-regular.timer
sudo systemctl daemon-reload
sudo systemctl enable --now \
  spy-report-publisher-premarket.timer \
  spy-report-publisher-postmarket-early.timer \
  spy-report-publisher-postmarket-regular.timer
systemctl list-timers 'spy-report-publisher*'
```

The premarket timer fires at 08:45 ET. The two postmarket timers fire at 13:30 and
16:30 ET; the calendar gate allows only the one that is 30 minutes after that day's
actual scheduled close. On holidays, both sessions skip successfully. `OnCalendar`
uses `America/New_York`, so daylight-saving changes do not require UTC cron edits.

If the generator needs to write anywhere besides the report output directory, add
that exact directory to `ReadWritePaths=` in the service. Keep the list narrow.

## Tests and first-run checks

Run the bundled tests before installation or directly from the extracted directory:

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile market_calendar.py spy_report_publisher.py schedule_runner.py thetadata_report_generator.py
```

The test suite uses temporary local Git repositories; it never contacts or modifies
GitHub. It covers regular and early-close sessions, holidays, test-data rejection,
the reported 05:17 UTC/date mismatch pattern, duplicate suppression, transactional
commits, and premarket-to-postmarket advancement. On Linux (including Raspberry Pi),
the integration test also pushes to a temporary local bare remote; Windows runs the
same transaction without the local transport because some Git for Windows sandboxed
installations cannot launch the file-transport helper. Generator tests use deterministic
ThetaData-shaped responses and do not contact a live account.

Verify the real local ThetaData terminal and data entitlement:

```bash
/usr/bin/python3 /opt/spy-report-publisher/thetadata_report_generator.py --health-check
```

Check the schedule gate without running the generator:

```bash
/usr/bin/python3 /opt/spy-report-publisher/schedule_runner.py \
  --config /etc/spy-report-publisher/publisher.ini \
  --session premarket \
  --check-schedule
/usr/bin/python3 /opt/spy-report-publisher/schedule_runner.py \
  --config /etc/spy-report-publisher/publisher.ini \
  --session postmarket \
  --check-schedule
```

Validate a newly generated report without touching Git:

```bash
/usr/bin/python3 /opt/spy-report-publisher/spy_report_publisher.py \
  --report /absolute/path/to/new-report.json \
  --session premarket \
  --repo /srv/spy-market-reports \
  --calendar-overrides /etc/spy-report-publisher/calendar-overrides.json \
  --validate-only
```

After the ThetaData health check passes, a manual service run exercises the same
schedule gate as the timer. It will skip outside the due window:

```bash
sudo systemctl start spy-report-publisher@premarket.service
sudo journalctl -u spy-report-publisher@premarket.service -n 100 --no-pager
sudo journalctl -u spy-report-publisher@postmarket.service -n 100 --no-pager
```

Do not force a fake current timestamp into a production report to test a push. Use a
real freshly generated report during its scheduled window, then verify:

```bash
git -C /srv/spy-market-reports status --short --branch
git -C /srv/spy-market-reports log -3 --oneline --decorate
git -C /srv/spy-market-reports show --stat --oneline HEAD
```

## Emergency holiday or early-close changes

The built-in rules cover recurring NYSE holidays and early closes. Exchange schedules
can change. Update `/etc/spy-report-publisher/calendar-overrides.json` when the NYSE
announces an exceptional session:

```json
{
  "closed": ["2026-12-31"],
  "open": [],
  "early_close": {
    "2026-12-30": "13:00"
  }
}
```

Dates in `closed` take precedence. `open` can override a built-in weekend/holiday only
when that unusual opening is confirmed. `early_close` values are Eastern wall times.
Malformed or contradictory overrides stop the run.

## What is verified remotely vs. what remains local

Verified from the public GitHub repository before the first live deployment:

- the default branch is `main`;
- the initial report-only state was commit
  `0dfd69eda1e6c6e34bd8e23620b209aaf9eafc2c`;
- that commit is timestamped `2026-11-06T05:09:24Z`;
- `latest.json` has `status: "test"` and says it is an integration test with no live
  market data.

Must be tested on the Raspberry Pi because GitHub cannot prove it:

- the locally running Theta Terminal v3 service and the account's SPY stock-data
  entitlement;
- live ThetaData response timing and file ownership for the service account;
- the deploy key's write permission and outbound network/DNS access;
- system clock synchronization and the installed timezone database;
- systemd timer firing, journal/file logging, a real commit, and a successful push;
- any one-off market closure that postdates this package's calendar rules.

The package has not been installed on, or run against, the Raspberry Pi by this work.

## Exit status and operations

- `0` — published, duplicate no-op, validation-only success, or scheduled skip
- `1` — Git, generator, configuration, I/O, authentication, or unexpected failure
- `2` — report validation rejected the data

The service logs to both systemd's journal and the configured rotating file. A failed
run does not update `latest.json` remotely. Inspect failures with:

```bash
sudo journalctl -u 'spy-report-publisher@*' --since today --no-pager
sudo tail -n 200 /var/log/spy-report-publisher/publisher.log
```

The hourly ChatGPT repository monitor is separate and should remain as-is; this
package does not create another monitor.

