# AIrsenal Improvement Roadmap

A prioritised todo list of improvements, based on a review of the codebase (June 2026).

## 1. Chip timing & opportunity-cost model (flagship feature)

> **Full implementation spec: [docs/chip_timing_spec.md](docs/chip_timing_spec.md)**
> (self-contained design doc with module layout, function signatures, edge cases,
> testing plan and acceptance criteria — written for delegation).

**Problem today:** Chips (wildcard, free hit, bench boost, triple captain) are only
considered if the user passes `--wildcard_week` etc. With "any week" (`0`), the
optimiser simply enumerates chip plays *within the 3-gameweek horizon*
(`construct_chip_dict` in `airsenal/scripts/fill_transfersuggestion_table.py`,
`next_week_transfers` in `airsenal/framework/optimization_utils.py`) and picks the
max expected points. Since playing a chip almost always beats not playing it within
the horizon, the recommendation is biased towards burning chips immediately — there
is no notion of the *opportunity cost* of not holding the chip for a better future
gameweek (e.g. a double gameweek or an easy fixture swing).

**Tasks:**
- [ ] Build a season-long chip value estimator that, for every remaining gameweek,
      estimates the marginal gain of each chip:
      - **Triple captain:** expected points of the best captain candidate that GW
        (doubled fixtures detected from the `Fixture` table; long-range fixture
        difficulty from the team model where player predictions don't exist yet).
      - **Bench boost:** expected bench points for a plausible squad that GW
        (again boosted by double gameweeks).
      - **Wildcard / free hit:** estimated gap between the current squad and an
        unconstrained optimal squad for that GW (free hit peaks in blank/double
        gameweeks; wildcard value grows with squad "staleness", injuries, and
        fixture swings).
- [ ] Detect double and blank gameweeks explicitly from the `Fixture` table and
      surface them in the chip value report (currently DGWs are only implicitly
      summed in predictions).
- [ ] Add an opportunity-cost decision rule: only recommend playing a chip in the
      current horizon if its expected gain exceeds the (uncertainty-discounted)
      best estimated future gain — i.e. `gain_now >= λ · max(gain_future)`, with λ
      tunable and validated by season replays (`airsenal_replay_season`).
      Account for chip deadlines (first-half chips expire at GW19 under the
      2025/26 two-of-each-chip rules), which makes holding less valuable as the
      deadline approaches: the sunk cost of burning early must be weighed against
      the risk of the chip expiring unused.
- [ ] Add a `airsenal_chip_report` CLI command that prints the expected value of
      each available chip per remaining gameweek and a recommendation
      (play now / hold, with the target gameweek).
- [ ] Wire `FPLDataFetcher.get_available_chips()` (defined in
      `framework/data_fetcher.py` but currently unused) into the optimiser so chip
      availability is auto-detected from the API instead of relying on CLI flags.
      Chip windows (two of each chip, one per half-season, first set expiring at
      GW19) are available without login from the public `bootstrap-static`
      endpoint's `chips` key (verified June 2026 — see spec §2.1).
- [ ] Integrate the chip recommendation into `airsenal_run_pipeline` so the default
      run considers chips with the opportunity-cost model instead of defaulting to
      "never play chips" (`-1`).
- [ ] Validate with replays: run past seasons with/without the chip-timing model
      and compare total points.

**Status (2026-08-06): implemented, on our fork, not merged upstream.** All four
tasks above are done (see `docs/chip_timing_spec.md` for the design;
`airsenal/framework/chip_timing.py`, `airsenal/scripts/chip_report.py`,
`--chip_strategy` on `airsenal_run_optimization`/`airsenal_run_pipeline`).
Default is `off` — replay validation doesn't yet support flipping it:

Replay comparison (off/greedy/auto, `airsenal_replay_season`, GW1-19, reduced
GA fidelity for tractability — `num_iterations=15`, `weeks_ahead=2`), combined
actual points across seasons 2324 + 2425: **greedy 2072 > off 1970 >
auto (best: λ=0.5, 2065)**. `auto` at λ=0.5 came within 0.3% of greedy and
outright beat both alternatives in one of the two seasons, but didn't clear
the "auto ≥ greedy" bar overall. A λ grid (0.3–0.8) showed a real interior
optimum around 0.5, not simply "lower is better" — λ=0.3 was the *worst*
variant tested (too eager to play), λ=0.8 the most conservative.

Two unrelated real bugs were found and fixed along the way (see git log for
`0a1bea1`, `3f31a24`, `28bce0d`): a pandas mixed-timezone crash, a silent
squad-reconstruction failure on budget-edge transfers, and a stale
2-vs-5 free-transfer banking cap.

**Update (2026-08-09) — 3rd season (2223) added.** After fixing the four
bugs above (two blank-gameweek crashes, a confusing-error-instead-of-clean-
failure quirk, and an unreliable initial-squad GA), completed a clean 2223
replay: **off 902, greedy 989, auto (λ=0.5) 897**. Combined across all
3 seasons: **greedy 3061 > auto 2962 (3.2% behind greedy) > off 2872**.
2223 was auto's *worst* relative showing of the three seasons - it didn't
even clear the "off" baseline this time (897 vs 902), and voluntarily
played only the forced GW17 World Cup wildcard, holding every other chip
all season (`chips_played: {"17": "wildcard"}` vs greedy's five: TC@2,
BB@3, FH@5, WC@6, WC@17). A 3rd season doesn't strengthen the case for
`auto` over `greedy` - if anything it weakens it. **Conclusion stands:
live default stays `off`/what the automated weekly job actually runs
(`greedy` for team 1) rather than flipping to `auto`.** See
`tools/weekly_transfer_run_team2_auto.sh` for the live A/B test now
running `auto` λ=0.5 on a second team once one exists, as a further,
non-replay check on this conclusion.

- [x] **Blank-gameweek crash in team-model fitting** — fixed 2026-08-07.
      `get_result_dict` (`airsenal/framework/bpl_interface.py`) did
      `np.array([fixture dates for this gameweek]).min()` with no guard for zero
      fixtures, crashing with `ValueError: zero-size array to reduction
      operation minimum which has no identity` on a genuine blank gameweek
      (confirmed: season 2223/2022-23, World Cup fixture disruption). Added
      `_get_reference_date()`, which falls back to the nearest surrounding
      gameweek's earliest dated fixture (searching outward both directions),
      raising a clear `ValueError` only if no dated fixtures exist anywhere
      nearby. Regression tests in `airsenal/tests/test_bpl_interface.py`.
- [x] **Missing position crashes squad optimisation** — fixed 2026-08-07.
      `SquadOpt._remove_zero_pts` (`airsenal/framework/optimization_squad.py`)
      built `position_idx` by inferring boundaries from where consecutive
      players' positions changed after filtering out zero-predicted-points
      players — if *every* player in a position (e.g. all DEF) got filtered out
      for a gameweek range, that position silently stole another position's
      index range (or dropped the last position's entry entirely), and
      `_get_mutation_bounds`/`_create_individual` crashed with `KeyError` or a
      cryptic DEAP `ValueError: empty range for randrange()`. Rewrote it to
      build `players`/`position_idx` position-by-position instead of by
      inference, and added `SquadOpt._check_positions_available()` to fail
      fast with a clear message if a *required* position ends up with zero
      eligible players. Regression tests in `test_optimization_squad.py`.

      Separately, this crash used to hang the whole tree-search worker pool:
      when it happened inside a `multiprocessing.Process` worker in
      `fill_transfersuggestion_table.py`, the crashed worker died silently,
      and every *other* worker then spun forever (`time.sleep(5)` loop)
      because their termination check (`is_finished`) compared output files
      on disk against a precomputed expected total that could never be
      reached once that node's subtree was abandoned — observed hanging
      ~10 hours with no progress. Fixed by wrapping each tree node's
      processing in a try/except (log and abandon that node's subtree
      instead of dying) and replacing the static-total check with a shared
      "outstanding work" counter (`SharedCounter`, incremented/decremented
      around every `queue.put`/node resolution) so termination is provable
      regardless of any individual node failing. Regression tests in
      `airsenal/tests/test_optimize_worker_resilience.py`.

      **Update (2026-08-09):** re-running the 2223 replay surfaced a
      second, distinct occurrence of the exact same "unguarded `.min()`
      over a possibly-empty fixture-date array" bug, in
      `prediction_utils.py`'s `process_player_data` (crashed all three of
      off/greedy/auto on the same blank gameweek, one step further into
      the pipeline than the first fix). Extracted the fallback logic from
      `bpl_interface.py` into a shared `get_reference_date_for_gameweek()`
      in `utils.py`, used by both call sites now. A third candidate
      (`bpl_interface.fixture_probabilities`, called from
      `chip_timing._team_fixture_strength`) was checked and is already
      safe - that caller wraps it in a broad `try/except: return 0.5`
      deliberately, so a blank-gameweek failure there degrades gracefully
      rather than crashing.

      With both crashes fixed, `off` completed cleanly (919 actual points,
      GW1-19, wildcard played at the GW17 World Cup hack), confirming the
      `_check_positions_available` guard and the `optimize()` worker
      resilience both work correctly under real load. But `greedy` and
      `auto` still failed - for GW7 specifically (genuinely blank), *every*
      wildcard/free-hit candidate in that gameweek's small
      (`weeks_ahead=2`) search tree hit `_check_positions_available`'s "no
      eligible GK" guard, so the whole tree yielded zero valid strategies.
      `run_optimization` then crashed with a confusing `TypeError` instead
      of its own intended clean `ValueError` - a pre-existing quirk noted
      (and deliberately left unfixed) earlier this project, now fixed:
      moved the `best_strategy is None` check in
      `fill_transfersuggestion_table.py` before the `fill_suggestion_table`
      call that was crashing on it. That alone still aborted the whole
      season's replay over one unplayable gameweek, so also made
      `replay_season()` catch that ValueError and fall back to "no
      transfer this gameweek, keep the existing squad" (matching what a
      human manager would actually do on a genuinely blank gameweek)
      instead of aborting - flagged per-gameweek via a new
      `optimization_fallback` field in the replay results for honest
      analysis later.

      **Fourth issue found, also fixed (2026-08-09):** with the above three
      fixed, re-running the replay 3 times surfaced a *different*,
      non-blank-gameweek problem: the one-time initial squad build (GW1)
      reused `num_iterations` (15, reduced for per-gameweek tractability)
      for its own GA population/generations, and failed to converge on a
      complete squad ("Squad is incomplete" from `Squad.optimize_lineup`)
      2 times out of 3 - aborting the whole season's replay on pure GA
      randomness, since the initial build only happens once per replay and
      was never the reason `num_iterations` needed to be small in the
      first place. Decoupled it: the initial squad build now always uses
      at least 100 generations/population regardless of `num_iterations`.
      Replay re-run in progress with all four fixes.

## 2. Optimisation engine

- [ ] Replace/augment the brute-force strategy tree enumeration with a smarter
      search (beam search / pruning of dominated strategies) — the tree blows up
      with `--max_transfers > 2` (flagged "EXPERIMENTAL ... very slow" in the CLI).
- [ ] Account for the 5-free-transfer rule in season replays
      (`MAX_FREE_TRANSFERS` comment in `optimization_utils.py`: "not accounted for
      in replay season").
- [ ] Tune the future-gameweek discount factor (hardcoded `14/15` in
      `get_discount_factor`) via replays; consider per-horizon tuning.
- [ ] Remove or generalise the hardcoded 2022 World Cup wildcard hack in
      `run_optimization` (`season == "2223" and gameweeks[0] == 17`).
- [ ] Consider risk/variance, not just expected points (e.g. captaincy choices
      with similar EV but different variance; rank-aware strategy near deadlines).
- [ ] Model expected price changes so transfers can be timed to gain/protect team
      value (currently sell prices are tracked but future price moves are ignored).
- [ ] Improve sub-ordering and auto-sub modelling: address the TODOs in
      `scripts/sub_probability.py` (per-position non-appearance probabilities,
      formation-aware sub order) and feed them into `DEFAULT_SUB_WEIGHTS`.

## 3. Prediction models

- [ ] Use the scraped Understat xG/xA data (`scripts/scrape_understat.py` exists
      but nothing in `framework/` consumes it) as features in the player/team
      models.
- [ ] Add set-piece/penalty-taker information to attacking point predictions.
- [ ] Improve the bonus points model (currently an empirical per-player average in
      `fit_bonus_points` with hardcoded minutes thresholds — noted in the code).
- [ ] Model minutes explicitly (e.g. a played-minutes model using rotation
      patterns, injuries, and price/news signals) instead of the "last 3 matches"
      heuristic in `get_recent_minutes_for_player`.
- [ ] Resolve the defending-points edge case noted in `prediction_utils.py:345`
      ("what about if the team concedes only after player comes off?").
- [ ] Evaluate prediction calibration each season: store predicted vs actual
      points and produce a diagnostic report.

## 4. Data & infrastructure

- [ ] Support adding newly-registered players without a full DB rebuild (NOTES.md:
      "if a new player is added to the game the whole database needs to be
      recreated").
- [ ] Scrape FIFA team ratings instead of committing the file to the repo
      (TODO in `scripts/fill_fifa_ratings_table.py`).
- [ ] Automate chip/transfer submission via the API where possible — `make_transfers.py`
      currently can't activate chips ("this must be done manually", see
      `data_fetcher.py` around line 610).
- [ ] Add retry/backoff and clearer errors for FPL API failures (login flows are a
      recurring pain point).

## 5. Docs, tests & housekeeping

- [ ] Update NOTES.md: it says bonus/save/card points are not predicted, but
      `get_bonus_points`, `get_save_points` and `get_card_points` exist; it also
      gives the old `/tmp/data.db` location (DB now lives in `AIRSENAL_HOME` /
      platform user data dir). The "Chips" section is an empty stub.
- [ ] Add tests for chip strategy enumeration with the 2025/26 two-chips rules and
      for `calc_free_transfers`/`calc_points_hit` edge cases (B/T transfer codes).
- [ ] Add an end-to-end smoke test for `airsenal_run_pipeline` with a small fixture
      DB so optimiser regressions are caught in CI.
- [ ] Document the chip CLI flags and the new chip report in the README.
