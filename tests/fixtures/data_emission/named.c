#include "span_16E000/code_80444030.h"
#include "types.h"

/* Once the timer D_80154100 is exactly zero, calls func_8040C2F8_de, reloads the timer from
   D_800E27C0 and copies the byte D_800E28DB to D_80146848. Returns zero. */
extern f32 D_8014DE70;
extern u8 D_800DE88B;
extern u8 D_80142788;
extern void func_8040C2F8_de();

static const f32 controller_pak_menu_reload_seconds = 18.0f;

s32 func_80444CF8_de(void) {
    if (D_8014DE70 == 0.0f) {
        func_8040C2F8_de();
        D_8014DE70 = controller_pak_menu_reload_seconds;
        D_80142788 = D_800DE88B;
    }
    return 0;
}
