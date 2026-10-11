"""Project-root launcher: `uv run python run.py`.

The app's modules import each other from the project root (`from core
import ...`), so the root must be on sys.path. Running `python
core/main.py` directly puts core/ on sys.path instead, and the imports fail
with ModuleNotFoundError. `python -m core.main` would also work, but this
file exists so the program starts with a plain filename and no -m flag.
Running it from the project root puts the root on sys.path, and it just
calls main().
"""

from core.main import main

if __name__ == "__main__":
    main()
