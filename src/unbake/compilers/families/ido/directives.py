"""IDO's preprocessor treats an active error directive as exit-zero Warning605."""

import re

from unbake.compilers.compiler_contracts import IDO_DIRECTIVE, validate_preprocessed
from unbake.config import Held
from unbake.process import Fault, NativeResult, SourceLocation, named

_DIRECTIVE = re.compile(IDO_DIRECTIVE, re.M)


def preprocessed(result: NativeResult) -> str:
    match = _DIRECTIVE.search(result.stderr)
    if match is not None:
        reason = "compile.preprocessor_directive: active #error: " + match[3].strip()
        cause = named(
            "compile.preprocessor_directive",
            reason,
            owner="compilers",
            stage="compile",
            location=SourceLocation(match[1], int(match[2])),
            evidence={"native_exit": result.exit, "diagnostic": match[0]},
        )
        # Keep the actual zero exit and both complete streams; do not invent a native failure.
        raise Held(Fault(cause, (result,)))
    return validate_preprocessed(result.stdout, result.stderr, result.exit, "ido-1")
