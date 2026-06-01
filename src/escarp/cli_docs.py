"""Bundled documentation CLI."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from importlib.resources import as_file, files
from importlib.resources.abc import Traversable

DOCS = {
    "codex-cua": "codex-cua-quickstart.txt",
}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="escarp docs")
    parser.add_argument(
        "name",
        nargs="?",
        choices=sorted(DOCS),
        help="bundled doc to print",
    )
    parser.add_argument(
        "--path",
        action="store_true",
        help="print the installed filesystem path instead of the doc contents",
    )
    parser.add_argument(
        "--open",
        action="store_true",
        help="open the installed doc with the platform default text viewer",
    )
    return parser


def doc_path(name: str) -> Traversable:
    return files("escarp").joinpath("docs", DOCS[name])


def print_doc(name: str) -> None:
    print(doc_path(name).read_text(encoding="utf-8"), end="")


def _open_path(path: str) -> int:
    if sys.platform == "darwin":
        cmd = ["open", path]
    elif os.name == "nt":
        os.startfile(path)  # type: ignore[attr-defined]
        return 0
    else:
        cmd = ["xdg-open", path]
    return subprocess.run(cmd, check=False).returncode


def _list_docs() -> None:
    print("Bundled docs:")
    for name in sorted(DOCS):
        with as_file(doc_path(name)) as path:
            print(f"  {name}\t{path}")


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    if args.open and args.path:
        print("choose only one of --open or --path", file=sys.stderr)
        return 2

    if args.name is None:
        _list_docs()
        return 0

    path = doc_path(args.name)
    if args.path:
        with as_file(path) as fs_path:
            print(fs_path)
        return 0
    if args.open:
        with as_file(path) as fs_path:
            return _open_path(str(fs_path))

    print_doc(args.name)
    return 0
