# Ireland U16 analytics: FIBA U16 EuroBasket 2026, Division B

**Live site: https://fiba-ireland.vercel.app/**

The old `connorleach01.github.io/fiba-ireland` address no longer hosts anything.
It serves a redirect so links already shared keep working, deep links included.

Turns FIBA's published game pages into scouting and self-scout reports within
minutes of a game going final, so staff have something usable before they play
the same opponent the next day. The poller publishes the site itself, so there is
nothing to send around: coaches bookmark one URL.

**Event:** 81 games, 6-16 August 2026, Gevgelija & Skopje (MKD).
Ireland open against the Netherlands, Thursday 6 August, 11:00 local time
(10:00 in Ireland, 3:00 am Mountain).

---

## Quick start

```bash
cd ~/code/fiba-ireland

.venv/bin/python -m fiba.watch --once     # one poll now, rebuild and publish
.venv/bin/python -m fiba.watch            # poll until stopped
open docs/index.html                      # the local copy of the site
```

Every poll that finds a new result rebuilds the site and pushes it, so the live
URL updates **about a minute after the final buzzer**. Add `--no-publish` to
build without pushing.

Run it unattended (survives reboots):

```bash
cp com.fiba.ireland.watch.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.fiba.ireland.watch.plist
sudo pmset repeat wakeorpoweron MTWRFSU 04:00:00   # once; wakes the Mac each morning
tail -f data/watch.log                             # one `poll ok` line per cycle
```

The poller works its own cadence out from the schedule: every 45 seconds while a
game could be publishing a result, every 15 minutes otherwise. It holds off
system sleep during those windows and releases outside them, so on a match day
the Mac is needed roughly 4:00 am to 4:00 pm Mountain and can sleep the other
twelve hours. That is 32% of the tournament rather than all of it. Leave the lid
open and the machine plugged in; closing the lid sleeps it regardless.

Sleeping never loses a game, it only delays one: the trigger is "final and not
yet scraped", so the first poll after waking catches up on everything.

Stop it with `launchctl unload ...` and `sudo pmset repeat cancel`.

Other commands:

```bash
.venv/bin/python -m fiba.watch --rebuild                 # regenerate and publish
.venv/bin/python -m fiba.watch --publish                 # push the current site
.venv/bin/python -m fiba.watch --backfill <event-slug>   # pull a past event
.venv/bin/python -m tests.test_pipeline                  # regression tests, offline
.venv/bin/python -m tests.validate_lineups <event-slug>  # lineup accuracy report
```

## The site

`docs/` is the published site, served by Vercel straight off the main branch
with no build step. GitHub Pages is still connected to the same directory as a
fallback and keeps older shared links working, but Vercel is the live site and
the one the poller verifies against. Every page carries a navigation bar (Games, Ireland, Teams, and a
dropdown of every scouting report), so any page reaches any other. The Ireland
tab and the Scouting dropdown appear as soon as those teams have played; before
the first game the bar is just Games and Teams.

Games is a fixture list, day by day, complete before a ball is thrown because
FIBA publishes all 81 fixtures when the event page opens. Scores fill in as games
finish and each finished row links to that game's full sheet. Tables sort by
clicking a column header, the leaderboard switches between Advanced, Box score,
Shooting and Scoring views, and ranks are shown by default with a button to turn
them off. All of that is a few dozen lines of plain
DOM code inlined in the page: no framework, no build step, and every page is
fully readable with JavaScript off.

All output is a self-contained HTML file. No external requests, works offline on
a phone.

The design is a **printed stats report, not a dashboard**: A4 paper geometry,
ruled sections, hairline tables, tabular figures, and no dark mode by choice, so
it reads like the thing that comes out of the printer. **Cmd-P produces a clean
PDF** you can email or hand out: short blocks are held whole, long tables flow
across pages and break only at row boundaries, and the report identity repeats at
the foot of every page. A typical game review is 3 A4 pages, a scout report 2.

| File | What it is |
|---|---|
| `index.html` | Dashboard: next fixture, recent results, links to everything |
| `schedule.html` | Every game day by day, scores as they land |
| `scout_<CODE>.html` | Opponent profile: four factors, shot profile, personnel, lineups |
| `<date>_<A>-v-<B>_game.html` | One sheet per game, covering both teams |
| `irl_tournament.html` | Ireland cumulative, same shape as a scouting report |
| `tournament_teams.html` | All 22 teams ranked, four views, both sides of the ball |
| `example/` | A worked example built from Ireland's U18 EuroBasket, linked from the dashboard until the first game is played |

## How it works

FIBA server-renders the full Genius Sports LiveStats feed into each game page,
so **one plain HTTP GET per game** yields the complete box score, play-by-play,
every substitution, and shot coordinates. No API key, no browser, no JavaScript.

```
schedule page ──> statusCode INIT → VALID  (the trigger; one cheap URL)
                          │
game page (1 GET) ──> parse ──> SQLite ──> metrics + lineups ──> HTML reports
                          │
                     raw/*.html.gz  (every page kept, so a parser fix
                                     can be replayed without refetching)
```

| Module | Responsibility |
|---|---|
| `fetch.py` | HTTP with polite spacing and backoff; atomic gzip cache |
| `parse.py` | RSC payload → structured data, with invariants that fail loudly |
| `db.py` / `ingest.py` | SQLite schema and writes |
| `lineups.py` | Substitutions → stints, validated against official minutes |
| `metrics.py` | Four factors, possessions, player advanced, shot zones |
| `analysis.py` | Query layer that assembles report inputs |
| `charts.py` | Inline SVG (shot chart, four factors, margin timeline) |
| `theming.py` | Inline flag badges; country colours, checked for CVD and contrast |
| `report.py` | Jinja2 → HTML |
| `watch.py` | The poller |
| `deploy.py` | Commits `docs/` and pushes; failures never abort a scrape |

## What was verified before the tournament

Built and validated against two completed events, 162 games in total.

- **Parser:** all 81 games of U16 Div B 2025 and all 81 of U18 Div B 2026 parse
  with the same code, including both payload encodings FIBA emits (inline
  objects, and React back-references).
- **Accuracy:** computed FG/3PT/FT percentages match FIBA's published figures to
  within 0.055 percentage points across 324 team-games, which is pure rounding.
- **Lineups:** across 3,513 player-games, 81.4% of derived minutes matched the
  official box score exactly, 100% within 5 seconds, worst case 2 seconds, and
  162 of 162 games inside tolerance.
- **Times:** every stored tip time, rendered back in the venue's timezone,
  reproduces the exact wall time FIBA published, across all 162 fixtures.
- **End to end:** a finished game was marked unplayed and the poller then
  discovered it, fetched it live, parsed it and rebuilt all 106 pages with no
  failures. In the same run it picked up three games that had genuinely gone
  final since the previous scrape.
- **Dress rehearsal:** the full report set was generated for Ireland's 7 games at
  U18 EuroBasket 2026 Division B, and is published at `example/`.

## Design decisions worth knowing

**Nothing unverified gets published.** Every parse asserts that player points sum
to the team total, that the team total matches the final score, and that the
shooting line implies the points scored. A game that fails these is refused, not
half-loaded. Lineup minutes are checked against the official box score per player,
and a game that fails is marked unreliable so reports suppress its lineup sections
rather than show numbers that look plausible and are not.

**Team colours come from the national flags, but they are checked, not assumed.**
`theming.pick_pair()` scores every matchup for contrast on paper and for
separation under protanopia, deuteranopia and tritanopia. Crucially it scores the
OKLab a/b plane separately from lightness: two greens that differ only in
lightness read as one colour on adjacent bars however large their total distance
is. When two teams collide it reaches for the opponent's *second* flag colour
before nudging the first, so Ireland against Portugal becomes green against
Portugal red rather than green against darker green. All 380 orderings in this
22-team field keep both countries' colours. Flags are inline SVG, simplified to
field colours and the major device because they render about 16px wide.

**Every team gets both halves of the picture.** Alongside the shots a team took,
each report shows the shots it allowed, with matching zone tables. Where a team
lets opponents shoot from reads its defence more directly than any single number:
a high rim share means the paint is open, a high three share means they run
shooters off the line but concede space outside.

**The team box score leads.** Each game review opens with both teams' raw
totals, the format coaches read first, before any derived metric. Scouting and
tournament pages carry the same table as per-game averages. Percentages are
always recomputed from totals rather than averaged across games.

**Player rows expand.** Clicking a player opens their shot chart and zone splits
beneath the table, scoped to the page: all games on a scouting report, that game
alone on a game review. The panels are rendered at build time rather than fetched
on click, so the page stays one self-contained file that works offline and prints
whatever is open.

**Charts do not repeat what the table already says.** Every figure has one home.
The four factors table carries the numbers and the bars beside it carry the shape;
the bars are unlabelled on purpose. Player rows show eFG% and the shot-zone
tables show points per attempt, which is the same number doubled but the right
one for comparing a corner three to a rim finish. Shot markers shrink as a chart
gets denser so a tournament-wide chart stays readable.

**Small samples are labelled, and thin ones are withheld.** Rate stats carry a
banner naming the number of games behind them. Per-100 figures are not published
below three minutes of shared court time (`analysis.MIN_RATE_SECONDS`); the raw
minutes and plus-minus still appear, because those are facts.

**Failures are isolated.** One broken report does not cost the staff the other
twenty-five. A game that fails to parse has its cache entry discarded so the next
poll starts clean, which matters if a page is scraped while the box score is still
being published.

**Times are local to the tournament.** Everything renders in the venue's own
timezone, resolved from the event's host country, because the staff reading these
pages are standing in Gevgelija. Every page says which zone it is showing.

**Ranks are shown by default, and they mean different things by scale.** Twenty
two teams rank naturally, so a team cell reads "3rd of 22". Two hundred and
sixty four players do not, so a player cell shows a percentile with 100 at the
top of the field, and the plain rank on hover. Shading is a five-step tint, blue
good, red bad, unshaded middle, with the number always printed alongside so
colour is never the only cue.

Which numbers carry a direction was decided from the data, not by taste. Across
the U18 event the rim returns 1.14 points per attempt against an all-shots
average of 0.89 and mid-range returns 0.61, while paint, corner, wing and top all
land between 0.73 and 0.81. Accuracy and points per attempt are therefore shaded
everywhere; **shot volume is ranked but never shaded**, because where a team
chooses to shoot from is a style and the columns beside it already say whether it
is working. Game sheets carry no ranks at all: one game is not a ranking.

**A live game is never stored as final**, and three separate guards say so: the
schedule's status must be final, its live flag must be false, and the parsed game
must have four scored periods that sum to each team's total. The third is the one
that matters, because every other invariant passes at half time. A live box score
is internally consistent, just incomplete, and a stored game is never revisited,
so a mid-game scrape would be wrong permanently. Refusing costs nothing: the game
is simply retried on the next poll.

**A degraded schedule page cannot erase good data.** FIBA occasionally serves a
`/games` page where some fixtures parse to all nulls. Seen once during the
pre-tournament soak, on the ten opening-day games. Schedule writes COALESCE
against what is already stored, so a null incoming value is a no-op rather than a
deletion, and the run logs a warning naming the affected games.

## Reading the leaderboard

Defensive columns describe what a team **allows**, so they do not all point the
same way: `Opp eFG%` and `Opp FTr` are lower-is-better, while `TOV% frc`
(turnovers forced) and `DREB%` are higher-is-better. `DREB%` is the exact
complement of the opponent's `OREB%`, which is why the two always sum to 100.

Because every game contributes both of its teams, the event average row is
identical on offence and defence by construction. That is a property of the
competition, not a bug.

## Known limits

- 5-7 games per team over a 40-minute game, so a most-used five may total only
  8-12 minutes. Sample sizes are shown next to every lineup for that reason.
- Early opponents may have played only one or two games when Ireland meet them.
  Previous years' U16 data is a different birth-year cohort and is not used.
- Net-per-100 uses an estimate of two possessions per minute, because
  possession-exact stint accounting is not derivable from the feed.
- The scoring categories (paint, fast break, second chance, off turnovers, bench)
  overlap and do not sum to the total, which the report states where it shows them.
- Shot coordinates are calibrated empirically (basket at feed coordinates
  (140, 28), ~17.9 units per metre). `metrics.zone_text_agreement()` cross-checks
  the geometry against FIBA's own shot descriptions and reports any drift.
