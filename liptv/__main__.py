"""支持 ``python -m liptv ...``。"""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
