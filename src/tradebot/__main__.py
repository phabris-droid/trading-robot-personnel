"""Point d'entrée : python -m tradebot"""
import sys

from tradebot.settings import ConfigError, load_settings


def main() -> int:
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"Démarrage refusé : {exc}", file=sys.stderr)
        return 2
    from tradebot.engine import Engine

    Engine(settings).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
