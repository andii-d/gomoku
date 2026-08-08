"""
Synchronous wrapper around the Rapfi engine binary, speaking the Piskvork
protocol (plus the YXSHOWINFO extension for eval scores).

Research-phase only: one game analysed at a time, no async, no process pool.
See PROJECT_PLAN.md Section 6.1.
"""

from __future__ import annotations

import os
import platform
import re
import struct
import subprocess
import sys
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Optional


MOVE_RE = re.compile(r"^(\d+),(\d+)$")
EVAL_RE = re.compile(r"Eval (-?\d+)")
DEPTH_RE = re.compile(r"Depth (\d+)-(\d+)")

# Fastest → most compatible. Must match filenames shipped in engine/rapfi_bin/.
X86_ISA_PREFERENCE: tuple[str, ...] = (
    "avx512vnni",
    "avxvnni",
    "avx512",
    "avx2",
    "sse",
)


def _normalize_machine(machine: str | None = None) -> str:
    m = (machine or platform.machine() or "").lower()
    if m in {"x86_64", "amd64", "x64"}:
        return "x86_64"
    if m in {"aarch64", "arm64"}:
        return "arm64"
    if m in {"i386", "i686", "x86"}:
        return "x86"
    return m


def _linux_cpu_flags() -> set[str]:
    flags: set[str] = set()
    try:
        text = Path("/proc/cpuinfo").read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return flags
    for line in text.splitlines():
        if line.lower().startswith("flags") or line.lower().startswith("Features".lower()):
            # x86: "flags : ..."; aarch64: "Features : ..."
            _, _, rest = line.partition(":")
            flags.update(f.lower().replace("_", "") for f in rest.split())
    return flags


def _darwin_cpu_flags() -> set[str]:
    flags: set[str] = set()
    keys = (
        "machdep.cpu.features",
        "machdep.cpu.leaf7_features",
        "machdep.cpu.extfeatures",
    )
    for key in keys:
        try:
            out = subprocess.check_output(
                ["sysctl", "-n", key],
                stderr=subprocess.DEVNULL,
                text=True,
            )
        except (OSError, subprocess.CalledProcessError):
            continue
        flags.update(f.lower().replace("_", "") for f in out.split())
    return flags


def _cpuid_leaf(leaf: int, subleaf: int = 0) -> tuple[int, int, int, int] | None:
    """Run CPUID on x86 Windows. Linux/macOS use /proc/cpuinfo or sysctl instead."""
    if _normalize_machine() not in {"x86_64", "x86"}:
        return None
    if sys.platform != "win32":
        return None
    return _cpuid_windows(leaf, subleaf)


def _cpuid_windows(leaf: int, subleaf: int) -> tuple[int, int, int, int] | None:
    """CPUID via a one-shot machine-code thunk (x86/x64 Windows)."""
    import ctypes
    from ctypes import CFUNCTYPE, c_uint32

    is_64 = struct.calcsize("P") == 8
    # Windows x64 calling convention: args in rcx/rdx; return via pointer in r8.
    # Thunk writes eax,ebx,ecx,edx into out[4].
    if is_64:
        # rcx=leaf, rdx=subleaf, r8=out_ptr
        code = bytearray(
            b"\x53"  # push rbx
            b"\x41\x54"  # push r12
            b"\x49\x89\xD4"  # mov r12, rdx  (save subleaf)
            b"\x89\xC8"  # mov eax, ecx
            b"\x44\x89\xE1"  # mov ecx, r12d
            b"\x0F\xA2"  # cpuid
            b"\x41\x89\x00"  # mov [r8], eax
            b"\x41\x89\x58\x04"  # mov [r8+4], ebx
            b"\x41\x89\x48\x08"  # mov [r8+8], ecx
            b"\x41\x89\x50\x0C"  # mov [r8+12], edx
            b"\x41\x5C"  # pop r12
            b"\x5B"  # pop rbx
            b"\xC3"  # ret
        )
    else:
        # cdecl: [esp+4]=leaf, [esp+8]=subleaf, [esp+12]=out
        code = bytearray(
            b"\x53"  # push ebx
            b"\x57"  # push edi
            b"\x8B\x44\x24\x0C"  # mov eax, [esp+12] leaf
            b"\x8B\x4C\x24\x10"  # mov ecx, [esp+16] subleaf
            b"\x0F\xA2"  # cpuid
            b"\x8B\x7C\x24\x14"  # mov edi, [esp+20] out
            b"\x89\x07"  # mov [edi], eax
            b"\x89\x5F\x04"  # mov [edi+4], ebx
            b"\x89\x4F\x08"  # mov [edi+8], ecx
            b"\x89\x57\x0C"  # mov [edi+12], edx
            b"\x5F"  # pop edi
            b"\x5B"  # pop ebx
            b"\xC3"  # ret
        )

    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    MEM_COMMIT = 0x1000
    MEM_RESERVE = 0x2000
    PAGE_EXECUTE_READWRITE = 0x40
    MEM_RELEASE = 0x8000

    size = len(code)
    ptr = kernel32.VirtualAlloc(None, size, MEM_COMMIT | MEM_RESERVE, PAGE_EXECUTE_READWRITE)
    if not ptr:
        return None
    try:
        ctypes.memmove(ptr, bytes(code), size)
        out = (c_uint32 * 4)()
        if is_64:
            func_type = CFUNCTYPE(None, c_uint32, c_uint32, ctypes.POINTER(c_uint32))
            func_type(ptr)(leaf, subleaf, out)
        else:
            func_type = CFUNCTYPE(None, c_uint32, c_uint32, ctypes.POINTER(c_uint32))
            func_type(ptr)(leaf, subleaf, out)
        return int(out[0]), int(out[1]), int(out[2]), int(out[3])
    except Exception:
        return None
    finally:
        kernel32.VirtualFree(ptr, 0, MEM_RELEASE)


def _flags_from_cpuid() -> set[str]:
    flags: set[str] = set()
    leaf1 = _cpuid_leaf(1)
    if leaf1 is None:
        return flags
    _, _, ecx, edx = leaf1
    # EDX
    if edx & (1 << 25):
        flags.add("sse")
    if edx & (1 << 26):
        flags.add("sse2")
    # ECX
    if ecx & (1 << 0):
        flags.add("sse3")
    if ecx & (1 << 9):
        flags.add("ssse3")
    if ecx & (1 << 19):
        flags.add("sse41")
    if ecx & (1 << 20):
        flags.add("sse42")
    if ecx & (1 << 28):
        flags.add("avx")

    leaf7 = _cpuid_leaf(7, 0)
    if leaf7 is None:
        return flags
    _, ebx, ecx7, edx7 = leaf7
    if ebx & (1 << 5):
        flags.add("avx2")
    if ebx & (1 << 16):
        flags.add("avx512f")
    if ebx & (1 << 17):
        flags.add("avx512dq")
    if ebx & (1 << 21):
        flags.add("avx512ifma")
    if ebx & (1 << 30):
        flags.add("avx512bw")
    if ebx & (1 << 31):
        flags.add("avx512vl")
    # AVX512-VNNI is ECX bit 11 of leaf 7
    if ecx7 & (1 << 11):
        flags.add("avx512vnni")
    # AVX-VNNI is EAX bit 4 of leaf 7 subleaf 1
    leaf7_1 = _cpuid_leaf(7, 1)
    if leaf7_1 is not None:
        eax, _, _, _ = leaf7_1
        if eax & (1 << 4):
            flags.add("avxvnni")
    _ = edx7
    return flags


def _cpuinfo_flags() -> set[str]:
    """Optional py-cpuinfo enrichment when installed."""
    try:
        import cpuinfo  # type: ignore
    except ImportError:
        return set()
    try:
        info = cpuinfo.get_cpu_info()
    except Exception:
        return set()
    raw = info.get("flags") or []
    return {str(f).lower().replace("_", "") for f in raw}


@lru_cache(maxsize=1)
def detect_cpu_flags() -> frozenset[str]:
    """Union of OS-reported and CPUID / py-cpuinfo flags, underscore-stripped."""
    system = platform.system()
    flags: set[str] = set()
    if system == "Linux":
        flags |= _linux_cpu_flags()
    elif system == "Darwin":
        flags |= _darwin_cpu_flags()
    elif system == "Windows":
        flags |= _flags_from_cpuid()
    flags |= _cpuinfo_flags()
    # Alias normalisation used when matching ISA builds
    if "avx512f" in flags:
        flags.add("avx512")
    if "avx512_vnni" in flags or "avx512vnni" in flags:
        flags.add("avx512vnni")
    if "avx_vnni" in flags or "avxvnni" in flags:
        flags.add("avxvnni")
    return frozenset(flags)


def supported_x86_isas(flags: frozenset[str] | set[str] | None = None) -> list[str]:
    """Return X86 ISA build tags this CPU can run, best-first."""
    f = set(flags if flags is not None else detect_cpu_flags())
    supported: list[str] = []
    for isa in X86_ISA_PREFERENCE:
        if isa == "avx512vnni" and "avx512vnni" in f and "avx512f" in f:
            supported.append(isa)
        elif isa == "avxvnni" and "avxvnni" in f:
            supported.append(isa)
        elif isa == "avx512" and "avx512f" in f:
            supported.append(isa)
        elif isa == "avx2" and "avx2" in f:
            supported.append(isa)
        elif isa == "sse" and (
            "sse2" in f or "sse4.1" in f or "sse41" in f or "sse" in f or "ssse3" in f
        ):
            supported.append(isa)
    # Always allow SSE as last-resort candidate even if flag probing failed;
    # every Rapfi x86 release ships an SSE build and modern CPUs run it.
    if "sse" not in supported:
        supported.append("sse")
    return supported


def _candidate_names(system: str, machine: str, isas: list[str]) -> list[str]:
    names: list[str] = []
    if system == "Darwin":
        if machine == "arm64":
            names.extend(
                [
                    "pbrain-rapfi-macos-apple-silicon",
                    "pbrain-rapfi-macos-arm64",
                    "pbrain-rapfi-macos-neon",
                    "pbrain-rapfi",
                ]
            )
        elif machine == "x86_64":
            # Official releases currently ship Apple Silicon only; still probe
            # sensible names in case a local Intel build was dropped in.
            for isa in isas:
                names.append(f"pbrain-rapfi-macos-{isa}")
            names.extend(
                [
                    "pbrain-rapfi-macos-x86_64",
                    "pbrain-rapfi-macos-intel",
                    "pbrain-rapfi",
                ]
            )
        return names

    if system == "Windows":
        for isa in isas:
            names.append(f"pbrain-rapfi-windows-{isa}.exe")
        names.append("pbrain-rapfi.exe")
        return names

    if system == "Linux":
        if machine == "arm64":
            names.extend(
                [
                    "pbrain-rapfi-linux-clang-neon",
                    "pbrain-rapfi-linux-clang-arm64",
                    "pbrain-rapfi-linux-arm64",
                    "pbrain-rapfi",
                ]
            )
            return names
        for isa in isas:
            names.append(f"pbrain-rapfi-linux-clang-{isa}")
            names.append(f"pbrain-rapfi-linux-{isa}")
        names.append("pbrain-rapfi")
        return names

    return names


def _ensure_executable(path: Path) -> None:
    if os.name == "nt":
        return
    mode = path.stat().st_mode
    if mode & 0o111:
        return
    path.chmod(mode | 0o111)


def _list_bundled_binaries(bin_dir: Path) -> list[Path]:
    if not bin_dir.is_dir():
        return []
    found: list[Path] = []
    for p in sorted(bin_dir.iterdir()):
        if not p.is_file():
            continue
        name = p.name.lower()
        if name.startswith("pbrain-rapfi") or name == "rapfi" or name == "rapfi.exe":
            found.append(p)
    return found


def default_engine_binary(bin_dir: Path | None = None) -> Path:
    """
    Pick the best bundled Rapfi binary for this host.

    Selection order:
      1. OS + architecture (macOS arm64, Windows/Linux x86_64, Linux arm64, …)
      2. Highest CPU ISA the machine supports that we have a build for
         (avx512vnni → avxvnni → avx512 → avx2 → sse)
      3. Any remaining pbrain-rapfi* file in the directory as a last resort

    Raises RuntimeError with a clear inventory if nothing usable is found.
    """
    bin_dir = bin_dir or (Path(__file__).parent / "rapfi_bin")
    system = platform.system()
    machine = _normalize_machine()
    isas = supported_x86_isas() if machine in {"x86_64", "x86"} else []

    candidates = _candidate_names(system, machine, isas)
    seen: set[str] = set()
    for name in candidates:
        if name in seen:
            continue
        seen.add(name)
        path = bin_dir / name
        if path.is_file():
            _ensure_executable(path)
            return path

    # Last resort: any bundled pbrain binary (helps when someone only dropped
    # one local build with a non-standard name).
    bundled = _list_bundled_binaries(bin_dir)
    if len(bundled) == 1:
        _ensure_executable(bundled[0])
        return bundled[0]

    available = ", ".join(p.name for p in bundled) or "(none)"
    raise RuntimeError(
        "No Rapfi engine binary found for this host.\n"
        f"  platform : {system} / {machine}\n"
        f"  cpu isas : {', '.join(isas) if isas else '(n/a)'}\n"
        f"  looked in: {bin_dir}\n"
        f"  tried    : {', '.join(candidates) or '(no candidates)'}\n"
        f"  available: {available}\n"
        "Download a matching build from https://github.com/dhbloo/rapfi/releases "
        "into engine/rapfi_bin/ alongside config.toml and the weight files."
    )


@dataclass
class EngineResponse:
    move: tuple[int, int]
    eval_score: Optional[int]
    depth: Optional[int]
    raw_messages: list[str] = field(default_factory=list)


class RapfiEngine:
    """
    Usage:
        engine = RapfiEngine(str(default_engine_binary()))
        engine.start()
        response = engine.begin()          # engine plays first move
        response = engine.turn(6, 6)       # tell engine opponent played (6,6)
        engine.close()
    """

    def __init__(
        self,
        binary_path: str | Path | None = None,
        board_size: int = 15,
        timeout_turn_ms: int = 5000,
    ):
        resolved = Path(binary_path) if binary_path is not None else default_engine_binary()
        if not resolved.is_file():
            raise FileNotFoundError(f"Rapfi binary not found: {resolved}")
        _ensure_executable(resolved)
        self.binary_path = resolved
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
    binary = default_engine_binary()
    print(f"Using binary: {binary.name}")
    print(f"Host: {platform.system()} / {_normalize_machine()}")
    if _normalize_machine() in {"x86_64", "x86"}:
        print(f"Supported ISAs (best→compat): {', '.join(supported_x86_isas())}")

    engine = RapfiEngine(binary)
    engine.start()

    resp = engine.begin()
    print(f"Engine opened with {resp.move}, eval={resp.eval_score}, depth={resp.depth}")

    resp = engine.turn(6, 6)
    print(f"After (6,6), engine replies {resp.move}, eval={resp.eval_score}, depth={resp.depth}")

    engine.close()
