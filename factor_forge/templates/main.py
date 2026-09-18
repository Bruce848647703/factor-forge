import datetime as dt
import os
import pathlib

NUM_THREADS = "1"
os.environ["OMP_NUM_THREADS"] = NUM_THREADS
os.environ["MKL_NUM_THREADS"] = NUM_THREADS
os.environ["NUMEXPR_NUM_THREADS"] = NUM_THREADS
os.environ["NUMEXPR_MAX_THREADS"] = NUM_THREADS
os.environ["NUM_INTER_THREADS"] = NUM_THREADS
os.environ["NUM_INTRA_THREADS"] = NUM_THREADS

import click  # noqa: E402
import function


@click.command(context_settings=dict(help_option_names=["-h", "--help"]))
@click.option("-d", "--date", help="runner date")
@click.option("-o", "--output", default=".", help="output dir path")
@click.option(
    "-r", "--rawdata", default="rawdata.json", help="database config file path"
)
@click.option(
    "-n",
    "--dry-run",
    is_flag=True,
    show_default=True,
    default=False,
    help="skip run signal",
)
@click.option(
    "--debug", is_flag=True, show_default=True, default=False, help="show input params"
)
def main(
    date: dt.date,
    output: pathlib.Path,
    rawdata: pathlib.Path,
    debug: bool = False,
    dry_run: bool = False,
) -> None:
    import core

    function.RAWDATA = rawdata
    core.main(date, output)


if __name__ == "__main__":
    main()
