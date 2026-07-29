# Design spec: Chip timing & opportunity-cost model

**Status:** ready for implementation. This document is self-contained: it describes
the current behaviour of the codebase (with file/line references), the desired
behaviour, a proposed module layout with function signatures, edge cases, a
validation plan, and acceptance criteria. It is written so that an engineer (or
agent) with no prior knowledge of AIrsenal can implement the feature.

---

## 1. Goal

AIrsenal should decide **when** to play each FPL chip (wildcard, free hit, bench
boost, triple captain), weighing the points gained by playing it *now* against the
expected value of holding it for a better future gameweek — and against the risk of
the chip **expiring unused** at its deadline. Today the optimiser either never
plays chips (the default) or burns them greedily inside its short planning horizon.

## 2. Background: FPL chip rules (2025/26 season)

- There are **two of each chip per season**, one per half:
  - First-half chips: bench boost & triple captain usable GW1–19, wildcard &
    free hit usable GW2–19. **Unused first-half chips are lost after GW19.**
  - Second-half chips: usable GW20–38.
- Only **one chip may be active per gameweek**.
- Chip effects:
  - **Wildcard (W):** unlimited free transfers that GW; squad changes persist.
  - **Free hit (F):** unlimited transfers for one GW only; squad reverts after.
  - **Bench boost (B):** points from all 15 players (bench included) count.
  - **Triple captain (T):** captain scores 3× instead of 2×.
- Playing a wildcard or free hit does not change your saved free transfers
  (already handled in `calc_free_transfers`, `airsenal/framework/optimization_utils.py:76`).
- Chips are most valuable in **double gameweeks** (a team plays twice; bench boost
  / triple captain / free hit value roughly doubles for affected players) and
  **blank gameweeks** (few fixtures; free hit lets you field a full XI without
  wrecking your squad).

### 2.1 Chip metadata from the FPL API (verified June 2026)

The **public, no-auth** endpoint `https://fantasy.premierleague.com/api/bootstrap-static/`
has a top-level `"chips"` key listing every chip window:

```json
{
  "id": 1, "name": "wildcard", "number": 1,
  "start_event": 2, "stop_event": 19, "chip_type": "transfer",
  "overrides": {...}
}
```

API chip names map to AIrsenal names as: `wildcard → wildcard`,
`freehit → free_hit`, `bboost → bench_boost`, `3xc → triple_captain`.

Per-entry chip availability (which chips *this team* still has) requires an
authenticated call: `FPLDataFetcher.get_current_squad_data()` returns a `"chips"`
list where each item has `"name"` and `"status_for_entry"` (`"available"`,
`"played"`, `"unavailable"`, ...). The helper
`FPLDataFetcher.get_available_chips(fpl_team_id)` at
`airsenal/framework/data_fetcher.py:392` already extracts the available ones, but
**is currently never called anywhere in the codebase** — chip availability is
purely manual via CLI flags today.

## 3. Current behaviour (what must change)

### 3.1 How chips enter the optimiser today

- CLI flags `--wildcard_week`, `--free_hit_week`, `--triple_captain_week`,
  `--bench_boost_week` on `airsenal_run_optimization`
  (`airsenal/scripts/fill_transfersuggestion_table.py:746`, `main()`) and on
  `airsenal_run_pipeline` (`airsenal/scripts/airsenal_run_pipeline.py`).
  Semantics: `-1` = never play (default), `0` = "allowed any week", `N` = play in
  GW N.
- `construct_chip_dict(gameweeks, chip_gameweeks)`
  (`fill_transfersuggestion_table.py:691`) converts those flags into
  `{gw: {"chip_to_play": str|None, "chips_allowed": [str]}}` for each GW **in the
  optimisation horizon only** (typically 3 weeks).
- `next_week_transfers(strat, ..., chips=...)`
  (`airsenal/framework/optimization_utils.py:426`) expands the strategy tree: for
  each GW it adds branches `"W"`, `"F"`, `"B0".."B2"`, `"T0".."T2"` when allowed
  (each chip at most once per run, checked against `strat_dict["chips_played"]`).
- Strategies are scored as discounted expected points over the horizon
  (`get_discounted_squad_score`, `optimization_utils.py:~195`; discount
  `14/15**n_ahead` from `get_discount_factor`, `optimization_utils.py:642`) and the
  max wins (`fill_transfersuggestion_table.py`, strategy comparison in
  `run_optimization`).

### 3.2 The problem

Playing bench boost / triple captain in *some* horizon week nearly always scores
higher than not playing it, so with `0` ("any week") the optimiser recommends
burning the chip within the next 3 GWs, regardless of whether GW+10 has a double
gameweek where it would be worth twice as much. There is **no estimate of the
chip's value beyond the horizon** and therefore no opportunity cost. The
`points_gain` recorded in the `TransferSuggestion` table includes the chip's
in-horizon boost, compounding the bias.

A correct decision needs: *expected gain from playing chip c in gameweek g* for
**every remaining gameweek in the chip's window**, plus a decision rule comparing
"now" against the discounted, uncertainty-penalised best future alternative.

## 4. Proposed design

### 4.1 New module: `airsenal/framework/chip_timing.py`

All new core logic lives here. Public API:

```python
@dataclass
class ChipWindow:
    name: str          # "wildcard" | "free_hit" | "bench_boost" | "triple_captain"
    start_gw: int      # first GW the chip can be played
    end_gw: int        # last GW the chip can be played (deadline)
    available: bool    # False if already played / not owned

@dataclass
class ChipValue:
    chip: str
    gameweek: int
    expected_gain: float       # estimated points gained by playing chip that GW
    method: str                # "prediction" (in-horizon) or "proxy" (long-range)
    is_double: bool            # whether the relevant team(s) have a DGW
    notes: str = ""

@dataclass
class ChipRecommendation:
    chip: str
    play_now: bool             # play in the next gameweek?
    best_gw: int               # argmax of (discounted) expected gain
    gain_now: float
    best_future_gain: float    # uncertainty-discounted best gain after next GW
    deadline_gw: int
    values: list[ChipValue]    # full per-GW table, for reporting
```

```python
def get_chip_windows(
    fpl_team_id: int | None = None,
    season: str = CURRENT_SEASON,
    apifetcher: FPLDataFetcher = fetcher,
    use_api: bool = True,
) -> list[ChipWindow]:
    """Chip windows + availability.

    - Windows from the public bootstrap-static "chips" key (no auth needed);
      map API names (freehit/bboost/3xc) to AIrsenal names.
    - Availability from apifetcher.get_available_chips() when logged in.
    - Fallbacks (API unavailable, or replay of a past season): hardcoded
      windows (GW2-19 / GW20-38 for transfer chips; GW1-19 / GW20-38 for team
      chips; for seasons before 2526, a single window GW1/2-38 per chip with
      the extra wildcard at GW20+), and availability inferred from the
      TransferSuggestion/Transaction tables (chip_played / free_hit columns).
    """
```

```python
def get_double_blank_gameweeks(
    season: str = CURRENT_SEASON,
    dbsession: Session = session,
) -> tuple[dict[int, list[str]], dict[int, list[str]]]:
    """Detect double and blank gameweeks from the Fixture table.

    Count fixtures per (team, gameweek) over all 20 teams (Fixture has
    home_team, away_team, gameweek, season - see schema.py:293; use
    get_fixture_teams / get_fixtures_for_gameweek in framework/utils.py).
    Returns ({gw: [teams with 2+ fixtures]}, {gw: [teams with 0 fixtures]}).
    NOTE: late-season fixture rearrangements mean future GWs can change; the
    report should recompute on every run (no caching across DB updates).
    """
```

#### Per-chip value estimators

Two regimes, selected per gameweek:

- **In-horizon** (`gw` has predictions for tag, check via `check_tag_valid`,
  `optimization_utils.py:34`): use the real prediction machinery.
- **Long-range proxy** (no predictions): use the fitted team model
  (`get_fitted_team_model` / `fixture_probabilities` in
  `airsenal/framework/bpl_interface.py`) to compute fixture-difficulty-scaled
  estimates, multiplied by fixture counts from `get_double_blank_gameweeks`.
  Running the full player prediction for 38 GWs is too slow (~minutes per GW);
  the proxy must be cheap (<1s per GW).

```python
def estimate_triple_captain_value(gw, squad, tag, season, dbsession) -> ChipValue:
    """In-horizon: best candidate captain's predicted points that GW (the extra
    1x the chip adds), i.e. max over squad players of
    get_predicted_points_for_player (utils.py:1073) summed over the GW's
    fixtures. Long-range: (league-leading captain baseline, e.g. mean of the
    current top captain pick's per-fixture prediction) x (number of fixtures
    that GW for his team) x (team-model win-probability scaling). Use the
    *current* squad's best captain if predictions exist, else a generic
    premium-player baseline (document the constant; suggest ~7 pts/fixture).
    """

def estimate_bench_boost_value(gw, squad, tag, season, dbsession) -> ChipValue:
    """In-horizon: squad.get_expected_points(gw, tag, bench_boost=True) minus
    squad.get_expected_points(gw, tag) - i.e. total_points_for_subs with full
    weights (squad.py:394). Long-range: (typical bench expected points
    baseline, ~6-8 pts) scaled by bench players' fixture counts (doubles).
    """

def estimate_wildcard_value(gw, squad, tag, season, dbsession,
                            num_iterations=100) -> ChipValue:
    """In-horizon: points of an unconstrained optimal squad for [gw, horizon]
    (reuse make_new_squad, optimization_squad.py:332, with the current budget)
    minus current squad's expected points over the same GWs. This is expensive
    (GA with num_iterations) - compute only for in-horizon GWs, and cache.
    Long-range: heuristic staleness proxy: number of current-squad players
    with poor upcoming fixtures (team model) + flagged injuries/suspensions
    + blank-GW shortfalls. Keep it simple and document it; precision matters
    less here because wildcard value is dominated by squad state, which is
    only known near-term.
    """

def estimate_free_hit_value(gw, squad, tag, season, dbsession,
                            num_iterations=100) -> ChipValue:
    """Like wildcard but single-GW: optimal one-week squad minus current squad
    for that GW only. Long-range proxy: dominated by blanks/doubles - estimate
    as (number of current-squad players without a fixture that GW) x (average
    starter points, ~4 pts) + DGW upside of a rebuilt XI.
    """
```

#### Decision rule

```python
def recommend_chip_timing(
    chip_windows: list[ChipWindow],
    squad: Squad,
    tag: str,
    gameweeks: list[int],          # the optimisation horizon, e.g. next 3 GWs
    season: str = CURRENT_SEASON,
    risk_lambda: float = 0.8,      # tunable, see validation plan
    dbsession: Session = session,
) -> list[ChipRecommendation]:
    """For each available chip:

    1. Compute ChipValue for every GW from next_gw to its deadline.
    2. gain_now = max expected_gain over the in-horizon GWs.
    3. best_future_gain = max over post-horizon GWs of
       expected_gain * get_discount_factor(next_gw, gw) * proxy_penalty
       where proxy_penalty (default 0.9) reflects estimation uncertainty
       for long-range values.
    4. play_now = gain_now >= risk_lambda * best_future_gain.
       Deadline pressure is handled naturally: as the deadline nears, the
       set of future GWs shrinks, so best_future_gain falls and play_now
       flips to True before the chip can expire. Additionally force
       play_now = True if the deadline is within the current horizon and
       gain_now > 0 (never let a chip expire unused).
    5. Only one chip per GW: if two chips both say "play now", keep the one
       with the larger (gain_now - risk_lambda * best_future_gain) margin and
       defer the other to its best_gw (recompute its recommendation with the
       chosen chip's GW excluded).
    """
```

### 4.2 New CLI command: `airsenal_chip_report`

New script `airsenal/scripts/chip_report.py` with entry point added to
`[project.scripts]` in `pyproject.toml`
(`airsenal_chip_report = "airsenal.scripts.chip_report:main"`).

Arguments (mirror conventions of `fill_transfersuggestion_table.py:main`):
`--fpl_team_id`, `--season`, `--tag` (default `get_latest_prediction_tag()`),
`--weeks_ahead` (default 3), `--risk_lambda`.

Output: for each available chip, a per-GW table (GW, expected gain, method,
DGW/blank markers) and a one-line recommendation, e.g.:

```
TRIPLE CAPTAIN (window GW20-38, available)
  GW31: +7.1  (prediction)
  GW34: +13.8 (proxy, DOUBLE GAMEWEEK: Man City, Liverpool)
  ...
  RECOMMENDATION: HOLD - best estimated gameweek GW34 (+13.8 vs +7.1 now)
```

Also support `--json` for machine-readable output (used by tests).

### 4.3 Integration with the optimiser and pipeline

In `run_optimization` (`fill_transfersuggestion_table.py:417`):

1. If the user passed explicit chip weeks (`N > 0` or `-1`), behave exactly as
   today (manual override always wins).
2. New default (replace the all-`-1` default): call `recommend_chip_timing`.
   For each chip with `play_now=True` and `best_gw` inside `gameweeks`, set
   `chip_gameweeks[chip] = best_gw` (i.e. "definitely play that week") and let
   the existing tree search find the best transfers around it. Chips with
   `play_now=False` stay `-1`. This keeps the tree small (no `0` wildcards in
   the enumeration) *and* fixes the burn-early bias.
3. Print the chip report summary as part of the optimisation output, and include
   it in the Discord webhook payload if configured (see existing webhook code in
   `fill_transfersuggestion_table.py:~680`).
4. Add `--chip_strategy {auto,manual,off}` flag (default `auto` = behaviour above;
   `off` = current default of never playing chips; `manual` = require explicit
   week flags). Add the same flag to `airsenal_run_pipeline`
   (`airsenal/scripts/airsenal_run_pipeline.py`), which currently forwards the
   four `*_week` arguments via `setup_chips` (line 287).

### 4.4 Validation: season replays

`airsenal_replay_season` (`airsenal/scripts/replay_season.py:52`) already
re-simulates a past season GW-by-GW (it generates predictions per GW with
`make_predictedscore_table` and calls `run_optimization(..., is_replay=True)`).

- Add a `--chip_strategy` passthrough to `replay_season` →
  `run_optimization`.
- Run at least two past seasons (data for the last 3 seasons ships in
  `airsenal/data`) under three configurations: `off` (baseline), greedy
  (`0`-style "any week", today's behaviour), and `auto` (this feature). Compare
  total season points; record results in the replay JSON output
  (`replay_results` dict).
- Use these replays to tune `risk_lambda` and `proxy_penalty` (coarse grid,
  e.g. λ ∈ {0.6, 0.7, 0.8, 0.9, 1.0}).
- Caveat to document: replays of pre-2526 seasons should use single-chip rules
  (one of each chip, wildcard refresh at GW20; `get_chip_windows` season
  fallback handles this).

## 5. Edge cases & constraints

- **One chip per GW** (enforce in `recommend_chip_timing`; the existing tree
  search already enforces it within a strategy via `chips_played`).
- **Free hit squad reversion**: transactions made on free hit are flagged
  (`Transaction.free_hit`, `schema.py:397`) and excluded when rebuilding the
  squad (`get_squad_from_transactions`, `optimization_utils.py:136`). The chip
  report does not need to change this, but wildcard-value estimation must use
  the *non-free-hit* squad.
- **No predictions beyond horizon**: never call `get_predicted_points_for_player`
  for GWs without predictions for the tag — guard with `check_tag_valid` or
  catch missing `PlayerPrediction` rows.
- **Fixture data gaps**: `Fixture.gameweek` is nullable (unscheduled fixtures,
  `schema.py:300`); treat fixtures with `gameweek=None` as unknown, exclude from
  DGW/blank detection, and note this in the report (a team with an unscheduled
  fixture will later create a DGW).
- **API failures / not logged in**: chip availability falls back to DB inference;
  the report must still run (warn, as `get_starting_squad` does with
  `warnings.warn`, `optimization_utils.py:126`).
- **Season start**: if the team doesn't exist yet (`get_entry_start_gameweek`
  path in `run_optimization:453`), skip chip recommendation entirely.
- **2223 World Cup hack** (`run_optimization:519`): leave in place; it sets
  `chip_to_play` explicitly, which counts as a manual override.

## 6. Testing plan

Tests live in `airsenal/tests/` (pytest; see `test_optimization.py` for existing
chip-strategy tests; dummy players and helpers in `test_resources.py`; a
pre-built historic test database at `airsenal/tests/testdata/testdata_1718_1819.db`).

- `test_chip_timing.py`:
  - `get_chip_windows`: parse a saved copy of the bootstrap-static `chips` JSON
    (commit a small fixture file under `airsenal/tests/testdata/`); name mapping;
    pre-2526 fallback windows; availability inference from a populated test DB.
  - `get_double_blank_gameweeks`: synthetic Fixture rows → correct DGW/blank
    detection, including `gameweek=None` exclusion.
  - Estimators: with the test DB's predictions, triple-captain value equals the
    top player's predicted points; bench-boost value equals
    `get_expected_points(bench_boost=True) - get_expected_points()`.
  - Decision rule: (a) big future DGW ⇒ HOLD; (b) deadline inside horizon ⇒
    PLAY; (c) two chips wanting the same GW ⇒ only one recommended; (d) λ=0 ⇒
    always play now, λ→∞ ⇒ only deadline-forced plays.
- Integration: `run_optimization` with `--chip_strategy off` reproduces current
  results on the test DB; `auto` with a manual override flag set respects the
  override.
- Style/CI: ruff (line length 88), pre-commit, type hints in the new modern
  style (`int | None`, lowercase generics) consistent with the repo; CI runs
  Python 3.10 and 3.14 (`.github/workflows/main.yml`).

## 7. Acceptance criteria

1. `airsenal_chip_report` runs against a freshly updated DB without login and
   prints a per-GW value table + recommendation for all four chips, in <2 min.
2. With a synthetic DGW placed 8 GWs ahead, the recommendation for bench boost /
   triple captain is HOLD with `best_gw` at the DGW; moving the DGW inside the
   horizon flips it to PLAY.
3. With the deadline GW inside the horizon and positive gain, recommendation is
   PLAY (chips never silently expire).
4. `airsenal_run_optimization` default behaviour changes only when
   `--chip_strategy auto` is active and recommends a chip; `off` is bit-identical
   to today's output.
5. Replay of at least one past season shows `auto` ≥ greedy in total points
   (and report the comparison in the PR description).
6. All new code covered by the tests in §6; CI green.

## 8. Implementation order (suggested PRs)

1. **PR 1:** `chip_timing.py` dataclasses + `get_chip_windows` +
   `get_double_blank_gameweeks` + tests. No behaviour change.
2. **PR 2:** estimators (in-horizon first, then proxies) + `airsenal_chip_report`
   CLI + tests.
3. **PR 3:** `recommend_chip_timing` decision rule + integration into
   `run_optimization` / pipeline behind `--chip_strategy` (default `off`
   initially) + tests.
4. **PR 4:** replay validation, λ tuning, flip default to `auto`, README/NOTES
   documentation.

## Appendix A: codebase orientation for the implementer

- **DB**: SQLite via SQLAlchemy 2.0 style (`select()` not `query()`), schema in
  `airsenal/framework/schema.py`, session helpers in `framework/utils.py`
  (`session`) and `schema.py` (`session_scope`). DB lives in `AIRSENAL_HOME`
  (platform user-data dir; `framework/env.py`).
- **Key globals**: `CURRENT_SEASON`, `NEXT_GAMEWEEK`, `fetcher` (a module-level
  `FPLDataFetcher`) in `framework/utils.py`.
- **Predictions**: stored per (player, fixture, tag) in `PlayerPrediction`;
  produced by `airsenal_run_prediction`
  (`scripts/fill_predictedscore_table.py`); latest tag via
  `get_latest_prediction_tag()` (`utils.py:1707`). Gameweek totals sum a
  player's fixtures in that GW (`get_predicted_points_for_player`,
  `utils.py:1073`).
- **Squad class** (`framework/squad.py`): `get_expected_points(gw, tag,
  bench_boost=False, triple_captain=False)` optimises lineup+captain then sums;
  bench via `total_points_for_subs`; budget/sell prices via
  `get_sell_price_for_player`.
- **Squad rebuild optimiser**: `make_new_squad` (`framework/optimization_squad.py:332`),
  DEAP genetic algorithm, used today for wildcard/free-hit/new-season squads.
- **Team model**: `framework/bpl_interface.py` wraps the `bpl` package;
  `get_fitted_team_model(season, gameweek, dbsession)` then
  `fixture_probabilities(...)` for win/draw/loss & goal probabilities for any
  future fixture — this is the long-range difficulty source.
- **Strategy tree**: `count_expected_outputs` / `next_week_transfers` /
  `get_discounted_squad_score` in `framework/optimization_utils.py`; chip codes
  in strategies are `"W"`, `"F"`, `"B<n>"`, `"T<n>"`.
- **Conventions**: see `CodingConventions.md` and `CONTRIBUTING.md`; ruff with
  line length 88; tests use the historic SQLite test DB in
  `airsenal/tests/testdata/` plus dummy-player helpers in
  `airsenal/tests/test_resources.py`.
