#include "types.h"
#include "span_1000/code_80200610.h"

/* Signature follows the published cartridge word reader implementation. */
extern s32 func_802005A0_de(s32 arg0);

int func_80200840_de(int arg0) {
    int shift = (~arg0 & 3) << 3;
    return (func_802005A0_de(arg0 & -4) >> shift) & 0xFF;
}
