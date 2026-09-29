"""Command-line entry point for quick instrument checks."""

from __future__ import annotations

import argparse
import os
from collections.abc import Sequence

from .controller import Oscilloscope, TektronixMSO4034
from .i18n import EN_US, ZH_CN, Translator
from .transport import SimulatedTransport, VisaTransport, list_visa_resources


def _cli_translator() -> Translator:
    """Build a translator for CLI messages.

    Language is selected via the ``OSCILL_LANGUAGE`` environment variable
    (``zh_CN``/``en_US``) and defaults to Chinese to preserve the original
    CLI behavior.
    """
    language = os.environ.get("OSCILL_LANGUAGE", ZH_CN)
    return Translator(language if language in (ZH_CN, EN_US) else ZH_CN)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Control a USB/VISA oscilloscope")
    parser.add_argument("--resource", help="VISA resource name, for example USB0::...::INSTR")
    parser.add_argument("--simulate", action="store_true", help="use a simulated oscilloscope")
    parser.add_argument("--model", choices=["generic", "tektronix-mso4034"], default="generic")
    parser.add_argument("command", choices=["identify", "list-resources", "measure"], nargs="?", default="identify")
    parser.add_argument("--parameter", default="VPP", help="SCPI measurement parameter")
    parser.add_argument("--source", help="measurement source; defaults to CH1 for Tektronix")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    t = _cli_translator()
    if args.command == "list-resources":
        resources = list_visa_resources()
        if resources:
            print("\n".join(resources))
        else:
            print(t.tr("cli.no_resources"))
        return 0

    if args.simulate:
        transport = SimulatedTransport()
    elif args.resource:
        transport = VisaTransport(args.resource)
    else:
        raise SystemExit(t.tr("cli.specify_resource"))

    scope_type = TektronixMSO4034 if args.model == "tektronix-mso4034" else Oscilloscope
    source = args.source or ("CH1" if scope_type is TektronixMSO4034 else "CHANnel1")
    with scope_type(transport) as scope:
        if args.command == "identify":
            print(scope.identify())
        else:
            result = scope.measure(args.parameter, source=source)
            suffix = f" {result.unit}" if result.unit else ""
            print(f"{result.source} {result.parameter}: {result.value}{suffix}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
