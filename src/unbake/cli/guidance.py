"""Render the owning typed Action; guidance never searches a fault or reparses its reason."""

from unbake.cli.args import Context
from unbake.config import Held


def after(context: Context, error: Held) -> str:
    return error.fault.cause.action.render(context)
