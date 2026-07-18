"""Allow ``python3 -m src.workflow`` as the Phase 1 Drive CLI entrypoint."""

from .cli import main

raise SystemExit(main())
