"""Tests for `escarp docs`."""

from __future__ import annotations

from pathlib import Path

from escarp import cli, cli_docs


def test_docs_lists_bundled_doc(capsys) -> None:
    rc = cli_docs.main([])

    output = capsys.readouterr()
    assert rc == 0
    assert "Bundled docs:" in output.out
    assert "codex-cua" in output.out
    assert "codex-cua-quickstart.txt" in output.out


def test_docs_prints_codex_cua_quickstart(capsys) -> None:
    rc = cli_docs.main(["codex-cua"])

    output = capsys.readouterr()
    assert rc == 0
    assert output.out.startswith("Escarp + Codex CUA quickstart")
    assert "Escarp slot alone is not enough" in output.out


def test_docs_path_points_to_packaged_doc(capsys) -> None:
    rc = cli_docs.main(["--path", "codex-cua"])

    output = capsys.readouterr()
    assert rc == 0
    path = Path(output.out.strip())
    assert path.name == "codex-cua-quickstart.txt"
    assert path.read_text(encoding="utf-8").startswith("Escarp + Codex CUA quickstart")


def test_top_level_cli_dispatches_docs(capsys) -> None:
    rc = cli.main(["docs", "codex-cua"])

    output = capsys.readouterr()
    assert rc == 0
    assert "Escarp + Codex CUA quickstart" in output.out
