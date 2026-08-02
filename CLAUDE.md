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
`--event SLUG` and `--org ID` retarget a run at another event or team,
`--interval N` changes the poll gap, `--verbose` turns on debug logging.

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

**A failing template does not look like a failure.** `report._safe()` logs the
exception and moves on, so the page keeps whatever content it had from the last
build and the run reports success. If a change does not appear to take effect,
that is the first thing to suspect. Call the builder directly to see the real
traceback:

```bash
.venv/bin/python -c "
import logging, pathlib; logging.basicConfig(level=logging.INFO)
from fiba import db, report, analysis
report.REPORTS_DIR = pathlib.Path('docs/example')
conn = db.connect()
report.build_teams(conn, 'fiba-u18-eurobasket-2026-division-b', 81)"
```

**Never name a template variable `values`, `items` or `keys`.** Jinja resolves
dot access to attributes before dict keys, so `row.values` silently returns
`dict.values` and the render fails deep inside Jinja with a confusing message.
The leaderboard row key is `stats` for exactly this reason.

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
fully readable with JavaScript disabled. The interactive parts are a few dozen
lines of plain DOM code and should stay that way:

- **Sortable tables** on any `th[data-sortable]`. Aggregate rows stay pinned,
  blanks sort last, `data-value` overrides what a cell sorts on (that is how
  "3-4" sorts by win percentage), and `data-rank-column` renumbers a rank column.
- **View toggles** (`.viewtoggle`) showing one column group at a time: four on
  the leaderboard (Advanced / Box score / Shooting / Scoring), three on a game
  log (Offence / Defence / Combined).
- **Rank toggles** (`.ranktoggle`) adding `show-ranks` to a table.
- **Expandable player rows**, keyboard accessible, one open at a time.
- **Nav dropdowns.**

Print hides all of it and shows every column group, so a printed page is never a
partial view of what is on screen.

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

**Any container holding a chart needs a definite width.** `.viz svg` sets
`width: 100%`, and a percentage against a shrink-to-fit parent is circular, so
the browser falls back to whatever space the neighbours left over. That is how
the player charts, all emitted at `width="228"`, ended up rendering at 228, 460
and 464 depending on how wide each player's zone labels made the table beside
them. `.detail-chart` therefore carries an explicit `flex: 0 0 228px` plus
`width: 228px`. If you add a new chart, give its container a definite basis and
check the rendered widths in the browser rather than trusting the SVG attribute.

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
menu**: individual games are reached from the team they belong to, through the
game log on that team's page or the dashboard's recent results.

**One game sheet per game, covering both teams**, not one per viewpoint. The
filename comes from the fixture (`{date}_{teamA}-v-{teamB}_game.html`) so every
team's log links to the same file; `game_filename()` is the only thing that
should generate it. `build_review`'s `org_id` argument only decides which team is
listed first, so Ireland leads on its own games. Sheets are built for every
finished game in the event, which is what makes the scouting game logs clickable.

For a full event that is about 80 sheets at roughly 190 KB each. The U18
reference set is 102 pages and `docs/` runs to about 19 MB. Fine for Pages and
for git, but per-page weight now matters: shot charts are the bulk of it.

## Ranks and percentiles

`analysis.TEAM_METRICS` declares every rankable team number, its group
(advanced / box / shot / scoring) and whether bigger is better.
`analysis.team_metrics()` computes them and `event_ranks()` ranks each one, ties
sharing a rank. Metrics with **no** better/worse direction carry `better: None`:
they still get a rank but never a shading tier, because taking a lot of threes or
playing fast is a style, not an achievement. The denominator counts only teams
that have played.

`analysis.player_percentiles()` does the same for players, against everyone in
the event clearing `MIN_MPG_FOR_PERCENTILE` (10 MPG). Players below the threshold
appear in tables but unranked, and the note under the table says what the pool is.

Both are event-wide, so `build_all` computes them **once** and threads them
through as `context`; do not call them per page.

Shading is a five-step diverging tint (blue good, red bad, unshaded middle) that
is off by default and turned on by a `rank_toggle`. The rank number always prints
alongside the tint, so colour is never the only cue. Game sheets get no ranks at
all: a single game is not a ranking.

`LEADERBOARD_GROUPS` drives the leaderboard's four views, so adding a column is a
one-line change there plus an entry in `TEAM_METRICS`.

## Deliberately not shown

Things that were built and then taken out. Do not reinstate them without a
reason, and if you do, say why in the commit.

- **On/off in game sheets.** A single game is far too few possessions for on/off
  to describe anything but noise.
- **Net rating in lineup tables.** Coaches found it confusing next to a raw
  plus-minus, and there is no possession-exact stint accounting behind it.
  Lineup tables show minutes, points for, points against and plus-minus.
- **TS% in page header strips and player tables.** It survives in the expanded
  player panel. eFG% left the player table too, because Pts/att is the same
  number (see above).
- **Player position column**, dropped for width once shooting splits went in.
- **A Games nav menu**, see the navigation model above.
- **Ranks on game sheets.** One game is not a ranking.

## Conventions

- **No em dashes in prose.** Use commas, colons, periods. Applies to generated
  report text and to code comments.
- Comments explain **why**, not what. Match the existing density: the tricky
  decisions are commented, the obvious lines are not.
- British spelling in user-facing copy ("Offence", "colour").
- Report copy addresses coaches, names teams explicitly ("Shots Ireland took",
  never "Shots we took"), and states caveats inline rather than hiding them.

## Verification

`tests/test_pipeline.py` runs fully offline against the cached pages, about 45
assertions covering the clock, both payload encodings, metrics against FIBA's
published percentages, four-factor arithmetic, shot zones, lineup validation,
small-sample guards, the empty-event shape, the theming rules, and rank and
percentile direction. Run it after any change.

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
navigation change; there are roughly 2,700 internal links across 102 pages:

```python
import pathlib, re
root = pathlib.Path("docs/example")
files = {p.name for p in root.glob("*.html")}
broken = [(p.name, h) for p in root.glob("*.html")
          for h in re.findall(r'href="([^"#?]+)"', p.read_text())
          if h.endswith(".html") and h not in files]
```

Layout regressions do not show up in the tests. After a template or CSS change,
open a page and measure rather than trusting it: chart widths and column
alignment have both silently broken before.

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
