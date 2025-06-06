import argparse

import scrapy
from scrapy.commands import ScrapyCommand
from scrapy.utils.versions import get_versions


class Command(ScrapyCommand):
    requires_crawler_process = False
    default_settings = {"LOG_ENABLED": False}

    def syntax(self) -> str:
        return "[-v]"

    def short_desc(self) -> str:
        return "Print Scrapy version"

    def add_options(self, parser: argparse.ArgumentParser) -> None:
        super().add_options(parser)
        parser.add_argument(
            "--verbose",
            "-v",
            dest="verbose",
            action="store_true",
            help="also display twisted/python/platform info (useful for bug reports)",
        )

    def run(self, args: list[str], opts: argparse.Namespace) -> None:
        if opts.verbose:
            versions = get_versions()
            width = max(len(n) for (n, _) in versions)
            for name, version in versions:
                print(f"{name:<{width}} : {version}")
        else:
            print(f"Scrapy {scrapy.__version__}")
