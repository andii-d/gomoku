# Gomoku Analysis Engine — Project Plan

A Gomoku equivalent of chess.com's post-game review, with live in-game move classification as well as a deeper post-game report. Built on top of Rapfi (open-source, NNUE-based Gomoku engine) rather than training a new engine from scratch.

## 1. Goals and non-goals

**Goals**
- Real-time move classification during a live game (Best / Good / Inaccuracy / Mistake / Blunder)
- A deeper post-game report with an eval graph, critical moments, and revised verdicts where the deep pass disagrees with the live pass
- A calibrated win-probability number attached to every position, not just a raw engine score
- A foundation that can support real users over time, with the calibration improving as usage grows

**Non-goals (for the MVP)**
- Training an RL agent from scratch to compete with Rapfi's strength
- Supporting every rule variant on day one (start with one, expand later)
- A human-skill-calibrated "what would a player at your level play" model (this is a stretch goal, layered on later)

## 2. System overview

Two call modes against the same underlying engine:

- **Fast mode (live)**: tight time/depth cap, used during an active game to flag an obvious blunder within a second or two of a move being played
- **Deep mode (post-game)**: no latency pressure, used to re-analyse every move after the game ends, and treated as the authoritative verdict when it disagrees with the fast pass

Both modes go through a persistent pool of Rapfi processes speaking the Piskvork protocol, rather than spinning up a fresh process per request.

A separate calibration model converts Rapfi's raw score into a win probability, which is what actually drives the Best/Good/Inaccuracy/Mistake/Blunder buckets.

## 3. Data requirements

### 3.1 Engine and reference material
- Rapfi engine binary and NNUE weight files (GPL-3, from the official `dhbloo/rapfi` and `dhbloo/rapfi-networks` repos). Used as a subprocess via the Piskvork protocol, so no licensing obligations flow into your own codebase, but the engine's own license and source notice need to stay attached to the binary.
- Piskvork protocol reference, for the exact command set (`START`, `BOARD`, `TURN`, `INFO`, etc.) and how Rapfi reports candidate moves and scores.

### 3.2 Calibration dataset (self-play phase)
This is the data that makes "70% predicted win probability wins 70% of the time" a true statement rather than a marketing line.

- **Volume**: thousands of complete games minimum. More weight needed at the extreme ends of the probability range (95%+, 5%-), since those buckets naturally get fewer samples from balanced self-play.
- **Diversity, not just volume**: full-strength engine self-play tends to stay near-balanced until a late decisive mistake, which starves the middle of the probability curve. Vary time controls and inject weaker/randomised play into some games so the dataset actually covers the 20-80% range, not just the tails.
- **Per-move logging**: for every move, log the position, Rapfi's raw score at that point, and the final game outcome from the mover's perspective.
- **Move number and rule variant**: log these alongside the score. Unrestricted-opening Gomoku is a solved game, so a "balanced-looking" score doesn't mean the same thing at move 3 as it does at move 40, and it doesn't mean the same thing under freestyle rules as under Swap2 or Renju. If you support more than one variant, you need separate calibration curves, or a model that conditions on variant.

### 3.3 Held-out validation set
- A separate batch of games, generated the same way as 3.2, not reused from training. Split by *game*, never by individual move, since positions within a game are highly correlated and a move-level split will leak information and make the calibration look better than it is.

### 3.4 Real user data (post-launch)
- Once there are real users, their games become a second and arguably better data source, since blunder patterns and outcome distributions in human play differ from engine self-play.
- Needs a data model decided up front: what gets stored per move (score, board state, timestamp), whether player rating/skill is tracked, and a retention/privacy stance, since this is real user data rather than synthetic self-play.

### 3.5 Stretch: human-skill dataset
- Only needed if pursuing the "what would a player at your level play" feature later. Requires a rated-game database bucketed by skill level, which chess has via Lichess but Gomoku does not have an obvious public equivalent for. Worth a feasibility check (existing Gomoku platforms, Gomocalc) before committing to this stretch goal.

## 4. Methodology

### 4.1 Engine wrapping
- Implement the Piskvork protocol handler: send board state, receive best move(s) and scores.
- Build a small process pool so concurrent games don't block each other and so there's no cold-start/weight-loading cost per request.
- Test time-limited search specifically: confirm Rapfi degrades gracefully under a tight time cap (still returns a usable score) rather than returning garbage when cut off early.

### 4.2 Calibration model
- **Features**: engine score at minimum. Move number and rule variant as conditioning features, given the solved-game issue above.
- **Model choice**: fit both logistic regression and isotonic regression, compare which generalises better on the held-out set. Isotonic is more flexible but can overfit with less data; logistic is more constrained but more stable.
- **Validation**:
  - Reliability diagram: bucket predictions (e.g. 0-10%, 10-20%, ...) and compare predicted probability to observed win rate in each bucket
  - A scalar summary metric, Brier score or log loss, on the held-out games
  - Sanity check at the extremes specifically (99% predicted should almost always win), since that's where sample size is thinnest and errors are most visible to users

### 4.3 Move-quality thresholds
- Once the calibration curve exists, define the Best/Good/Inaccuracy/Mistake/Blunder buckets as win-probability deltas between the move played and the engine's top choice, not raw score deltas. This mirrors how chess.com derives its classifications from Stockfish's win% model rather than raw centipawns.
- Thresholds will need tuning by eye against a sample of known-good and known-bad games before they feel right. Treat the first set of cutoffs as a draft, not a final answer.

### 4.4 Live vs post-game reconciliation
- Store both the fast-mode and deep-mode verdict per move, not just one.
- Post-game report is authoritative when the two disagree. Surfacing that disagreement explicitly ("live review flagged this as OK, deeper analysis found a mistake") is a better user experience than silently overwriting the live verdict, and is worth keeping as a visible feature rather than an internal detail.

### 4.5 Ongoing recalibration (once there are users)
- Periodically retrain the calibration curve on accumulated user games rather than leaving it fixed on the original self-play batch.
- Monitor calibration drift over time (worsening Brier score on recent games is the signal to watch).
- Recalibrate whenever the underlying Rapfi network is upgraded, since a new engine version shifts the score distribution and silently invalidates the old curve.

## 5. Stages and outcomes

**Stage 1: Engine wrapper**
- Outcome: Rapfi running behind a REST API, with fast and deep call modes, backed by a process pool that survives concurrent requests
- Done when: can submit a board state and reliably get back a move and score within the fast-mode time budget, under concurrent load

**Stage 2: Self-play data generation**
- Outcome: a logged dataset of games (varied strength/time controls) with per-move score, move number, variant, and final outcome
- Done when: dataset covers the full 0-100% probability range with reasonable density, verified visually before moving on

**Stage 3: Calibration model**
- Outcome: fitted score-to-winrate model, validated on a held-out game-level split, with a reliability diagram and Brier score reported
- Done when: the model is calibrated well enough that a stated probability is trustworthy, not just well-ranked

**Stage 4: Move classification logic**
- Outcome: Best/Good/Inaccuracy/Mistake/Blunder thresholds derived from the calibrated win probability deltas
- Done when: thresholds have been checked against a handful of known games and produce sensible, non-surprising verdicts

**Stage 5: Live review**
- Outcome: in-game move classification, fed by fast mode, shown to the player as they play
- Done when: verdicts appear fast enough not to disrupt play, under realistic concurrent usage

**Stage 6: Post-game report**
- Outcome: eval graph across the game, critical-moments summary, deep-mode verdicts that can override live ones, with disagreements surfaced rather than hidden
- Done when: a full game can be replayed end to end with an eval graph and a clear, accurate blunder list

**Stage 7 (post-launch): Recalibration loop**
- Outcome: a recurring job that retrains the calibration model on accumulated user games and tracks calibration drift over time
- Done when: there's a repeatable process for recalibrating after both user-data growth and engine version upgrades, not a one-off fit

**Stretch: Human-skill-calibrated model**
- Outcome: a secondary model predicting what a player at a given skill level would play, layered on top of the Rapfi-based classification
- Done when: feasibility of a suitable rated-game dataset has been confirmed; treat this as optional and separate from the MVP timeline

## 6. Attribution

This project depends on Rapfi (GPL-3.0), by the Rapfi developers (`dhbloo/rapfi`), used as an external process via the Piskvork protocol. Rapfi's license and source notice must be kept alongside the distributed binary.