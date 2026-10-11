# Phase 28 — CI/CD Pipeline

**Status: built, and every job run — on the workflow files as they are committed — by a local runner. NOT yet seen on GitHub, and the deploy has nothing to deploy to: this build could not read the repository's Actions (it is private, and the build has no GitHub login), and Phase 27's accounts do not exist yet.** What that leaves open, and the three things that close it, are in [On GitHub](#on-github-what-to-look-at-and-what-is-yours-to-set).
**What the phase delivers (roadmap):** Push to feature branch → lint + tests in under 3 minutes. Merge to main → auto-deploy. Nightly job runs and posts results.

Decisions: `DECISIONS.md` #208–#220.

Four things are worth knowing before reading on.

- **Nothing here was watched on GitHub.** The repository is private; without a login its runs, timings and
  issues cannot be read. So the workflows were run by [`act`](https://github.com/nektos/act), which executes
  the files themselves in containers, with the service containers they ask for — and the build is exact about
  what that does not show (#219).
- **"A broken test must fail the pipeline and block merge" is two things.** The first is the pipeline's, and is
  shown. The second is a repository setting — branch protection — which GitHub does not enforce for a private
  repository on the free plan (#211). What the pipeline does by itself: a commit that fails on `main` is never
  deployed.
- **The deploy job passes without deploying, on purpose.** Until the secret and the variables exist it says
  "Nothing was deployed" on the run and is green (#213). The day they are set, the same merge deploys.
- **Four faults were found by running things rather than reading them** — one of which would have shown a
  perfect night when the tests had not run at all. They are under [Found and fixed](#found-and-fixed-along-the-way).

## What was built

```
.github/workflows/ci.yml               ← REWRITTEN: every push — `backend` and `frontend`, side by side; on main,
                                         `deploy` after both
.github/workflows/deploy.yml           ← NEW: is there a target? → the commit told to the service → `railway up --ci`
                                         → `railway status` → wait until the merged commit is what answers
.github/workflows/nightly.yml          ← NEW: 02:00 UTC — the integration tests on a Postgres and a Redis of their own,
                                         the eval suite once there is one, one comment, then the verdict
.github/nightly-issue.md               ← NEW: what the nightly issue is opened with
scripts/wait_for_release.py            ← NEW: asks GET /version at the backend, the site and the site's /api until all
                                         answer with the commit; says how long merge-to-live took
scripts/nightly_summary.py             ← NEW: JUnit reports → one Markdown comment, and the night's verdict as exit code
scripts/nightly_post.sh                ← NEW: finds (or makes) the issue labelled `nightly`, adds the comment
tests/ci/fake_gh.sh                    ← NEW: a stand-in for GitHub's CLI that writes down what it is asked
tests/unit/test_phase28_pipeline.py    ← NEW: 30 tests — the workflow files held to what they must do, and the scripts

src/backend/app/api/routes/health.py   ← GET /version → {"version": "0.28.0", "commit": "<sha>" | null}
src/backend/app/core/config.py         ← GIT_COMMIT_SHA; `settings.commit` (the pipeline's, else Railway's own)
src/backend/app/core/error_reporting.py← a Sentry report's release names the same commit
src/frontend/next.config.mjs           ← the build is told its commit (Vercel's, else the pipeline's)
src/frontend/src/app/version/route.ts  ← NEW: GET /version on the site → {"commit": "<sha>" | null}
docker/prodlike/compose.yml            ← PRODLIKE_COMMIT → the stand-in deployment's commit, to rehearse a deploy
.env.example                           ← GIT_COMMIT_SHA, documented
```

## What happens, and when

```
a push to any branch ──► CI ─┬─ backend   (≤10 min)  install pinned → ruff check + format → migrations up / check /
                             │                        down / up on pgvector:pg16 → pytest tests/unit tests/contract
                             └─ frontend  (≤10 min)  npm ci → tsc --noEmit → eslint → next build

…and when the branch is main:
                             both green ──► deploy (≤20 min)
                                              is there a target?  no → "Nothing was deployed", green, done
                                              yes → railway variable set GIT_COMMIT_SHA=<sha> --skip-deploys
                                                  → railway up --ci          (exit 1 if the build or the start fails)
                                                  → railway status
                                                  → wait_for_release.py: <api>/version, <web>/version, <web>/api/version
                                                    all say <sha>            → "abc1234 is live — from the merge: N s"
                             (meanwhile Vercel has built the site from the same push, by its own integration)

02:00 UTC, every night ──► Nightly: integration tests (real Postgres + Redis) → [eval suite, Phase 33]
                             → summary from the JUnit reports → comment on the issue labelled `nightly`
                             → and only then: a night in which something failed is a failed run
```

## Dev A — CI pipeline

| The roadmap asks | What was built | Seen |
|---|---|---|
| On every push: `ruff check src/`, `pytest tests/unit/ tests/contract/ -v` (no network) | The `backend` job: `ruff check . && ruff format --check .` (the whole repository, and its formatting — a superset), then the roadmap's pytest command word for word. Pushes only: `pull_request` was dropped, it ran everything twice (#209) | Runner: 1,010 passed in 18 s; no network — the job has no keys and no `.env` |
| A deliberately broken test in a PR must fail the pipeline and block merge — test this before marking the phase done | A test asserting `1 + 1 == 3`, pushed as a commit to a copy: the job fails at "Unit + contract tests". Blocking is branch protection: `HOW_TO_RUN.md` Step 26 D.1 | Runner: `1 failed, 1008 passed`, job failed, exit 1. **Not seen: a blocked merge button** — a setting, and one this repository's plan does not enforce (#211) |
| `alembic check` step: fails the build if DB schema is ahead of migrations | Four commands against a `pgvector/pgvector:pg16` service: `upgrade head`, `check`, `downgrade base`, `upgrade head` (#210) | Runner, with a column added to `Trip` and no migration: `FAILED: New upgrade operations detected: [('add_column', None, 'trips', Column('a_column_nobody_migrated', …))]`, job failed in 15 s |
| **Acceptance:** lint catches a ruff violation | Two unused imports in a new file | Runner: `F401 [*] \`os\` imported but unused … Found 2 errors.`, job failed at "Lint (ruff)" before anything slower ran |
| **Acceptance:** tests run in under 2 minutes | — | Runner: the tests step 18 s; the whole `backend` job 96 s the first time (nothing cached), about 30 s after. **GitHub's own runners: not measured** |

## Dev B — CD pipeline & nightly

| The roadmap asks | What was built | Seen |
|---|---|---|
| CD on merge to main: GitHub Actions → `railway up` | The `deploy` job of the CI run, after both checks, on `main` only; `railway up --ci --service …` — `--ci` so that a failed build or a crashing start is exit 1 (#212). Railway's own deploy-on-push is to be switched off: one deployer (#220) | Runner, healthy `main`: the job runs and says "Nothing was deployed". Runner, failing test on `main`: the job does not start. **Not seen: `railway up` itself** — no account |
| …+ Vercel auto-deploy via GitHub integration | Nothing in this repository deploys the site: that is a setting in Vercel (Root Directory `src/frontend`). What the repository adds: the site is built knowing its commit and says it at `/version` (#215) | A production build with `GIT_COMMIT_SHA` set answers `{"commit": "<sha>"}` — the rehearsal below. **Not seen: Vercel** |
| …verify via `railway status` | `railway status`, and then the thing itself is asked: `scripts/wait_for_release.py` (#214) | The rehearsal below |
| Nightly job (`0 2 * * *`): `RUN_INTEGRATION=1 pytest tests/integration/ -v` | `nightly.yml`, that cron, that command (plus `--junitxml`), on service containers: `pgvector/pgvector:pg16` and `redis:7-alpine` | Runner: `60 passed in 23 s` |
| …+ eval suite from Phase 33 | A step that runs `pytest tests/eval/` once the directory exists, and until then says there is none; the comment has a row for it: "not run: no eval suite yet (Phase 33)". A test fails the day `tests/eval/` appears and the row is still there | Runner: the step passes in 0.06 s and says so |
| …+ posts summary to a dedicated GitHub issue via `gh issue comment` | `scripts/nightly_post.sh`: the open issue labelled `nightly`, made the first night; `gh issue comment <n> --body-file summary.md` (#216) | Runner, against the stand-in `gh`: "made the nightly issue: #7", "posted the night's summary to issue #7". **Not seen: a comment on GitHub** |
| **Acceptance:** merge to main → automatic Railway + Vercel deploy within 5 minutes | The pipeline measures it: merge-to-live on the run's page, a warning past 300 s | **Not done** — nothing to deploy to. The mechanism: the rehearsal below |
| **Acceptance:** nightly job posts results to GitHub issue | As above | **Not seen on GitHub.** A schedule runs only from `main`: the first night is the one after this is merged — or Actions → Nightly → Run workflow |

## Found and fixed along the way

**A night whose tests never ran would have read as a perfect night.** The comment is written from the tests'
JUnit reports. When pytest dies before its first test — an import error, a database that never came up — it
writes no report; no report has no failures in it. The summary now takes the names of the suites that *must*
have run: a missing report is `DID NOT RUN`, a report that cannot be parsed is `DID NOT FINISH`, and both are
failures (#217). A test that could not begin (pytest's "error", not "failure") is counted and named too — a
gap the break-on-purpose run found, not the first draft of the tests.

**On a bad night, the run's own page would have been empty.** GitHub runs a step with `bash -e`: the first
command that fails ends the script. On a bad night the summary script exits 1 — that is its verdict — so the
two lines after it, which put the summary on the run's page and pass the verdict on, never ran. The comment
was still posted and the run still failed; the page simply did not say why. Found by a test that runs the
step's script the way GitHub's shell does; fixed with `|| verdict=$?`.

**The posting script did not parse on a Mac.** `"${1:?usage: … the night's summary}"` — inside `${…}`, bash 3.2
(which macOS still ships) takes the apostrophe for the start of a quoted string, and the whole script fails
with "unexpected EOF". GitHub's runners have bash 5, where it is fine: the script would have worked there and
failed for anyone trying it by hand. The apostrophe is gone, and a breakage that puts it back is caught.

**The wait for a deploy could not be handed a clock.** `def wait(…, fetch=fetch_json, sleep=time.sleep,
clock=time.monotonic)` binds those when the function is defined, so a test that patched `time.sleep` patched
nothing: it waited ten real minutes on an address that does not exist. They are looked up when the function is
called now.

**`act` hands the repository's `.env` to every job.** Unasked. The nightly job's own `DATABASE_URL` was
replaced by the laptop's (its first run could not log in to its database — which is how the bad-night path was
first seen working), and the laptop's provider keys were inside the runner. GitHub does nothing of the kind.
Every run recorded here was made with `--env-file`, `--secret-file` and `--var-file` pointing at an empty file;
`HOW_TO_RUN.md` says so in bold.

**A pull request ran everything twice.** The old workflow started on `push` and on `pull_request`. Beyond the
cost: two checks of one name on one commit, one of which can be "skipped" — and a required check counts a
skipped one as passed (#209).

## Live results (the local runner, 2026-10-11)

`act` v0.2.89, runner image `catthehacker/ubuntu:act-latest`, on an Apple-silicon Mac. No `.env`, no secrets,
no variables. `actionlint` v1.7.12: no findings in the three files.

### The four jobs, on the repository as it stands

| Job | Result | Steps |
|---|---|---|
| CI / `backend` | ✅ | install (pinned) → lint 0.1 s → migrations up / check / down / up 1.0 s → **1,010 passed** 18 s |
| CI / `frontend` | ✅ | `npm ci` → types 1.5 s → lint 1.2 s → production build 13 s |
| Nightly / `integration tests` | ✅ | **60 passed** 23 s on the job's own Postgres and Redis → "✅ Nightly, 2026-10-11 — 60 passed" → posted (stand-in) |
| Deploy / `backend → Railway` | ✅ | "Nothing was deployed: the secret RAILWAY_TOKEN or the variable RAILWAY_SERVICE is not set" — 0.05 s, no other step ran |

The first run of each, with nothing cached: `backend` 96 s (37 s of it installing Python, 39 s installing the
dependencies), `frontend` 124 s (21 s Node, 42 s `npm ci`). Both together are inside the roadmap's three
minutes on this machine; GitHub's runners have a cache for both installs and were not measured.

### Broken on purpose — each on a copy, pushed through the same files

| The breakage | Where it stopped | |
|---|---|---|
| a test that asserts `1 + 1 == 3` | `backend` → Unit + contract tests: `1 failed, 1008 passed` | exit 1, 30 s |
| two unused imports | `backend` → Lint (ruff): `F401 … Found 2 errors.` | exit 1, 15 s — nothing after it ran |
| a column on `Trip`, no migration | `backend` → Migrations: `FAILED: New upgrade operations detected: [('add_column', None, 'trips', …)]` | exit 1, 15 s |
| `const count: number = "not a number"` | `frontend` → Types: `error TS2322: Type 'string' is not assignable to type 'number'.` | exit 1, 50 s |
| the failing test, **on `main`** | `backend` failed, `frontend` passed — **no line from a deploy job in the log** | exit 1, 67 s |
| nothing broken, on `main` | both passed → `deploy` ran: "Nothing was deployed" | exit 0, 39 s |
| nothing broken, on a branch | both passed → no deploy job | exit 0, 32 s |

(These seven were run when the suite had 1,008 tests; it has 1,010 now.)

### Two bad nights — each on a copy, through `nightly.yml`

| The night | What the run did | |
|---|---|---|
| one integration test fails (`assert 1 + 1 == 3`) | tests: `1 failed, 60 passed` → the night goes on → summary on the run's page: "❌ Nightly, 2026-10-11 — 1 failed of 61", **What failed:** … → posted (stand-in) → reports kept → "A night in which something failed is a failed run" | exit 1, 37 s |
| the tests cannot start (`tests/integration/conftest.py` raises) | pytest exits in 0.4 s and writes no report → summary: "❌ Nightly, 2026-10-11 — 1 failed", `\| integration \| — \| — \| — \| DID NOT RUN — no report was written \|` → posted → failed | exit 1, 15 s |

The second is the one that matters: without `--require integration` that night's comment would have been a
table with no failures in it.

### A deploy, rehearsed

The production-like stack of Phase 27 (the deployed image, TLS-only Postgres behind a pooler, TLS-only Redis)
stood in for Railway, and a production build of the frontend for Vercel.

```
the old commit, deployed
  backend /version       {"version":"0.28.0","commit":"45c070e92143f0eeb7d1af3e63910e3c40336d97"}
  site /version          {"commit":"45c070e92143f0eeb7d1af3e63910e3c40336d97"}
  site /api/version      {"version":"0.28.0","commit":"45c070e92143f0eeb7d1af3e63910e3c40336d97"}

a commit that is never deployed:  wait_for_release.py --commit 4f7c8e7… --timeout 12
  error: the backend is not running 4f7c8e7 after 12 s — last answer: commit 45c070e92143
  error: the site is not running 4f7c8e7 after 12 s — last answer: commit 45c070e92143
  error: the site's /api is not running 4f7c8e7 after 12 s — last answer: commit 45c070e92143
  exit 1

the merge: the waiter is started, then the backend is replaced (5 s) and the site rebuilt and replaced (8 s)
  ✓ the backend is running 4f7c8e7  (after 10 s)
  ✓ the site is running 4f7c8e7  (after 10 s)
  ✓ the site's /api is running 4f7c8e7  (after 10 s)
  **4f7c8e7 is live** — the backend after 10 s, the site after 10 s, the site's /api after 10 s.
  From the merge to live: **11 s** (the aim: within 300 s).
  exit 0
```

Eleven seconds is one machine replacing two processes; it says nothing about Railway's build or Vercel's. What
the rehearsal shows is the part that is this repository's: all three addresses carry the commit, an old one is
not mistaken for the new one, and the wait ends when — and only when — the last of them has changed.

## Tests

**1,010 unit + contract, 60 integration, 60 browser — all passing.** New in this phase: 30 unit
(`tests/unit/test_phase28_pipeline.py`).

- **The workflow files, read** (11): which events start each; the backend job's four commands, in order, on the
  pinned versions, against a Postgres with pgvector; the frontend job's four; the deploy needing both jobs and
  `main`; every step of a deploy behind "is there a target"; the nightly's cron, services and `RUN_INTEGRATION`;
  the comment posted before the run is failed; a time limit on every job; no workflow may write to the
  repository; only GitHub's own actions, each at a named version; Python 3.11, as in the image; every script a
  workflow names exists.
- **Two steps, run the way GitHub's shell runs them** (2): the deploy's "is there a target" with no token, half a
  target and a whole one; the night's summary step on a night with no report and on a good one.
- **`GET /version`** (3): null on a laptop, the commit when deployed, `no-store`; the pipeline's commit wins over
  the platform's; the site's build reads Vercel's commit or the pipeline's (run through `node`).
- **The wait for a deploy** (6): a short SHA and a long one are one commit, six characters are not enough; the three addresses; then, on a clock that only moves when something sleeps: it ends the moment the last
  address is live and not before; an address that is live is not asked again; one that does not answer is; a
  deploy that never arrives is given up exactly on time, with each address's last answer; past five minutes is
  a warning and exit 0.
- **The night's summary** (6): a good night word for word; a bad one names each test and the first line of why;
  no report, an unreadable report, and nothing-ran-at-all are failures; forty failures are cut at fifteen "and
  25 more"; and what pytest really writes — a pass, a failure and a fixture that raises — is read correctly.
- **Posting it** (2), against the stand-in `gh`: the first night makes the label and the issue and comments; the
  second only comments, on the same issue; an empty summary posts nothing and exits 1.

### Break it on purpose

87 deliberate breakages, each applied to a scratch copy of the repository and the Phase 28 tests run on it:
**84 caught on the first pass; all 87 after one test was widened and one breakage re-written.** The two that
survived were of one kind — a test that *errors* was not counted, and not named, in the night's summary — and
no test had a test that errors in it. One did now. (The third was a breakage whose text did not match the
file; re-written, it is caught.)

| Where | Breakages | For example |
|---|---|---|
| `ci.yml` | 18 | `alembic check` removed · the deploy needing only `backend` · the deploy from any branch · `pull_request` back · no constraints file · a Postgres without pgvector · a job with no time limit |
| `deploy.yml` | 13 | `railway up` not behind "is there a target" · no `--ci` · half a target counting as one · no target reading as a target · the wait followed by `\|\| true` |
| `nightly.yml` | 14 | weekly instead of nightly · `RUN_INTEGRATION` gone · the post skipped after a failure · a bad night as a green run · the summary step stopping at its own failure · the report under another name |
| `wait_for_release.py` | 14 | one address live being enough · waiting ten times as long as told · not live as exit 0 · any two strings being the same commit · the site's `/api` not asked |
| `nightly_summary.py` | 13 | a missing report not a failure · nothing-ran as a good night · errors not failures · the traceback instead of the reason · exit code always 0 |
| `nightly_post.sh` | 7 | a new issue every night · an empty summary posted · the apostrophe put back |
| the backend and the site | 8 | Railway's commit winning over the pipeline's · an error report not naming the commit · Vercel's commit not read |

## Done criterion checklist

| | Criterion | |
|---|---|---|
| ✅ | Lint catches a ruff violation | the runner: job fails at Lint |
| ✅ / ◻ | Tests run in under 2 minutes | 18 s for the tests, 96 s for the whole job cold, on the local runner. Not measured on GitHub |
| ✅ | `alembic check` step works | the runner: a model ahead of its migrations fails the job |
| ✅ / ✗ | Broken test fails the pipeline / blocks merge | fails: shown. Blocks: a repository setting, not set — and not enforceable on a free private repository (#211) |
| ✗ | Merge to main → automatic Railway + Vercel deploy within 5 minutes | not done: no Railway service, no Vercel project. Built, gated, rehearsed; says "Nothing was deployed" until configured |
| ◻ | Nightly job posts results to GitHub issue | the job and the posting run by the local runner against a stand-in `gh`. Not seen on GitHub |

✅ seen · ◻ built and run locally, not seen on GitHub · ✗ not done

## Known limits

- **GitHub itself has not run any of this where it could be watched from here.** What a local runner cannot
  show: the real runners' speed and their caches; whether the job's token may create a label, an issue and a
  comment (`issues: write` is declared; the stand-in does not check permissions); a schedule firing; a reusable
  workflow being handed its secrets (`secrets: inherit`) and variables.
- **The schedule is the roadmap's `0 2 * * *`, and GitHub says the top of an hour is when scheduled runs are
  most often late — and that under enough load one can be dropped.** Nothing notices a night that never ran:
  the issue simply has no comment for that date. If that happens, move the minute off `0`.
- **The first deploy will find things no rehearsal could:** whether `railway variable set … --skip-deploys`
  followed by `railway up` behaves as its documentation says on this project; whether a project token is enough
  for `railway status`; how long Railway's build takes with a cold layer cache. If `railway up` fails after the
  variable was set, the service's variables name a commit that is not running — the running deployment itself
  still answers with its own.
- **The site is not held back by the checks** (#215). Vercel builds every push to `main` the moment it arrives;
  a commit whose tests fail can reach the site while the backend stays on the one before.
- **Each push runs CI on its head, not on the result of merging it** (#209) — "Require branches to be up to
  date" covers that where a branch can be protected.
- **A push to a `nightly-check/…` branch posts a real comment** on the real issue. That is what it is for;
  delete the comment afterwards if it is noise.
- **Minutes.** A free private repository has 2,000 a month, each job rounded up to a minute. The estimate —
  4–5 a push, 2–3 a night — comes from the local runs and is not GitHub's figure.

## On GitHub: what to look at, and what is yours to set

The steps, with where to click, are `HOW_TO_RUN.md` Step 26 D. In short:

1. **Look** — after this branch is pushed, the commit shows two checks, `backend` and `frontend` (Actions →
   CI). Their durations are the numbers this log does not have.
2. **Branch protection on `main`** requiring `backend` and `frontend` — needs GitHub Pro while the repository is
   private (or a public repository). Without it the checks inform and do not block.
3. **For deploy-on-merge**, after the accounts of Phase 27 exist: the secret `RAILWAY_TOKEN`; the variables
   `RAILWAY_SERVICE`, `PRODUCTION_API_URL`, `PRODUCTION_WEB_URL`; Railway's own automatic deployments switched
   off; Vercel's GitHub integration on (Root Directory `src/frontend`).
4. **The night** needs nothing: it starts the night after the merge. Actions → Nightly → Run workflow has one
   at once; the issue "Nightly test results" appears with its first comment.
