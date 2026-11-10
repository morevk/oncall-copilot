"""`python -m oncall_memory` runs the review CLI, the package's only entry point."""

from .review import main

if __name__ == "__main__":
    raise SystemExit(main())
