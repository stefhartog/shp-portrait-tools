"""`shp-skin-tools` entry point: move pre-rename folders once, then start the Skin Tools web UI.

The server module resolves its data and settings folders when it is imported, so the one-time
migration has to run before that import. The supervisor starts the server module directly; by then
the launcher has already migrated.
"""

import os

from .shared.platform_paths import migrate_legacy_roots


def main():
    if not os.environ.get("DNB_DATA"):
        migrate_legacy_roots()
    from .modules.dnb.ui_server import main as serve
    serve()


if __name__ == "__main__":
    main()
