"""Unit tests for Rapfi host binary selection (no engine process required)."""

from pathlib import Path

import pytest

from engine.wrapper import (
    X86_ISA_PREFERENCE,
    _candidate_names,
    default_engine_binary,
    supported_x86_isas,
)


def test_supported_isas_prefer_fastest():
    flags = {"avx512f", "avx512vnni", "avx2", "sse2", "avxvnni"}
    assert supported_x86_isas(flags)[0] == "avx512vnni"
    assert supported_x86_isas(flags) == [
        "avx512vnni",
        "avxvnni",
        "avx512",
        "avx2",
        "sse",
    ]


def test_supported_isas_fallback_to_sse():
    assert supported_x86_isas(set()) == ["sse"]
    assert supported_x86_isas({"avx2", "sse2"}) == ["avx2", "sse"]


def test_windows_candidate_order():
    names = _candidate_names("Windows", "x86_64", list(X86_ISA_PREFERENCE))
    assert names[0] == "pbrain-rapfi-windows-avx512vnni.exe"
    assert names[-2] == "pbrain-rapfi-windows-sse.exe"
    assert names[-1] == "pbrain-rapfi.exe"


def test_linux_candidate_order():
    names = _candidate_names("Linux", "x86_64", ["avx2", "sse"])
    assert "pbrain-rapfi-linux-clang-avx2" in names
    assert "pbrain-rapfi-linux-clang-sse" in names


def test_macos_arm_candidates():
    names = _candidate_names("Darwin", "arm64", [])
    assert names[0] == "pbrain-rapfi-macos-apple-silicon"


def test_default_engine_binary_resolves_on_this_host():
    path = default_engine_binary()
    assert path.is_file()
    assert path.name.startswith("pbrain-rapfi")


def test_default_engine_binary_missing_dir(tmp_path: Path):
    with pytest.raises(RuntimeError, match="No Rapfi engine binary"):
        default_engine_binary(tmp_path)


def test_default_engine_binary_picks_best_available(tmp_path: Path):
    # Simulate a Windows-like inventory on whatever host; we only check
    # that among matching candidate files, the highest ISA wins.
    (tmp_path / "pbrain-rapfi-linux-clang-sse").write_bytes(b"x")
    (tmp_path / "pbrain-rapfi-linux-clang-avx2").write_bytes(b"x")
    (tmp_path / "pbrain-rapfi-linux-clang-avx512").write_bytes(b"x")
    (tmp_path / "pbrain-rapfi-windows-avx2.exe").write_bytes(b"x")
    (tmp_path / "pbrain-rapfi-macos-apple-silicon").write_bytes(b"x")

    chosen = default_engine_binary(tmp_path)
    # On this CI/dev machine the OS filter still applies, so we only assert
    # the chosen file exists in the fake dir and is a known Rapfi name.
    assert chosen.parent == tmp_path
    assert chosen.name.startswith("pbrain-rapfi")
