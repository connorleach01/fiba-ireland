# CLAUDE.md

Guidance for Claude Code working in this repo. Read this before changing anything.

## What this is

Fast-turnaround basketball analytics for the **Ireland U16 national team** at
**FIBA U16 EuroBasket 2026, Division B** (81 games, 6-16 August 2026, Gevgelija
and Skopje, North Macedonia). Ireland open against the Netherlands on
**Thursday 6 August, 10:00 Irish time**.

The team had no analytics of any kind. They play on consecutive days, so the job
is to turn a finished game into a usable opponent scouting report within minutes.
Connor volunteers this; the audience is coaching staff reading on phones in a gym,
not analysts.

**Live site: https://connorleach01.github.io/fiba-ireland/**
Repo `connorleach01/fiba-ireland`, public. GitHub Pages serves `docs/` off `main`.

## The data source, and why it is easy

FIBA's site is a Next.js React Server Components app that **server-renders the
complete Genius Sports LiveStats feed into each game page's HTML**. One plain
HTTP GET per game returns box score, full play-by-play, every timestamped
substitution, and shot x/y coordinates. No API key, no browser, no JavaScript
execution. `fiba/parse.py` reassembles the `self.__next_f.push([1,"…"])` chunks
and pulls objects out by anchor plus balanced-brace scan.

The event's `/games` page returns **all 81 games in one response**, each with a
`statusCode` that flips `INIT` → `VALID` when the game is final. That flip is the
scrape trigger: one cheap URL says what is new.

Game URL: `/en/events/{event-slug}/games/{gameId}-{teamACode}-{teamBCode}`

## Commands

```bash
cd ~/code/fiba-ireland

.venv/bin/python -m fiba.watch --once      # one poll, rebuild, publish
.venv/bin/python -m fiba.watch             # poll every 5 min until stopped
.venv/bin/python -m fiba.watch --rebuild   # regenerate reports from stored data
.venv/bin/python -m fiba.watch --example   # rebuild the U18 reference set
.venv/bin/python -m fiba.watch --publish   # commit and push docs/ only
.venv/bin/python -m fiba.watch --backfill <event-slug>

.venv/bin/python -m tests.test_pipeline            # offline regression tests
.venv/bin/python -m tests.validate_lineups <slug>  # lineup accuracy distribution
```

Add `--no-publish` to any build to skip the git push. Use it while iterating.

Preview locally: `python3 -m http.server 8899 --directory docs` then open
`http://localhost:8899/example/` (the reference set is the only populated site
until the tournament starts).

Deps are `requests` and `jinja2` in `.venv`. Nothing else. Do not add a
framework, a bundler, or a CSS library.

## Layout

```
fiba/
  config.py    paths, event slug, Ireland org id (81), shot-chart calibration
  fetch.py     HTTP with polite spacing and backoff; atomic gzip cache in raw/
  parse.py     RSC payload -> dicts, with invariants that raise
  clock.py     period + game-clock conversions (shared; avoids a circular import)
  db.py        SQLite schema and upserts
  ingest.py    parsed game -> rows; runs lineup reconstruction; sync_event()
  lineups.py   substitutions -> stints, validated against official minutes
  metrics.py   four factors, possessions, player advanced, shot zones
  analysis.py  query layer that assembles report inputs
  charts.py    inline SVG: shot chart, four-factor bars, margin timeline
  theming.py   flag badges; country colours with CVD and contrast checks
  report.py    Jinja2 -> HTML; build_all() is the entry point
  deploy.py    commits docs/ and pushes
  watch.py     the poller and the CLI
templates/     _base (all CSS + JS), _macros, index/scout/review/tournament/teams
docs/          the published site (GitHub Pages source)
docs/example/  U18 reference set, stamped "not live U16 data"
raw/           gzipped scraped pages, gitignored
data/fiba.db   SQLite, gitignored
```

`raw/` and `data/` are gitignored but **do not delete them casually**: `raw/`
lets any parser change be replayed offline against 159 real games without
refetching, which is the main safety net.

## Non-obvious things that will bite you

**Two payload encodings.** FIBA emits player stats either inlined or as React
back-references like `"$1b:props:gameDetails:c:0:Children:0:Stats"`. Both appear
in the same event. The anchor-based parser handles both; a regex tuned to one
will silently miss the other.

**Never trust, always check.** Every parse asserts player points sum to the team
total, the team total matches the final score, `FG2M+FG3M == FGM`, and the
shooting line implies the points scored. A game that fails is refused, not
half-loaded. Keep it that way: a quietly wrong box score is far worse for a
coaching staff than a scrape that refuses to run.

**Lineups are validated per player.** Derived minutes are compared to the
official box score; a game that fails is marked `lineups_ok=0` and reports
suppress its lineup sections rather than show plausible-but-wrong numbers.
Measured across 1,928 player-games: 85.8% exact, 100% within 5s, worst 2s.
`TOLERANCE_SECONDS = 10` is deliberately just above that noise floor so it trips
immediately if FIBA changes how it reports substitutions.

**Live polling never reads the cache.** A game we have not ingested is either new
or previously failed, so `sync_event` fetches fresh when `only_new=True`.
Reusing the cache there could pin us to a page captured while the box score was
still being published, and no amount of retrying would get past it. A page that
fails to parse has its cache entry deleted so the next poll starts clean.

**Cache writes are atomic** (temp file + rename). The poller and a manual command
can run at once; a half-written gzip caused a real failure during development.

**Pts/att is exactly twice eFG%.** Do not show both. Player tables show Pts/att
because it reads directly (1.00 means a shot is worth a point).

**DREB% is the exact complement of the opponent's OREB%**, so they always sum to
100. That is arithmetic, not a bug.

**The event-average row is identical on offence and defence.** Every game
contributes both of its teams, so the competition is its own opponent. Also not a
bug; the leaderboard note says so.

**The scoring categories overlap** (paint, fast break, second chance, off
turnovers, bench) and do not sum to total points: a fast-break layup is also
points in the paint, and bench points cut across all of them. Any table showing
them must say so.

**Free throws are logged at `x:0, y:0`** and carry no location. Exclude them from
shot charts.

**About 0.4% of attempts fall outside the plotted half court** (half-court heaves
out to 13 metres). The SVG does not clip, so left alone they float over the
heading. `charts.shot_chart` clamps them to the frame and says how many were
moved. Only genuine outliers are counted: nudging a baseline attempt in by a
marker radius is presentation, not a moved datum.

## Design rules

The output is a **printed stats report, not a dashboard**. A4 paper geometry,
paper-white only with **no dark mode by choice**, ruled sections, hairline tables,
tabular figures, small type. `Cmd-P` must keep producing a clean PDF: short blocks
are held whole via `section.keep`, long tables flow and break only between rows.
A game review is about 3 A4 pages.

All CSS and JS live inline in `templates/_base.html.j2`. Every page is a
self-contained file with **no external requests**, works offline, and must remain
fully readable with JavaScript disabled. The interactive parts (sortable tables,
the leaderboard Offence/Defence/Combined toggle, expandable player rows, nav
dropdowns) are a few dozen lines of plain DOM code. Keep it that way.

**Team colours come from national flags but are computed, not assumed.**
`theming.pick_pair()` checks contrast on paper and separation under protanopia,
deuteranopia and tritanopia, and scores the OKLab a/b plane **separately from
lightness**: two greens differing only in lightness read as one colour on
adjacent bars however large their total distance. When two teams collide it
reaches for the opponent's second flag colour before nudging the first, so
Ireland v Portugal is green against Portuguese red. All 380 orderings in the
22-team field keep both countries' colours; the test suite asserts this.

Flags are inline SVG simplified to field colours and the major device, because
they render about 16px wide. Unknown codes fall back to a neutral chip.

**Charts do not repeat what the table already says.** The four-factor bars are
deliberately unlabelled because the table above them carries the numbers.

**Offence and defence sit side by side in a `.chartpair` grid**, offence left and
defence right, on every page that shows shots. It is a CSS grid rather than flex
so the two halves are exactly equal: the courts then render at the same size and
the zone tables beneath them start on the same line. Each column carries one
heading, so `zone_table` is called with `caption=''` there.

**Small samples are labelled and thin ones are withheld.** Per-100 figures are
not published below `analysis.MIN_RATE_SECONDS` (180s) of shared court time; the
raw minutes and plus-minus still show, because those are facts.

**Failures are isolated.** `report._safe()` means one broken template does not
cost the staff the other twenty-five reports.

## Navigation model

Nav is Dashboard, Ireland, Teams, and a Scouting dropdown. **There is no Games
menu**: individual games are reached from the team they belong to. Ireland's game
log links each opponent to that game's review, and the dashboard lists recent
results the same way. Reviews are only built for Ireland's games.

## Conventions

- **No em dashes in prose.** Use commas, colons, periods. Applies to generated
  report text and to code comments.
- Comments explain **why**, not what. Match the existing density: the tricky
  decisions are commented, the obvious lines are not.
- British spelling in user-facing copy ("Offence", "colour").
- Report copy addresses coaches, names teams explicitly ("Shots Ireland took",
  never "Shots we took"), and states caveats inline rather than hiding them.

## Verification

`tests/test_pipeline.py` runs fully offline against the cached pages and covers
the clock, both payload encodings, metrics against FIBA's published percentages,
four-factor arithmetic, shot zones, lineup validation, small-sample guards, the
empty-event shape, and the theming rules. Run it after any change.

Built and validated against **159 completed games** across two past events, both
already in `data/fiba.db`:

- `fiba-u16-eurobasket-2025-division-b` (81 games), same age group
- `fiba-u18-eurobasket-2026-division-b` (78 played), Ireland played 7

Percentages match FIBA's published figures to within 0.055pp across 318
team-games. Derived per-game scoring and efficiency averages match FIBA's own
published Ireland leaders exactly.

After changing templates, rebuild both sites and eyeball the output in a browser,
the tests check data, not layout:

```bash
.venv/bin/python -m fiba.watch --example --no-publish
.venv/bin/python -m fiba.watch --once --no-publish
```

A quick internal-link check over `docs/example/` is worth running after any
navigation change; there are about 810 internal links across 31 pages.

## Known limits

- 5-7 games per team over a 40-minute game, so a most-used five may total only
  8-12 minutes. Sample sizes appear next to every lineup for that reason.
- Early opponents may have played only one or two games when Ireland meet them.
  Previous years' U16 data is a different birth-year cohort and is not used.
- Lineup plus-minus is raw points for and against; there is no possession-exact
  stint accounting, which is why net-per-100 was removed from lineup tables.
- Shot coordinates are calibrated empirically: basket at feed coordinates
  (140, 28), about 17.9 units per metre. `metrics.zone_text_agreement()`
  cross-checks the geometry against FIBA's own shot descriptions and reports drift.

## State as of 2 August 2026

The U16 event has not started, so `docs/` holds only the dashboard and an empty
leaderboard, with a banner pointing at `docs/example/`. Everything is built,
validated and deployed; the poller is not yet running as a launchd agent
(`com.fiba.ireland.watch.plist` is ready to install).
