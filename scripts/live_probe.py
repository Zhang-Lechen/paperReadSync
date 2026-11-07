"""Local human entry point: no implicit --live or --confirm flags."""
import sys
from paperread.cli import app, configure_console

if __name__ == "__main__":
    configure_console()
    # Global options may instead be supplied through the normal paperread command.
    app(["probe", *sys.argv[1:]])
