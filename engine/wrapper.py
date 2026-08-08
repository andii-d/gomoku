"""
Synchronous wrapper around the Rapfi engine binary, speaking the Piskvork
protocol (plus the YXSHOWINFO extension for eval scores).

Research-phase only: one game analysed at a time, no async, no process pool.
See PROJECT_PLAN.md Section 6.1.
"""

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


MOVE_RE = re.compile(r"^(\d+),(\d+)$")
EVAL_RE = re.compile(r"Eval (-?\d+)")
DEPTH_RE = re.compile(r"Depth (\d+)-(\d+)")


@dataclass
class EngineResponse:
    move: tuple[int, int]
    eval_score: Optional[int]
    depth: Optional[int]
    raw_messages: list[str] = field(default_factory=list)


class RapfiEngine:
    """
    Usage:
        engine = RapfiEngine("engine/rapfi_bin/pbrain-rapfi-macos-apple-silicon")
        engine.start()
        response = engine.begin()          # engine plays first move
        response = engine.turn(6, 6)       # tell engine opponent played (6,6)
        engine.close()
    """

    def __init__(
        self,
        binary_path: str,
        board_size: int = 15,
        timeout_turn_ms: int = 5000,
    ):
        self.binary_path = Path(binary_path)
        self.board_size = board_size
        self.timeout_turn_ms = timeout_turn_ms
        self.process: Optional[subprocess.Popen] = None

    def start(self) -> None:
        self.process = subprocess.Popen(
            [str(self.binary_path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,  # merge stderr into stdout for simplicity
            text=True,
            bufsize=1,  # line-buffered
            cwd=self.binary_path.parent,  # so it finds config.toml / weights
        )
        self._send("YXSHOWINFO")  # enables Eval reporting in MESSAGE lines
        self._send(f"START {self.board_size}")
        self._read_until_ok()
        self._send(f"INFO timeout_turn {self.timeout_turn_ms}")

    def begin(self) -> EngineResponse:
        """Ask the engine to play first."""
        self._send("BEGIN")
        return self._read_response()

    def turn(self, x: int, y: int) -> EngineResponse:
        """Tell the engine the opponent just played (x, y), get its reply move."""
        self._send(f"TURN {x},{y}")
        return self._read_response()

    def board(self, moves: list[tuple[int, int, int]]) -> EngineResponse:
        """
        Set up an arbitrary position and ask for the engine's move.
        moves: list of (x, y, player) where player is 1 (engine/self) or 2 (opponent),
        per the Piskvork BOARD command spec. NOT YET TESTED against this build,
        verify manually before relying on it.
        """
        self._send("BOARD")
        for x, y, player in moves:
            self._send(f"{x},{y},{player}")
        self._send("DONE")
        return self._read_response()

    def close(self) -> None:
        if self.process is None:
            return
        try:
            self._send("END")
            self.process.wait(timeout=2)
        except Exception:
            self.process.kill()

    # -- internals --

    def _send(self, cmd: str) -> None:
        assert self.process is not None and self.process.stdin is not None
        self.process.stdin.write(cmd + "\n")
        self.process.stdin.flush()

    def _read_until_ok(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        while True:
            line = self.process.stdout.readline().strip()
            if line == "OK":
                return
            # otherwise it's a startup MESSAGE line (config/version info), ignore

    def _read_response(self) -> EngineResponse:
        """
        Reads MESSAGE lines (tracking the deepest Eval/Depth seen) until the
        actual move line (e.g. "8,7") appears, then returns everything.
        """
        assert self.process is not None and self.process.stdout is not None
        messages: list[str] = []
        last_eval: Optional[int] = None
        last_depth: Optional[int] = None

        while True:
            line = self.process.stdout.readline().strip()
            if not line:
                continue

            if line.startswith("MESSAGE"):
                messages.append(line)
                eval_match = EVAL_RE.search(line)
                if eval_match:
                    last_eval = int(eval_match.group(1))
                depth_match = DEPTH_RE.search(line)
                if depth_match:
                    last_depth = int(depth_match.group(1))
                continue

            move_match = MOVE_RE.match(line)
            if move_match:
                x, y = int(move_match.group(1)), int(move_match.group(2))
                return EngineResponse(
                    move=(x, y),
                    eval_score=last_eval,
                    depth=last_depth,
                    raw_messages=messages,
                )
            # anything else unrecognised gets silently skipped for now;
            # if you see the wrapper hang, print `line` here to see what
            # the engine actually sent that didn't match either pattern.


if __name__ == "__main__":
    # Quick manual smoke test, mirrors the terminal session you just ran.
    # Path is anchored to this script's own location, so it works regardless
    # of which directory you run `python engine/wrapper.py` from.
    _binary_path = Path(__file__).parent / "rapfi_bin" / "pbrain-rapfi-macos-apple-silicon"
    engine = RapfiEngine(str(_binary_path))
    engine.start()

    resp = engine.begin()
    print(f"Engine opened with {resp.move}, eval={resp.eval_score}, depth={resp.depth}")

    resp = engine.turn(6, 6)
    print(f"After (6,6), engine replies {resp.move}, eval={resp.eval_score}, depth={resp.depth}")

    engine.close()