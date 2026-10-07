#include "shared/func_80245B74_de_closed.h"

void func_80245B74_de(void) {
    Shared_MenuContext *record;
    Shared_MenuVoidCallback fn;

    record = D_800DE7E0;
    if (record->initialized44 == 0) {
        fn = record->callback10;
        if (fn != 0) {
            record->callback10 = 0;
            fn();
        }
        record = D_800DE7E0;
        record->initialized44 = 1;
    }
}
