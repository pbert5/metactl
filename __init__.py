"""Public package surface with lazy CLI loading."""


def main(*args, **kwargs):
    from .cli import main as _main
    return _main(*args, **kwargs)
