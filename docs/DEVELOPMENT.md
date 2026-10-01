# Development

[Back to AppTrail](../README.md)

## Local setup

Use Python 3.11 or newer and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/serpapi/apptrail.git
cd apptrail
uv sync
uv run apptrail --data-dir .apptrail
```

The UI opens in your browser. Use the terminal setup code to create an owner account on first launch. Use `--no-browser` for a terminal-only launch, or `--port 8080` for a fixed port. A development data directory keeps experiments separate from your normal user workspace.

No frontend build is required. HTML, CSS, and JavaScript live in `src/apptrail/static/`. Reload the browser after frontend edits; restart AppTrail after Python edits.

Utility icons use a bundled subset of Lucide 1.48.0 in `static/vendor/lucide-icons.js`. Its ISC and Feather-derived MIT notices are included in `static/vendor/lucide-LICENSE.txt`. The original sidebar navigation icons remain separate.

Charts use the bundled Chart.js 4.4.9 distribution in `static/vendor/chart.umd.min.js`, with its upstream MIT notice in `static/vendor/chart-LICENSE.md`. Application scripts load from the AppTrail origin.

## Project layout

| File | Responsibility |
|---|---|
| `src/apptrail/cli.py` | Console command, free-port binding, browser launch |
| `src/apptrail/api.py` | FastAPI routes and input validation |
| `src/apptrail/auth.py` | Owner setup, password hashing, session validation, CSRF, and login throttling |
| `src/apptrail/backups.py` | Restore validation, schema compatibility, and exclusion of concurrent database users during restore |
| `src/apptrail/storage.py` | Storage measurements, age-based response and image cleanup, and database compaction |
| `src/apptrail/config.py` | Data directory and credentials |
| `src/apptrail/db.py` | SQLAlchemy models and numbered SQLite schema migrations |
| `src/apptrail/engines.py` | Official SerpApi SDK requests and source normalization |
| `src/apptrail/matching.py` | Store identities, AI mentions, and citation evidence |
| `src/apptrail/insights.py` | Confirmed rank alerts, saved-query matrices, and ranking distributions |
| `src/apptrail/listing_history.py` | Opt-in regional listing schedules, normalized snapshots, and archived images |
| `src/apptrail/insights_api.py` | Authenticated insight and listing-history endpoints |
| `src/apptrail/service.py` | Apps, shared queries, history, and usage estimates |
| `src/apptrail/worker.py` | Persistent queue, retries, query scheduling, and manual statistics refreshes |
| `src/apptrail/static/` | Vanilla JavaScript UI, CSS, and HTML |

Search definitions are independent of the apps attached to them. A successful run keeps its source parameters, sanitized responses, normalized result, and observations for each app. Attaching another app to a saved query analyzes existing successful runs without repeating searches.

## Tests

Use Node.js 22 or newer for the JavaScript tests.

```bash
uv run pytest
node --test tests/*.test.mjs
uv run ruff check src tests
uv run ruff format --check src tests
```

The default suite exercises identity matching, validation, credential redaction, database persistence, queue behavior, errors, and retries without spending search credits. Authentication tests also cover every protected route, first-owner races, CSRF, session expiry and revocation, password recovery, and migrations. Workflow fixtures sign in through the public API; authentication is never disabled for tests.

For the live suite, inject `SERPAPI_KEY` through your shell or secret manager:

```bash
uv run pytest -m live -v --tb=short
```

Live tests call the real Account API, discover and verify both Todoist store listings, run all five tracking sources, check overdue scheduling, export history, and exercise invalid-listing errors. Each test uses its own temporary SQLite database, seeded from a shared verified-app baseline. To retain the test workspaces, set `APPTRAIL_LIVE_DATA_DIR` to a parent directory. The regional tests use `APPTRAIL_REGIONAL_TEST_DIR`. Each run creates separate subdirectories.

Run `APPTRAIL_BROWSER_TESTS=1 uv run pytest -m browser` with local-port access and Chrome installed to exercise notifications, matrices, distributions, listing setup, pausing, and mobile text comparisons in an isolated fixture workspace. These checks use no SerpApi credits. To use Playwright's Chromium instead of installed Chrome:

```bash
uv run playwright install chromium
APPTRAIL_BROWSER_TESTS=1 APPTRAIL_BROWSER_CHANNEL='' uv run pytest -m browser
```

Never commit keys, credentials, `.env` files, databases, or unredacted provider responses. Avoid logging SDK exceptions directly because request URLs may contain a key.

## Packaging

```bash
uv build
uvx --from ./dist/apptrail-1.0.0-py3-none-any.whl apptrail --version
uvx --from ./dist/apptrail-1.0.0-py3-none-any.whl apptrail --no-browser
```

The wheel includes the static UI. Verify it from outside the checkout. The command's data directory is independent of the installed package and uv tool cache.

The source distribution includes application code, tests, documentation, scripts, and release configuration through an explicit allowlist in `pyproject.toml`. Inspect both archives before publishing. Local databases, credentials, IDE files, caches, and test output must not be included.

The CI workflow runs on pull requests targeting `main` and pushes to `main`, including merges. It runs Python behavior tests on Linux (Python 3.11 through 3.14) and macOS (Python 3.12), JavaScript tests, and browser workflows in Chromium. It builds the distributions and checks that the installed wheel serves its UI assets. It can also be started manually.

The separate live workflow runs on the same events and can be started manually. It uses the `SERPAPI_KEY` repository secret and spends SerpApi credits. Fork pull requests and Dependabot runs skip this job because repository secrets are unavailable to them. An eligible run fails with a setup message if `SERPAPI_KEY` is missing.

## Database changes

`PRAGMA user_version` records the schema version. Keep existing migration steps stable and add a numbered step for each schema change. Test upgrades from an older database as well as a clean database. Make a backup before upgrading a deployed workspace.

SQLite uses WAL mode, foreign keys, and a busy timeout. Partial unique indexes prevent duplicate queued/running jobs for the same query or listing. A process lock allows one active AppTrail worker per data directory. Do not run multiple Uvicorn workers against the same workspace.

Downloaded backups call `Database.backup(compact=True, include_sessions=False)` to remove raw response JSON, archived image blobs, and sessions from the copy, then run `VACUUM` on that copy. Normalized results and snapshot image hashes remain. The `backup_omissions` setting records the last affected run and snapshot IDs so restored views can explain missing content without labelling new checks as incomplete. Internal recovery and migration snapshots use full backups. Restore supports the current schema and schema 6, whose image comparison upgrade does not change the table layout.

Storage cleanup uses the same request gate as restore and stops the worker before deleting content and running `VACUUM` plus a WAL checkpoint. It only clears responses for completed runs before the selected cutoff. Image age comes from the latest snapshot reference across every media field, so deduplicated assets used by newer snapshots survive. Unreferenced assets are also removed. The `storage_cleanup` setting records each category's cutoff for missing-content notices. Measurements read blob and JSON byte lengths without loading their contents into Python; snapshot media references are indexed in a temporary table for shared-image checks.

Schema 4 stores normalized results and provider responses in `run_payloads`, separate from the small `runs` records used for polling and scheduling. Load payloads only for evidence, matching, or reanalysis. State and dashboard queries must not fetch them. The upgrade preserves a snapshot in `backups/before-schema-3.sqlite3` before moving existing payloads.

Schema 5 adds notifications, per-query alert state, regional listing watches, listing snapshots, and archived image blobs. Existing workspaces receive no enabled listing watches. The migration backs up a schema-4 database to `backups/before-schema-4.sqlite3`. Listing-history jobs use `watch_id` and a separate partial unique index so different countries can be collected independently. Images share content hashes to avoid storing identical bytes repeatedly, and authenticated asset routes serve them from the database.

Schema 6 adds explicit competitor relationships between apps and backs up existing schema-5 databases before upgrading. Apps can be competitors for more than one original app. Existing shared-query targets keep their behavior; the migration does not infer which apps are competitors.

The competitor form in Apps and competitors selects from an original app’s saved queries and countries. `POST /api/apps/{app_id}/competitors` accepts an existing app or a new app with one or two verified store listings, plus the selected monitor IDs. The relationship and target attachments are saved atomically. Incompatible store listings and searches outside the original app’s tracking are rejected. Attaching a competitor reanalyzes saved successful results and reuses existing schedules, including paused queries, without enqueueing extra searches.

Listing collection is explicitly enabled per listing/country/language. The worker checks pause and archive state before a request and again before saving its snapshot. Store API requests use the existing gateway. Image archival accepts bounded raster downloads only from Apple and Google image CDNs, checks redirects, and shows a placeholder when archival fails. Public store fields are normalized independently of the small listing statistics used elsewhere in the dashboard.

Rank alerts run only when the worker saves a new store observation. Reanalysis and competitor backfills do not emit notifications. Two successful checks must confirm a significant loss, failures interrupt confirmation, and recovery rearms the alert. Distribution comparisons use one latest observation per app/query pair per period. Matrix and distribution endpoints never fetch provider data.

## Release

The PyPI project is [apptrail](https://pypi.org/project/apptrail/) and the source repository is [serpapi/apptrail](https://github.com/serpapi/apptrail). The console entry point is `apptrail`.

### One-time GitHub setup

1. In the repository's **Settings > Secrets and variables > Actions > Secrets**, add `SERPAPI_KEY` with your SerpApi API key. Use a repository secret so the live test jobs can read it. An organization secret with access to this repository also works.
2. In **Settings > Environments**, create an environment named `pypi`. Under **Deployment branches and tags**, choose **Selected branches and tags** and add a **Tag** rule for `v*`. Release workflows run against tag refs, so a branch-only rule for `main` would block publishing. The workflow separately verifies that the tagged commit is in `main`'s history. See [GitHub's environment rules](https://docs.github.com/en/actions/reference/workflows-and-actions/deployments-and-environments#deployment-branches-and-tags).

No GitHub Actions variables or PyPI API-token secrets are required. The publishing job requests its OIDC token with `id-token: write` and runs `uv publish --trusted-publishing always` in a separate job after the build and tests succeed. See [uv's Trusted Publishing workflow](https://docs.astral.sh/uv/guides/integration/github/#publishing-to-pypi).

### One-time PyPI setup

Open [apptrail's Publishing settings](https://pypi.org/manage/project/apptrail/settings/publishing/) and add a GitHub trusted publisher with these values:

| PyPI field | Value |
|---|---|
| PyPI project name | `apptrail` |
| Owner | `serpapi` |
| Repository name | `apptrail` |
| Workflow name | `publish.yml` |
| Environment name | `pypi` |

The workflow name is the filename, without `.github/workflows/`. If the project does not exist yet, use [PyPI's pending-publisher form](https://pypi.org/manage/account/publishing/) with the same values. See PyPI's instructions for [existing projects](https://docs.pypi.org/trusted-publishers/adding-a-publisher/) and [new projects](https://docs.pypi.org/trusted-publishers/creating-a-project-through-oidc/).

### Publish a version

1. Update `pyproject.toml` and `src/apptrail/__init__.py` to the same version, then run `uv lock` to update `uv.lock`.
2. Merge those changes and the workflows into `main`, and wait for the CI and live test workflows to pass.
3. Create and publish a GitHub Release with a tag of `v<version>` targeting `main`, for example `v1.0.0` for package version `1.0.0`. Use a version that has not already been published to PyPI.

The [Publish to PyPI workflow](https://github.com/serpapi/apptrail/actions/workflows/publish.yml) starts when the release is published, including published prereleases. Saving a draft or pushing a tag alone does not publish a package. Tags without a `v` prefix are ignored; mismatched versions and commits outside `main` fail validation. The workflow reruns Python, JavaScript, and Chromium tests, builds with `uv build`, and smoke-tests both the wheel and source distribution before uploading those artifacts to PyPI. Live tests run separately and are not a publishing-job dependency.

## Countries and regional tracking

`regions.json` bundles the country lists from [SerpApi Apple Regions](https://serpapi.com/apple-regions), [Google Countries](https://serpapi.com/google-countries), and [Apple Languages](https://serpapi.com/apple-languages). Update the bundled lists when provider coverage changes. The picker offers countries supported by all selected regional sources; individual app availability can vary by store and country.

Each country, query, source, language, device, and depth combination has its own monitor and history. `POST /api/monitors/batch` saves up to 2,000 monitor specifications atomically and reuses identical searches. Bing Copilot is normalized to one global monitor. `country` and `source` filters apply to dashboard data, CSV exports, and manual checks. Manual listing refreshes use the country where the listing was connected. They are requested through `POST /api/apps/{app_id}/refresh` and are excluded from scheduled usage estimates.

Country comparison charts use saved observations, with one series per selected country for a single source and optional query. `static/history-chart.js` aggregates successful daily rankings or AI mention rates, preserves missing values, and excludes global Copilot results from country comparisons.

The setup checklist derives completed steps from saved apps and queries. `POST /api/onboarding` persists skip, dismiss, and restore actions in SQLite settings. It uses the same session and CSRF checks as other workspace writes.
