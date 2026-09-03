# Audit fix pass — P0/P1 bugs closed

Applied on top of the previously-audited codebase (which already fixed
BUG #2 queue-crash/requeue, BUG #6 `/game`-crash-before-lobby-message, and
BUG #16 the 5+5 composition guard). This pass closes the remaining P0/P1
items from the full audit. All 112 tests pass (105 original + 7 new
regression tests).

## Fixed

- **BUG #1 (CRITICAL) — negative VC on `/edit_result`.** The VC revert now
  clamps at `MAX(0, vc_balance - amount)` instead of a bare subtraction, so
  a player who already spent their match reward can't drive their balance
  negative when a result is edited. Added migration `008_vc_nonnegative.sql`
  with a DB-level trigger that hard-rejects any negative `vc_balance`
  (defense in depth, independent of the app-level fix).
- **BUG #3 (HIGH) — join/leave ignored game status.** `add_game_player_if_space`
  and `remove_game_player` now both check `games.status = 'WAITING'` inside
  the same atomic `INSERT/DELETE ... WHERE` as the rest of their logic, so a
  join/leave can no longer race a WAITING→DRAFT transition and desync the
  roster from `game_players`. `remove_game_player` now returns `bool`; the
  "Покинуть" button reports "too late" instead of silently no-opping.
- **BUG #4 (HIGH) — duplicate voice channels on repeated `_finalize()`.**
  `_setup_voice_channels` now checks for already-persisted voice channel IDs
  first and reuses those real Discord channels instead of always creating a
  fresh pair — closes the orphan-channel path when `_finalize` runs twice
  (auto-pick racing a manual pick, or a restart recovering an already-8/8
  DRAFT).
- **BUG #5 (HIGH) — DRAFT games stuck without captains/draft message.**
  `set_captains` is now persisted immediately after the WAITING→DRAFT CAS,
  before any Discord API call, so every DRAFT game in the DB always has
  captains. `_recover_draft` can now repost a missing draft message from
  those persisted captains instead of giving up.
- **BUG #7 (HIGH) — no `defer()` before slow operations.** The DM confirm
  buttons, `/confirm_result`, and `/edit_result` now `defer()` immediately
  after the admin check and reply via `followup`, so a slow multi-DM/
  multi-row transaction can no longer produce a Discord "interaction
  failed" after the DB has already committed.
- **BUG #8 (MEDIUM) — LOSS spins could visually look like a win.** The
  slot symbol generator now guarantees three genuinely distinct symbols on
  a LOSS outcome (previously only guarded against a triple, so a LOSS could
  render two matching symbols). Note: the `/casino` UI text already matched
  the real payout table in this codebase, so that half of the original
  audit finding was already resolved.
- **BUG #9 (MEDIUM-HIGH) — failed auto-pick could freeze DRAFT forever.**
  `_auto_pick_on_timeout` now retries against the remaining local pool, then
  re-syncs from the DB once, and — critically — always restarts the pick
  timer instead of returning silently when an assign race is lost.
- **BUG #10 (MEDIUM) — no rollback on exceptions in simple write methods.**
  `_serialized_write` now wraps every decorated method in a rollback-on-
  exception handler, so a mid-sequence failure in methods like
  `set_captains`/`set_lobby_message`/`transition_game_status` can't leave a
  dangling open transaction on the shared connection.
- **BUG #12 (MEDIUM) — lost update in `/admin_elo add|remove`.** Added
  `admin_adjust_elo`, an atomic read-modify-write under the write lock;
  `/admin_elo add` and `/admin_elo remove` now use it instead of computing
  the new absolute value from an unlocked read in the cog.
- **BUG #13 (MEDIUM) — queue and lobby weren't mutually exclusive.**
  `join_queue` now atomically refuses a player who is in an active game for
  that guild; `add_game_player_if_space` now atomically refuses a player who
  is currently queued. Both checks are folded into the existing single
  `INSERT ... WHERE` statement.
- **BUG #14 (MEDIUM) — `/edit_result` deleted the original ledger rows.**
  Original `MATCH_WIN`/`MATCH_LOSS` transactions are now renamed to
  `*_REVERSED` instead of being deleted, preserving audit history while
  still freeing the unique index for the re-applied rows.
- **BUG #17 (LOW-MEDIUM) — protection decrement didn't check rowcount.**
  Both `finalize_game_result_atomic` and `edit_game_result_atomic` now
  check the `UPDATE ... elo_protection - 1` rowcount and fall back to an
  unprotected loss if the CAS'd decrement didn't actually hit a row.

## Final polish pass (all remaining fixable items)

- **REFUND ledger accuracy** — `edit_game_result_atomic` now records the
  *actual* clawed amount (`min(original_credit, balance_before)`), not the
  original credit, when the player already spent part of the match reward.
- **Wins/losses/games_played floor** — both the atomic edit path and the
  legacy `revert_game_result` use `MAX(0, …)` so stats never go negative.
- **`set_captains` CAS** — only applies while `status IN ('WAITING','DRAFT')`;
  returns `bool`. Call sites in game/queue abort cleanly on `False`.
- **BUG #15 closed** — `has_later_completed_game()` + hard refuse in
  `/edit_result` when any participant already has a later COMPLETED match.
  Full elo_history replay remains out of scope; blocking is the safe choice.
- **BUG #18 closed** — casino per-user lock dict prunes idle entries after
  every spin so it cannot grow unbounded across long uptimes.

## Intentionally out of scope

- BUG #11 (dirty reads on the single shared connection) — would need a
  second read-only connection or widening the write lock to all reads;
  acceptable at current single-guild scale.
- Multi-guild config storage — design change, not a bug.
- Full elo_history replay on edit of non-latest games — superseded by the
  hard guard above.

## New regression tests

- `tests/test_economy.py`: `test_edit_result_does_not_go_negative_after_spend`,
  `test_admin_adjust_elo_concurrent_adds_commute`,
  `test_casino_loss_reels_never_look_like_a_win`,
  `test_edit_result_refund_uses_actual_clawed_amount`,
  `test_has_later_completed_game_blocks_stale_edit`,
  `test_set_captains_rejected_after_playing`
- `tests/test_game.py`: `test_add_player_rejected_after_draft_transition`,
  `test_remove_player_rejected_after_draft_transition`,
  `test_remove_player_allowed_while_waiting`,
  `test_join_queue_blocks_active_game_player_atomically`,
  `test_add_game_player_blocks_queued_player_atomically`
- `tests/test_migrations.py`: updated hardcoded migration-count assertions
  for the new `008_vc_nonnegative.sql` migration.

---

# Production hardening pass (final)

Applied on top of the previous audit-fix codebase. Goal: close the four
explicit remaining correctness items and re-audit concurrency / economy /
locks without rewriting architecture. **129 tests pass** (115 prior + 14 new).

## Fixed in this pass

### 1. `cogs/shop.py` — `_locks` leak (and race-safe cleanup)

**Problem:** `Shop._locks` created a per-user `asyncio.Lock` on first purchase
and never removed it. Under long uptime every unique buyer left a permanent
entry. A naive `del self._locks[user_id]` after release is also unsafe: a
waiter can hold a reference to a lock that was removed from the dict while a
new coroutine creates a *second* lock for the same user → concurrent purchases.

**Fix:** Refcounted lock entries under a meta-lock (`_locks_guard`):

* Enter `_user_lock` → increment refcount (create if missing).
* Acquire the per-user lock, run the purchase.
* On exit → decrement; delete the dict entry only when refcount hits 0.

Holders *and* waiters are counted, so cleanup cannot delete a lock another
coroutine is about to use. Same pattern applied to `cogs/casino.py` (replacing
the previous “prune if not locked()” which had the same TOCTOU race).

### 2. `_result_lines` — 🛡️ semantics

**Problem:** `utils/helpers._result_lines` showed 🛡️ when
`user.elo_protection > 0` (current inventory), not when protection actually
fired in *this* match. A player who bought protection after a loss, or who
still had charges left after a win, would get a misleading shield on the
public result embed.

**Fix:**

* `get_game_result_details` now selects `game_players.protection_used` and
  returns `tuple[User, int, bool]`.
* `_result_lines` / `build_result_announcement_embed` use that flag only.
* Inventory stock no longer affects the result announcement.

### 3. Removed `__import__("asyncio").Lock` hacks

Replaced with normal `import asyncio` + `asyncio.Lock()` in:

* `cogs/shop.py`
* `cogs/casino.py`
* `cogs/queue.py`

Project-wide search confirms no remaining `__import__("asyncio")` usages.

### 4. `balance_teams` no longer hard-coded to 10

**Problem:** `services.matchmaking.balance_teams` required `len(players) == 10`
and always split into teams of 5.

**Fix:** Size comes from `config.PLAYERS_PER_GAME`. Team size is `N // 2`.
Odd `N` (unequal teams impossible) raises `ValueError`. Default remains 10
→ 5v5; algorithm works for any even configured size (tested with 6 → 3v3).

## Additional audit notes (this pass)

### Concurrency / economy

* All balance-changing paths (`credit_vc`, `debit_vc`, `purchase_elo_protection`,
  `play_casino_slots`, `finalize_game_result_atomic`, `edit_game_result_atomic`,
  `admin_adjust_elo`) remain under `@_serialized_write` + atomic SQL.
* Shop/casino per-user locks prevent accidental double-click double-spend of
  *intent*; DB constraints still prevent negative VC.
* Casino interaction idempotency (migration 007) still in place.
* Match VC unique index still blocks duplicate MATCH_WIN/MATCH_LOSS rewards.

### Database / BUG #11

* BUG #11 (possible dirty reads on the single shared aiosqlite connection)
  **re-evaluated and still intentionally deferred.** Fixing it properly needs
  either a second read-only connection or holding the write lock across all
  reads — both are architectural changes with latency trade-offs. At current
  single-guild scale and with writers already serialized, residual risk is
  accepted. Documented again below under “Intentionally out of scope”.

### Discord interactions

* Existing `defer()` on slow admin/confirm paths retained.
* Shop/casino still respond ephemerally inside the per-user lock window.
* No new double-response paths introduced.

### Tooling

* Added `pyproject.toml` with project metadata, pytest config, and a
  pragmatic Ruff rule set (E/F/I/UP/B/ASYNC; line-length ignored to avoid
  mass churn). `requirements.txt` left as the install source of truth.
* `mypy` / strict typing not enforced: large legacy surface would produce
  noise without proportional safety gains; types already present on new code.

## New regression tests (`tests/test_production_hardening.py`)

* `test_shop_lock_cleaned_after_use`
* `test_shop_lock_no_leak_after_many_users`
* `test_shop_lock_serializes_concurrent_holders`
* `test_shop_lock_refcount_under_contention`
* `test_casino_lock_cleaned_after_use`
* `test_result_lines_no_shield_when_protection_not_used`
* `test_result_lines_shield_when_protection_used`
* `test_result_announcement_embed_uses_protection_used_flag`
* `test_get_game_result_details_exposes_protection_used` (DB end-to-end)
* `test_balance_teams_default_10`
* `test_balance_teams_rejects_wrong_count`
* `test_balance_teams_respects_config_size` (PLAYERS_PER_GAME=6)
* `test_balance_teams_rejects_odd_configured_size`
* `test_balance_teams_minimizes_elo_diff`

## Intentionally out of scope (unchanged)

* BUG #11 — dirty reads on shared connection (see above).
* Multi-guild config storage.
* Full elo_history replay on edit of non-latest games (blocked by guard).
* Mass style-only refactors / strict mypy over legacy modules.
