#include "shared/func_80404D84_de_closed.h"

void func_80404D84_de(void) {
    struct Shape_typemap_165 *temp_v0;
    s32 temp_a0;

    temp_v0 = func_8025343C_de(0, 0x810, 0x23, &D_800DCCC0);
    temp_a0 = temp_v0->field_0;
    D_800DE808 = (s32 *)temp_v0;
    D_800DE804 = temp_a0;
    func_802A001C_de((void *)temp_a0, 0, 0x810);
    D_800DE800 = 1;
}
