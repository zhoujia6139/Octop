"""Terminal locale without loading the retired control-plane database."""

from __future__ import annotations

import os

from octop.i18n import tr
from octop.infra.utils.locale import Locale, normalize_locale


def cli_locale() -> Locale:
    return normalize_locale(
        os.environ.get("LC_ALL") or os.environ.get("LC_MESSAGES") or os.environ.get("LANG")
    )


def cli_text(key: str) -> str:
    return tr("product_cli." + key, cli_locale())
