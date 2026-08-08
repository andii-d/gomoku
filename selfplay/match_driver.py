"""
Self-play data generation. Runs full games with a single Rapfi engine
instance playing both sides, logging per-move eval scores and the
eventual outcome, for use in Phase 3 (calibration).

See PROJECT_PLAN.md Section 5.1 / 6.1.

Run from the repo root:
    python selfplay/match_driver.py
"""

import json
import random
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# Make `engine` importable regardless of how this script is invoked.
sys.path.insert(0, str(Path(__file__).parent.parent))
from engine.wrapper import RapfiEngine, default_engine_binary  # noqa: E402


BOARD_SIZE = 15
WIN_LENGTH = 5
DIRECTIONS = [(1, 0), (0, 1), (1, 1), (1, -1)]

# Vary time budget per game for outcome-distribution diversity (Section 5.1).
# A single game always uses one profile throughout; per-move variation and
# random-move injection are natural v2 additions, not implemented yet.
STRENGTH_PROFILES = {
    "strong": 5000,
    "medium": 1500,
    "weak": 300,
}


@dataclass
class MoveRecord:
    move_number: int
    mover: int  # 1 or 2
    x: int
    y: int
    eval_score: Optional[int]  # mover's own evaluation before playing this move
    depth: Optional[int]


@dataclass
class GameRecord:
    game_id: str
    board_size: int
    time_budget_ms: int
    moves: list[MoveRecord] = field(default_factory=list)
    winner: Optional[int] = None  # 1, 2, or None for draw
    total_moves: int = 0


class Board:
    """Minimal board tracker, just enough for win/draw detection."""

    def __init__(self, size: int = BOARD_SIZE):
        self.size = size
        self.grid: dict[tuple[int, int], int] = {}

    def place(self, x: int, y: int, player: int) -> None:
        self.grid[(x, y)] = player

    def is_full(self) -> bool:
        return len(self.grid) >= self.size * self.size

    def check_win(self, x: int, y: int, player: int) -> bool:
        """
        Checks whether the stone just placed at (x, y) completes 5+ in a row.
        NOTE: freestyle rules only (overlines count as a win). Renju's
        forbidden-move rules for Black are not handled here, this v1
        targets freestyle self-play generation only.
        """
        for dx, dy in DIRECTIONS:
            count = 1
            for sign in (1, -1):
                nx, ny = x + dx * sign, y + dy * sign
                while self.grid.get((nx, ny)) == player:
                    count += 1
                    nx += dx * sign
                    ny += dy * sign
            if count >= WIN_LENGTH:
                return True
        return False


def play_one_game(
    engine_binary: str,
    board_size: int = BOARD_SIZE,
    time_budget_ms: Optional[int] = None,
) -> GameRecord:
    if time_budget_ms is None:
        time_budget_ms = random.choice(list(STRENGTH_PROFILES.values()))

    engine = RapfiEngine(engine_binary, board_size=board_size, timeout_turn_ms=time_budget_ms)
    engine.start()

    board = Board(board_size)
    game = GameRecord(
        game_id=str(uuid.uuid4()),
        board_size=board_size,
        time_budget_ms=time_budget_ms,
    )

    response = engine.begin()
    mover = 1
    move_number = 1

    while True:
        x, y = response.move
        board.place(x, y, mover)
        game.moves.append(
            MoveRecord(move_number, mover, x, y, response.eval_score, response.depth)
        )

        if board.check_win(x, y, mover):
            game.winner = mover
            break
        if board.is_full():
            game.winner = None  # draw
            break

        mover = 2 if mover == 1 else 1
        move_number += 1
        response = engine.turn(x, y)

    game.total_moves = len(game.moves)
    engine.close()
    return game


def save_game(game: GameRecord, output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{game.game_id}.json"
    payload = {
        "game_id": game.game_id,
        "board_size": game.board_size,
        "time_budget_ms": game.time_budget_ms,
        "winner": game.winner,
        "total_moves": game.total_moves,
        "moves": [
            {
                "move_number": m.move_number,
                "mover": m.mover,
                "x": m.x,
                "y": m.y,
                "eval_score": m.eval_score,
                "depth": m.depth,
                # label for calibration: did the mover at this position
                # go on to win the game? None for draws.
                "won": (m.mover == game.winner) if game.winner is not None else None,
            }
            for m in game.moves
        ],
    }
    path.write_text(json.dumps(payload, indent=2))
    return path


def run_batch(n_games: int, engine_binary: str, output_dir: Path) -> None:
    for i in range(n_games):
        start = time.time()
        game = play_one_game(engine_binary)
        path = save_game(game, output_dir)
        elapsed = time.time() - start
        print(
            f"[{i + 1}/{n_games}] {path.name} — {game.total_moves} moves, "
            f"winner={game.winner}, budget={game.time_budget_ms}ms, {elapsed:.1f}s"
        )


if __name__ == "__main__":
    # Start deliberately small. Confirm the pipeline end to end before
    # scaling up (Section 5.1: "start small, 20-50 games first").
    ENGINE_BINARY = default_engine_binary()
    OUTPUT_DIR = Path(__file__).parent.parent / "data" / "raw"

    run_batch(n_games=5, engine_binary=str(ENGINE_BINARY), output_dir=OUTPUT_DIR)