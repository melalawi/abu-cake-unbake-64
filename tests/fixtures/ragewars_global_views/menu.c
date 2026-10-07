#include "shared/menu.h"
s32 gamma(void) {
    D_80140F80.pause = 0;
    D_80140F80.stage = 8;
    return D_80140F80.object.language;
}
