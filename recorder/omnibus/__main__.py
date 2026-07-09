from __future__ import annotations

import typer
import uvicorn

from omnibus.config import settings

cli = typer.Typer(add_completion=False, no_args_is_help=False, help="ZW-Omnibus Recorder")


@cli.callback(invoke_without_command=True)
def _default(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is None:
        serve()


@cli.command()
def serve() -> None:
    """Run the recorder API (default command)."""
    uvicorn.run(
        "omnibus.web.app:app",
        host=settings.web_host,
        port=settings.web_port,
        log_level="info",
    )


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
