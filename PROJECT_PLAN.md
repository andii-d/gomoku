# Gomoku Analysis Engine — Project Plan

**Current phase: private research.** No frontend, no live service, no public repo yet. The goal right now is a local pipeline that takes a finished game, analyses every move against Rapfi, and shows where each player played well or badly, including a grid rendering of the position with the best available move marked. Everything web/service-related is deliberately paused, see Section 8.

## 1. Goals and non-goals (current phase)

**Goals**
- Post-game analysis only: feed in a completed game, get back a per-move classification (Best / Good / Inaccuracy / Mistake / Blunder)
- A calibrated win-probability behind that classification, not a raw engine score
- A board-grid visualisation per move: the move actually played, and the engine's best alternative if different, rendered statically (matplotlib), no web UI
- A self-play dataset and a fitted, validated calibration model, usable as standalone research artifacts

**Non-goals (paused, see Section 8)**
- Live/in-game analysis
- Any served API, frontend, or public deployment
- Multi-user concurrency of any kind
- RL/self-play-trained agent, human-skill-calibrated model

## 2. Environment setup

**Python**: 3.11 or later.

```bash
mkdir gomoku-analysis && cd gomoku-analysis
python3 -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

Use the `requirements.txt` and `.gitignore` provided alongside this plan.

**Rapfi engine**:
1. Get the compiled binary from the [dhbloo/rapfi releases page](https://github.com/dhbloo/rapfi/releases), or build from source with CMake (Clang recommended for speed) if a release isn't available for your OS/architecture.
2. Download the network weights from [dhbloo/rapfi-networks](https://github.com/dhbloo/rapfi-networks) (CC0 licensed) along with `config.toml`.
3. Place the binary, `config.toml`, and weight files together in `engine/rapfi_bin/` (see structure below). Rapfi auto-discovers them from the executable's directory.
4. **Verify manually before writing any code.** Run the binary directly in a terminal and hand-type Piskvork protocol commands (see Section 4) to confirm it responds sensibly. Do not skip this, debugging a subprocess wrapper against a mystery binary is much harder than debugging the binary alone first.

## 3. Project structure

```
gomoku-analysis/
├── engine/
│   ├── rapfi_bin/            # binary, config.toml, weights (gitignored)
│   └── wrapper.py            # subprocess wrapper, Piskvork protocol
├── selfplay/
│   └── match_driver.py       # generates self-play games via wrapper.py
├── data/
│   ├── raw/                  # per-game logs from self-play, JSON or CSV
│   ├── renjunet/             # private RenjuNet download (gitignored, offline use only)
│   └── processed/            # consolidated dataset, Parquet or SQLite
├── calibration/
│   ├── train_calibration.py  # fits logistic/isotonic models
│   ├── models/                # saved fitted models (joblib)
│   └── plots/                 # reliability diagrams, saved as PNGs
├── analysis/
│   ├── classify.py           # win-probability delta -> move classification
│   └── board_viz.py          # matplotlib grid rendering per move
├── notebooks/
│   └── research.ipynb        # exploration, dataset checks, model iteration
├── tests/
│   └── test_wrapper.py       # sanity tests against the engine
├── requirements.txt
├── .gitignore
├── README.md
└── PROJECT_PLAN.md
```

Storage choice at this scale: **SQLite or Parquet, not Postgres.** This is single-user, single-machine research, a running database server isn't earning its place yet. `pandas` reads both natively. Migrate later if this goes public.

## 4. Piskvork protocol quick reference

Rapfi speaks a simple text protocol over stdin/stdout. Core commands worth knowing before writing the wrapper:

- `START <size>` — initialise a board of given size (e.g. `START 15`)
- `BOARD` — followed by a list of moves (`x,y,player`), one per line, terminated by `DONE`, to set up a position
- `TURN <x>,<y>` — tell the engine the opponent just played here, expect a move back
- `BEGIN` — ask the engine to play first
- `INFO <key> <value>` — configure engine parameters (e.g. time per move)
- `END` — terminate the engine process cleanly

The engine responds with a move (`x,y`) and, depending on config, `MESSAGE` lines carrying evaluation/score info. Confirm the exact score-reporting format for your build during the manual terminal test in Section 2, since output verbosity is configurable.

## 5. Data

### 5.1 Self-play dataset (primary)
- Generated via `selfplay/match_driver.py`: two Rapfi instances playing each other, varied time controls, some weaker/randomised play injected so the outcome distribution isn't just "balanced until one late mistake."
- Log per move: board state, engine score, move number, final game outcome (from the mover's perspective).
- **Start small.** Run the full pipeline (wrapper → match driver → data → calibration → visualisation) end to end on 20-50 games first, before generating thousands. Cheap to catch bugs early, expensive to catch them after a long generation run.
- Self-play games are independent of each other, so once the pipeline is trusted, parallelise generation with `concurrent.futures.ProcessPoolExecutor` (one Rapfi process pair per worker) rather than running games sequentially.
- Record the Rapfi binary version/commit and NNUE network version alongside each batch of games. This matters later: a different engine version shifts the score distribution and silently invalidates an old calibration fit.

### 5.2 RenjuNet (private validation only)
- Downloaded from [renju.net/game/](https://www.renju.net/game/), XML format (`renjunet_v10_yyyymmdd.rif`).
- **Offline, private use only.** Their terms restrict use to non-commercial offline databases and explicitly exclude any online system. Keep this in `data/renjunet/`, gitignored, used only to sanity-check the calibration model against real human outcomes on your own machine. Not used to train anything that will ever be shipped or shared without RenjuNet's explicit permission first.
- Renju rules differ from freestyle Gomoku (forbidden moves for Black, opening restrictions), so filter by ruleset before comparing against freestyle self-play data.

## 6. Methodology

### 6.1 Engine wrapper (`engine/wrapper.py`)
- Synchronous subprocess wrapper, no async, no pool. One game analysed at a time is fine for research use.
- One function: given a board state (list of moves) and a time/depth budget, return the engine's top move(s) and score(s).
- Since this is post-game only, there's no fast/live mode distinction, everything runs at a generous, consistent time budget.

### 6.2 Calibration model (`calibration/train_calibration.py`)
- Load the consolidated dataset with `pandas`.
- Split **by game**, not by move, before anything else (positions within a game are correlated; a move-level split leaks information).
- Fit `sklearn.linear_model.LogisticRegression` and `sklearn.isotonic.IsotonicRegression`, compare on the held-out split.
- Validate with `sklearn.calibration.calibration_curve` (reliability diagram) and a scalar metric (Brier score or log loss). Check the extremes (near-99% predictions) specifically, since that's where sample size is thinnest.
- Save the fitted model with `joblib` to `calibration/models/`.

### 6.3 Move classification (`analysis/classify.py`)
- Convert the calibrated win probability of the move played vs. the engine's top choice into a delta.
- Bucket into Best / Good / Inaccuracy / Mistake / Blunder. Treat the first thresholds as a draft, tune by eye against a handful of known games.

### 6.4 Board visualisation (`analysis/board_viz.py`)
- `matplotlib`, one function that takes a board state plus the move played and the engine's top alternative, and renders a 15x15 grid: stones as filled circles, the move played coloured by its classification, the engine's suggested alternative marked separately (e.g. a star or dashed outline) if it differs from what was played.
- Useful both as a static PNG per critical move and as a small-multiples figure across a full game. Live entirely in `notebooks/research.ipynb` for now, no need for a standalone script-per-image workflow yet.

## 7. Build order

1. **Environment + Rapfi verified manually** (Section 2) — confirm the binary responds to hand-typed Piskvork commands before writing any code
2. **`engine/wrapper.py`** — subprocess wrapper, tested with a throwaway script sending one board state and printing the response
3. **`selfplay/match_driver.py`** — self-play generation, run on 20-50 games first, logged to `data/raw/`
4. **RenjuNet download + parse** — pulled into `data/renjunet/`, parsed with `lxml` into the same schema as self-play data, for later validation only
5. **Consolidate to `data/processed/`** — pandas, clean schema (board state, score, move number, outcome, source, engine version)
6. **`calibration/train_calibration.py`** — fit, validate, save the model, review reliability diagrams before trusting it
7. **`analysis/classify.py`** — thresholds from the calibrated model, sanity-checked against known games
8. **`analysis/board_viz.py`** — grid rendering, tie together in `notebooks/research.ipynb` to review full games end to end
9. **Scale up self-play generation** (parallelised) once the whole pipeline above is trusted on the small batch

## 8. Later (paused, not being built now)

Kept here so the earlier planning isn't lost, revisit once the research phase produces a model worth building a product around.

- Live/in-game analysis with fast vs. deep call modes
- FastAPI service, process pooling, websockets
- React/Next.js frontend, Recharts eval graph, board component
- PostgreSQL, Redis, `arq` job queue
- Ongoing recalibration loop against real user data
- Human-skill-calibrated (Maia-style) model
- RenjuNet permission email, if the project later wants to ship anything trained on their data
- Public repo, Docker packaging for contributors

## 9. Attribution

This project depends on Rapfi (GPL-3.0), by the Rapfi developers (`dhbloo/rapfi`), used as an external process via the Piskvork protocol. Rapfi's license and source notice must be kept alongside the distributed binary. RenjuNet data (renju.net) is used privately and offline only, per their terms.
