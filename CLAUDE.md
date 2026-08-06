# CLAUDE.md

Guidance for Claude Code working in this repo. Read this before changing anything.

## What this is

Fast-turnaround basketball analytics for the **Ireland U16 national team** at
**FIBA U16 EuroBasket 2026, Division B** (81 games, 6-16 August 2026, Gevgelija
and Skopje, North Macedonia). Ireland open against the Netherlands on
**Thursday 6 August, 11:00 local time** (10:00 in Ireland).

The team had no analytics of any kind. They play on consecutive days, so the job
is to turn a finished game into a usable opponent scouting report within minutes.
Connor volunteers this; the audience is coaching staff reading on phones in a gym,
not analysts.

**Live site: https://fiba-ireland.vercel.app/**
Repo `connorleach01/fiba-ireland`, public. Vercel serves `docs/` off `main` (see
`vercel.json`, no build step). GitHub Pages serves the same directory as a
fallback and is deliberately left connected, but is NOT the URL that decides
whether a deploy landed: set `FIBA_SITE_URL` (done in the launchd plist) and
`deploy.ensure_live` confirms against Vercel.

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

## Automation

The poller runs as a launchd agent, `com.fiba.ireland.watch`, installed from the
plist in the repo root. It polls every 300s, and `RunAtLoad` plus `KeepAlive`
mean it starts on login and restarts if it dies, with a 60s throttle so a
persistent failure cannot spin.

```bash
launchctl list | grep fiba            # PID and last exit status
tail -f data/watch.log                # what it is doing
launchctl unload ~/Library/LaunchAgents/com.fiba.ireland.watch.plist   # stop
launchctl load   ~/Library/LaunchAgents/com.fiba.ireland.watch.plist   # start
```

**The Mac only needs to be awake while a result is due.** In Mountain time,
where Connor is, a match day looks like this:

| day | Mac on (Mountain) | hrs | then off |
|---|---|---|---|
| Thu 6 Aug | 4:15 am to 4:00 pm | 11.8 | 12 hrs |
| Fri 7 Aug | 4:15 am to 4:00 pm | 11.8 | 12 hrs |
| Sat 8 Aug | 4:15 am to 4:00 pm | 11.8 | 36 hrs |
| Mon 10 Aug | 4:15 am to 4:00 pm | 11.8 | 12 hrs |
| Tue 11 Aug | 4:15 am to 4:00 pm | 11.8 | 25 hrs |
| Wed 12 Aug | 5:15 pm to 7:00 pm | 1.8 | 22 hrs |
| Thu 13 Aug | 5:15 pm to 7:00 pm | 1.8 | 34 hrs |
| Sat 15 Aug | 4:45 am to 4:00 pm | 11.2 | done |

73.5 hours on out of 228, so **32% of the tournament**. The 12 and 13 August rows
come from FIBA's stub 22:00 UTC knockout times and will move once the bracket
resolves; the code reads them from the schedule each poll, so nothing needs
editing when they do. So `watch.py`
holds the sleep assertion through a `SleepBlocker` driven by
`sleep_hold_needed()`, which asserts inside a result window **and across the
45-minute gaps between them**, releasing only for the long ones, rather than wrapping the whole agent in
`caffeinate` and pinning the machine awake around the clock to buy nothing. The
plist therefore has no `caffeinate` wrapper; do not add one back.

Two things follow, and both are easy to get wrong:

- **`sleep_until` measures the wall clock, not the monotonic one.** macOS does
  not advance the monotonic clock during system sleep, so a plain
  `time.sleep(900)` started before a suspend still has most of its 900s left on
  wake, delaying the first poll of the morning. Sleeping in 30s steps and
  re-checking `datetime.now()` means a suspend-and-resume falls straight through.
- **The hold has to bridge the intra-day gaps.** Windows on a match day sit
  about 45 minutes apart and only one wake is scheduled per day, so releasing in
  one of those gaps would let the Mac sleep at 6am with nothing to wake it for
  the 6:45am window. `SLEEP_HOLD_BRIDGE_S` (100 min) covers the gap; the
  overnight 12 hours is far past it. Do not lower it below the longest intra-day
  gap.
- **Something has to wake the Mac.** A launchd agent does not wake a sleeping
  system, so this needs a one-off, run by hand because it wants sudo:

  ```bash
  sudo pmset repeat wakeorpoweron MTWRFSU 04:00:00   # 15 min before the first window
  sudo pmset repeat cancel                            # after the tournament
  pmset -g sched                                      # check what is scheduled
  ```

  It is a standing rule: it persists across lid close, sleep, reboot and
  shutdown, and needs running once, not nightly. Without it the Mac sleeps at
  16:00 and stays asleep, and the overnight games are not scraped until someone
  opens the lid. Nothing is lost, since the trigger is "VALID and not yet
  scraped", but the reports arrive hours late.
- **A lid-closed wake is a DarkWake and is not trusted here.** The schedule still
  fires, but the machine comes up with the display off and returns to sleep
  quickly unless something takes a power assertion, and the poller only takes one
  after its next poll. `sleep_until` uses a 10s step to keep that race short, but
  this has not been tested on this hardware and clamshell behaviour varies with
  power and displays. **Lid open is the supported configuration**; treat
  lid-closed operation as unverified rather than broken.

**`caffeinate -s` is inert on battery by design**, so all of the above is a no-op
unless the laptop is plugged in, and closing the lid sleeps the machine whatever
any assertion says. Plug in, lid open.

**Editing `fiba/` does not affect the running agent.** It holds the code it was
started with, so unload and load again after any change you want live. That is
the easiest thing to forget mid-tournament.

**Polling is adaptive, and `--interval` disables it.** `next_interval()` works
the cadence out from the tip times in the schedule: a game can only produce a
result between `FINISH_WINDOW_START_S` (75 min) and `FINISH_WINDOW_END_S` (3 h)
after tipping, so inside that window the
poller runs every `FAST_INTERVAL_S` (45s) and outside it every
`IDLE_INTERVAL_S` (15 min), waking early when the next window is about to open.
The plist deliberately passes no `--interval`, because that flag pins the gap and
turns all of this off.

Measured over match day one that is about 770 requests against 288 for a flat
5 minutes: more traffic overall, but concentrated in the five windows where a
result can appear, and a quarter of the old rate overnight and on rest days.

**Where the time actually goes**, measured rather than assumed:

| step | time |
|---|---|
| **FIBA flips VALID, box score not yet published** | **250 to 495s** |
| notice it (12s pending poll) | ~6s |
| schedule GET | 0.4s |
| game GET | 4.7s |
| parse, validate, store | under 0.1s |
| rebuild all 106 pages | 2.3s |
| commit and push | ~2s |
| Vercel deploy | under 30s |

So **five to nine minutes from final buzzer to live report**, and the first row
is nearly all of it. FIBA marks a game VALID several minutes before the box score
appears in the page HTML, so the game page parses to nothing in between; measured
across the four games of match day one that gap ran 250 to 495s (mean 371). It is
not ours to shorten.

Everything we control totals well under a minute. Do not chase the code path for
speed. The one lever that mattered was the retry gap: at 45s the data sat
published for an average of 25s before we looked again, so a game known
VALID-but-unparseable now polls every 12s (`PENDING_INTERVAL_S`) and that tail is
about 6s.

**Every cycle logs one line**, whether or not anything happened:

```
poll ok: 81 scheduled, 81 final, 4 new
next poll in 45s (2 game(s) due to finish)
```

That heartbeat exists because a healthy poll used to log nothing at all, which
made silence ambiguous: an operator could not tell "polling fine, nothing new"
from "wedged two hours ago". During the event, `final` counting up is the signal
that the trigger works, and **a timestamp that has stopped moving is the alarm**.
Do not remove it to quieten the log.

**The agent commits and pushes `docs/` itself.** Always `git pull --rebase`
before committing by hand or the push is rejected.

**Transient connection resets from FIBA are normal.** Over a two-day soak the
agent logged 19 first-attempt `ConnectionResetError`s that all recovered on
retry, and one cycle that exhausted all four attempts. The loop caught it and
carried on, which is the designed behaviour: a failed poll must never end the
watch, and the next one is only five minutes away. Warnings in the log are not
by themselves a problem; `poll ok` no longer appearing is.

New results fire a macOS notification through `watch.notify` (`osascript`), and a
game that fails to parse fires a separate one, so a silent failure still
surfaces. Both are best effort and can never break a run.

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
templates/     _base (all CSS + JS), _macros,
               index/scout/review/tournament/teams/schedule
docs/          the published site (Vercel source, Pages fallback)
docs/example/  U18 reference set, stamped "not live U16 data"
raw/           gzipped scraped pages, gitignored
data/fiba.db   SQLite, gitignored
```

`raw/` and `data/` are gitignored but **do not delete them casually**: `raw/`
lets any parser change be replayed offline against 162 real games without
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

**Every published time is venue-local, not Irish.** The staff reading these pages
are standing in Gevgelija, so that is the clock they are on. `report._venue()`
resolves the zone from the event's host country through `ingest.venue_timezone`,
the same mapping that converted the feed's wall times to UTC on the way in, so
pointing the tool at a different country needs no code change. Nothing renders in
Irish time any more; `IRISH_TZ` survives in `report.py` only so an operator can
reason about the offset.

The check that matters: `gameDateTime` arrives as venue-local wall time with no
offset and is stored as UTC, so rendering that UTC back in the venue zone must
return the exact string FIBA published. A test asserts this across all 162
fixtures in both events, which catches an offset applied twice, not at all, or
backwards. Croatia and North Macedonia are both UTC+2 in August, so the U18
reference set and the live U16 event agree; do not read that as proof the
mapping is right for a third country.

**A live game must never be stored as final, and three guards say so.** The
schedule's `statusCode` must be `VALID`, the schedule's `isLive` must be false,
and `validate_game` requires at least `REGULATION_PERIODS` scored periods per
team summing to that team's total. The third guard is the one that matters,
because **every other invariant passes at half time**: a live box score is
internally consistent, just incomplete, and a stored game is never revisited, so
a mid-game scrape would be wrong forever. Note that both events backfilled so far
were already over, so **`INIT` and `VALID` are the only status codes ever
observed**; what FIBA sets during a live game is still unknown, which is why the
other two guards exist. Refusal is the right failure here: the game stays out of
`scraped_game_ids`, so the next poll five minutes later simply tries again.

**Lineups are validated per player.** Derived minutes are compared to the
official box score; a game that fails is marked `lineups_ok=0` and reports
suppress its lineup sections rather than show plausible-but-wrong numbers.
Measured across 3,513 player-games in two complete events: 81.4% exact, 100%
within 5s, worst 2s, and 162/162 games inside tolerance.
`TOLERANCE_SECONDS = 10` is deliberately just above that noise floor so it trips
immediately if FIBA changes how it reports substitutions.

**A degraded schedule page must never blank good data.** Seen for real during
the pre-tournament soak: FIBA served a `/games` page where the ten opening-day
games parsed to all nulls, no teams, no tip time, no status, and the plain upsert
wrote those nulls straight over good rows. The next poll repaired it, but a game
with no `game_utc` drops out of the fixture list **and** out of the window that
decides when to poll fast and when to let the Mac sleep, so a blip at the wrong
moment could have slept through a match day. `db.upsert_schedule` now COALESCEs
every column against what is stored, so a null incoming value is a no-op, and it
logs a warning naming the affected games. Every column here only ever goes null
to value in real life, so nothing legitimate is lost.

**Live polling never reads the cache.** A game we have not ingested is either new
or previously failed, so `sync_event` fetches fresh when `only_new=True`.
Reusing the cache there could pin us to a page captured while the box score was
still being published, and no amount of retrying would get past it. A page that
fails to parse has its cache entry deleted so the next poll starts clean.

**Cache writes are atomic** (temp file + rename). The poller and a manual command
can run at once; a half-written gzip caused a real failure during development.

**Pts/att is exactly twice eFG%.** Never show both in the same table. Player
tables show eFG%, because that is what the staff already read; the zone tables
show points per attempt, because comparing a corner three to a rim finish is
exactly what it is for.

**DREB% is the exact complement of the opponent's OREB%**, so they always sum to
100. That is arithmetic, not a bug.

**The event-average row is identical on offence and defence.** Every game
contributes both of its teams, so the competition is its own opponent. Also not a
bug; the leaderboard note says so.

**The scoring categories overlap** (paint, fast break, second chance, off
turnovers, bench) and do not sum to total points: a fast-break layup is also
points in the paint, and bench points cut across all of them. Any table showing
them must say so.

**Column-group views are mutually exclusive CSS, so the stale class must come
off.** A table in a `viewtoggle` group hides everything not in the chosen group:
`table.view-adv .c-box { display: none }` and so on. Leave two `view-` classes on
the same table and both rules apply, so **every column hides and the table
renders empty**. That is exactly what the leaderboard did once it grew from three
views to four and the handler was still removing only the three original class
names. The handler now strips any class beginning `view-` rather than a fixed
list; keep it that way if you add a fifth view.

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

Nav is Games, Ireland, Teams, and a Scouting dropdown, with the **IRL U16 brand
as the home link**.

**Games is a fixture list, not a menu of games.** A dropdown listing every game
was built and removed early on, and that decision stands: games are still reached
from the team they belong to, through the game log on that team's page. What
`schedule.html` adds is the thing a dropdown could not, a day-by-day view of the
whole tournament that is complete before a ball is thrown, because FIBA publishes
all 81 fixtures when the event page opens. It fills in with scores and links to
game sheets as the event runs.

**The fixture list has to survive an unresolved bracket.** 31 of the 81 U16 games
are knockout ties with `team_a_code` null, and two of those rounds carry a stub
tip time: ten games at the identical minute, 22:00 UTC. Two games sharing a slot
is normal, since two venues run in parallel, so `build_schedule` treats any slot
holding more than four games as a placeholder and prints TBC. Both resolve
themselves as the group stage finishes, and the finished U18 event has zero
unassigned fixtures, which is the proof that they do. Nothing here is keyed to a
round or a date; do not hard-code either.

**There is no Dashboard tab either.** The dashboard is `index.html`, so it is
where staff already are when they open the site; a tab pointing at it only ever
mattered on the way back, and the brand does that job. The page itself still
builds and is still the landing page: do not confuse removing the tab with
removing the dashboard.

**Ireland's page is a scouting sheet.** `tournament.html.j2` deliberately carries
the same sections in the same order as `scout.html.j2`, down to the ranked stat
strip, so staff read their own team on exactly the terms they read an opponent.
If you add a section to one, add it to the other or explain why not.

**One game sheet per game, covering both teams**, not one per viewpoint. The
filename comes from the fixture (`{date}_{teamA}-v-{teamB}_game.html`) so every
team's log links to the same file; `game_filename()` is the only thing that
should generate it. `build_review`'s `org_id` argument only decides which team is
listed first, so Ireland leads on its own games. Sheets are built for every
finished game in the event, which is what makes the scouting game logs clickable.

For a full event that is about 80 sheets at roughly 190 KB each. The U18
reference set is 109 pages and `docs/` runs to about 20 MB. Fine for Pages and
for git, but per-page weight now matters: shot charts are the bulk of it.

## Ranks and percentiles

`analysis.TEAM_METRICS` declares every rankable team number, its group
(advanced / box / shot / scoring) and whether bigger is better.
`analysis.team_metrics()` computes them and `event_ranks()` ranks each one, ties
sharing a rank. Metrics with **no** better/worse direction carry `better: None`:
they still get a rank but never a shading tier, because taking a lot of threes or
playing fast is a style, not an achievement. The denominator counts only teams
that have played.

**No shot share is shaded**, on either side of the ball. Where a team chooses to
shoot from is a style, and the FG% and points-per-attempt columns sitting beside
it already say whether the choice is working. Shares still carry a rank, which is
the part that answers "do they take more of these than anyone else". This was
briefly done the other way, shading rim and mid-range volume because the event's
own efficiency supported it (rim 1.14 points per attempt against an all-shots
average of 0.89, mid-range 0.61, everything else between 0.73 and 0.81), and it
was reverted on Connor's call. Do not reinstate it. Points per attempt is the
column that carries the judgement, and it is shaded everywhere, which is what
gives every zone row a coloured cell.

**`TEAM_METRICS` holds more than the leaderboard shows.** Every shot zone is
declared on both sides of the ball, share, accuracy and points per attempt, so
the zone tables on a scouting page can shade every cell; the leaderboard's
Shooting view picks a readable handful of them. The zone entries are generated in a loop from
`metrics.ZONE_ORDER` with `setdefault`, so a hand-written entry above always wins
over the generated default. `zone_metric()` builds the key and
`zone_breakdown()` stamps the same slugs onto every row it emits, so a template
never spells a metric name itself. If you add a zone, both sides follow for free.

`fg3_rate` is ranked but sits in no leaderboard group: the profile strip needs a
rank for the exact number it prints, and the zone-derived `three_share` already
covers that ground on the leaderboard.

`analysis.player_percentiles()` does the same for players, against **every player
who has taken the floor**. There is no minutes threshold: a table where the last
three rows are blank reads as broken, and a deep bench player ranking 254th of
264 in minutes is a true and useful fact rather than a small-sample artefact.

The one exception is the shooting percentages. Without a volume floor a player
who went one-for-one leads the event in FG%, which is worse than showing nothing,
so `SHOOTING_RANK_GATES` requires attempts of at least `max(3, rate * games)`
before FG%, 3P%, FT%, eFG%, TS% or Pts/att is ranked. The gate is per metric and
per player, so it costs a cell, never a row, and the note under the table says a
percentage without a rank is a thin sample rather than a missing figure. Around
93% of rankable player cells carry a rank in the U18 reference set; if that drops
sharply, suspect the gate before suspecting the data.

**Teams show a rank, players show a percentile.** Twenty-two teams rank
naturally, so a team chip reads "3rd". Two hundred and sixty-four players do not:
"254th" is hard to place and reads as an error next to a button offering
percentiles. The `pct=True` argument to `rk()` switches the chip to the
percentile, 100 being the top of the field, with the plain rank on hover. Both
come off the same `_rank_values` entry, so there is one calculation, two
renderings. Note that a shooting percentile has a smaller denominator than the
pool, because the volume gate excludes players before ranking rather than after.

Both are event-wide, so `build_all` computes them **once** and threads them
through as `context`; do not call them per page.

Shading is a five-step diverging tint (blue good, red bad, unshaded middle). The
rank number always prints alongside the tint, so colour is never the only cue.
Game sheets get no ranks at all: a single game is not a ranking.

**Ranks are on by default.** `rank_toggle` renders pressed, reading "Hide ranks",
and every table it drives ships with `show-ranks` already in its class list. The
two have to move together: leave the class off and the button's first click reads
as a no-op. The button exists to turn ranks off for a plain table or a cleaner
print, not to turn them on. The profile strip has no toggle at all; `stat_ranked`
always prints the rank and tints the card.

Because of that default, the **four-factor table is sized to its content and
centred** (`table.ff`, `width: auto` with a 300px floor, 78px numeric columns,
`margin: 7px auto 0`) rather than stretched across the page. Its rank toggle is
wrapped in `.ff-toggle` so it centres with the table instead of sitting orphaned
against the left margin. Eight short rows of two numbers spread over 794px
puts a hand's width of empty paper between a factor and its value, which is the
hardest way to read a table, and a shaded cell becomes a slab of colour. Any
other two or three column table will need the same treatment.

It leaves whitespace to the right, and that is the accepted trade. The bars below
it stay full width **on purpose**: `four_factor_bars` draws into a 640px viewBox
with 8.6px labels, so squeezing it into the leftover ~470px beside the table
scales its text down to about 6.4px, and worse in print, where this report is
meant to be read. A narrow table above a full-width chart is the right shape.

**`rank_toggle` takes a CSS selector, not an id.** The two shot-zone tables sit
side by side under one heading and share one button (`table.zones`), so the
handler uses `querySelectorAll` and drives every match. Passing `#some-id` still
works and is what the single-table cases do.

`LEADERBOARD_GROUPS` drives the leaderboard's four views, so adding a column is a
one-line change there plus an entry in `TEAM_METRICS`. **Box score and Shooting
must mirror themselves**: anything shown for a team's own play has to be shown
for what it concedes, or a reader comparing the two halves is quietly comparing
different things. Both shipped asymmetric at some point and a test now asserts
the mirror holds. Advanced is exempt because its defensive metrics are not
`opp_`-prefixed (`tov_forced_pct`, `dreb_pct`) and Scoring because bench points
have no conceded counterpart in the feed.

**A conceded metric's direction follows meaning, not sign.** `opp_tov` is
turnovers forced, so higher is better; `opp_stl` is our own giveaways, so higher
is worse; `opp_pf` is fouls drawn, so higher is better. Do not flip these
mechanically. `opp_ft_pct` carries a rank but no direction on purpose: nobody
contests a free throw, so shading it would grade a team on something it cannot
affect.

## Deliberately not shown

Things that were built and then taken out. Do not reinstate them without a
reason, and if you do, say why in the commit.

- **On/off in game sheets.** A single game is far too few possessions for on/off
  to describe anything but noise.
- **Net rating in lineup tables.** Coaches found it confusing next to a raw
  plus-minus, and there is no possession-exact stint accounting behind it.
  Lineup tables show minutes, points for, points against and plus-minus.
- **TS% in page header strips and player tables.** It survives in the expanded
  player panel.
- **Pts/att in the player table.** It briefly replaced eFG%, being the same
  number on a scale coaches read directly, but eFG% is what they already know, so
  eFG% is back and Pts/att stays only in the per-zone breakdowns, where points
  per attempt is the standard way to compare a corner three to a rim finish.
- **The greyed-out Rank column in the four-factor table.** It duplicated the rank
  chip that the toggle already reveals, and it printed on every page whether the
  reader wanted ranks or not. `four_factor_table`, the game-sheet version, never
  took ranks at all and its dead `ranks` argument is gone with it.
- **Player position column**, dropped for width once shooting splits went in.
- **A Games nav menu listing individual games**, and **a Dashboard tab**, see the
  navigation model above. The Games tab that exists now is a fixture list, which
  is a different thing; it did not reinstate the dropdown.
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

`tests/test_pipeline.py` runs fully offline against the cached pages, about 87
assertions covering the clock, both payload encodings, metrics against FIBA's
published percentages, four-factor arithmetic, shot zones, refusing an unfinished
game, lineup validation, small-sample guards, the fixture list, venue-local
times, a degraded schedule page not blanking stored data, adaptive polling, the
empty-event shape, the theming rules, and rank and percentile direction. Run it
after any change.

Built and validated against **162 completed games** across two past events, both
already in `data/fiba.db`:

- `fiba-u16-eurobasket-2025-division-b` (81 games), same age group
- `fiba-u18-eurobasket-2026-division-b` (81 games), Ireland played 7

Percentages match FIBA's published figures to within 0.055pp across 324
team-games. Derived per-game scoring and efficiency averages match FIBA's own
published Ireland leaders exactly.

After changing templates, rebuild both sites and eyeball the output in a browser,
the tests check data, not layout:

```bash
.venv/bin/python -m fiba.watch --example --no-publish
.venv/bin/python -m fiba.watch --once --no-publish
```

A quick internal-link check over `docs/example/` is worth running after any
navigation change; there are roughly 2,930 internal links across 109 pages:

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

## State as of 5 August 2026, the day before tip-off

The U16 event has not started, so `docs/` holds the dashboard, the full fixture
list and an empty leaderboard, with a banner pointing at `docs/example/`. The
launchd agent is **installed and running**, has been up continuously for two days,
and publishes on its own.

The whole chain has been rehearsed end to end against a copy of the database: a
finished game was marked unplayed, and `watch.cycle()` then discovered it,
fetched it live from FIBA, parsed it, stored it and rebuilt all 106 reports with
no failures. In the same run it picked up three U18 games that had genuinely gone
final since the last scrape, so the `INIT` to `VALID` trigger has now fired on
newly-finished games rather than only on replayed ones. That is the closest thing
to a live rehearsal available before the U16 event starts.

**What is still unproven:** what FIBA sets as `statusCode` while a game is
actually in progress. Every event scraped so far was already over. The three
guards described above mean the worst case is a game being skipped and retried
five minutes later rather than a wrong report being published, but the first
genuine live trigger is Thursday 6 August. Watch `data/watch.log` around then.
