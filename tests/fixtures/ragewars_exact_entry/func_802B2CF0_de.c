#include "shared/func_802B2CF0_de_closed.h"

s32 func_802B2CF0_de(s32 val, f32 div) {
    f32 fval;
    f64 dresult;
    s32 result;

    if (div == 0.0f) {
        func_802BAC50_de(D_800C7548, D_800C754C, 0x11D);
    }

    result = 0x7FFFFFFF;
    fval = (f32)val / div;
    dresult = (f64)fval;
    if (!(D_800C7560 < dresult)) {
        result = (s32)dresult;
    }
    return result;
}
