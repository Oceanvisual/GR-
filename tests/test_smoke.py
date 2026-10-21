import gr
from gr.__main__ import build_parser


def test_import() -> None:
    assert gr.__version__
    assert build_parser().parse_args(["serve"]).host == "127.0.0.1"
