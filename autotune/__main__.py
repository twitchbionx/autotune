"""Module entry point for `python -m autotune`. Delegates to autotune.app.main()."""
import sys
from autotune.app import main

if __name__ == "__main__":
    sys.exit(main())
