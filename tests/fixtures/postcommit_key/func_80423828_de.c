#include "types.h"
#include "common/types_1dc8418c21db.h"
#include "span_16E000/code_80423280.h"
/* Handles the menu message func_80299A08_de reports after func_8029973C_de, for its MESSAGE_COUNT messages from
   FIRST_MESSAGE (0x377 to 0x380 on us) through jtbl_800E1668: one reopens screen D_800E4518 (marking it shown with timer -1 and
   refreshing its two windows at 0x44 and 0x4C) and calls func_80245B28_de; one tries func_8042ACD8_de
   and on failure sets the screen's modes 5 and 4 with timer 20, else reopens it with timer 0; the
   others pass codes 0x17, 13, 12 and 14 to func_8042E988_de. Any handled message then runs
   func_80423080_de. Returns zero. */
/* The first message this handles and how many it takes: eu-x numbers the messages 4 higher, de 4
   lower and takes only 8, as each cartridge's own bytes show; us, us-rev1 and eu share 0x377 and 10. */
extern struct Screen_func_80423828_de *D_800E04C8;
extern void *jtbl_800DD638[];
extern void func_8029973C_de(void);
extern s32 func_80299A08_de(void);
extern void func_802A2394_de(void);
extern void func_8043C278_de(struct Screen_func_80423828_de *);
extern void func_8040E8D8_de(void *object, int enabled);
extern void func_80245B28_de(void);
extern void func_8042E988_de(s32);
extern s32 func_8042ACD8_de(void);
s32 func_80423828_de(void) {
    static void *labels[0] __attribute__((section(".sdata"))) = {
        &&reopen, &&code_17, &&retry, &&code_13, &&code_12, &&code_14, &&other, &&done
    };
    s32 handled;
    u32 message;
    func_8029973C_de();
    handled = 1;
    message = func_80299A08_de() -
#if defined(VERSION_DE)
        0x373;
#elif defined(VERSION_EU_X)
        0x37B;
#else
        0x377;
#endif
    if (message >=
#if defined(VERSION_DE)
        8
#else
        10
#endif
    ) {
        goto other;
    }
    goto *jtbl_800DD638[message];
reopen:
    func_802A2394_de();
    func_8043C278_de(D_800E04C8);
    D_800E04C8->shown = 1;
    D_800E04C8->timer = -1;
    func_8040E8D8_de(D_800E04C8->window, 1);
    func_8040E8D8_de(D_800E04C8->second_window, 1);
    func_80245B28_de();
    goto done;
code_17:
    func_8042E988_de(0x17);
    goto done;
retry:
    func_802A2394_de();
    if (func_8042ACD8_de() == 0) {
        D_800E04C8->mode = 5;
        D_800E04C8->next_mode = 4;
        D_800E04C8->timer = 20;
    } else {
        func_8043C278_de(D_800E04C8);
        D_800E04C8->shown = 1;
        D_800E04C8->timer = 0;
    }
    goto done;
code_13:
    func_8042E988_de(13);
    goto done;
code_12:
    func_8042E988_de(12);
    goto done;
code_14:
    func_8042E988_de(14);
    goto done;
other:
    handled = 0;
done:
    if (handled == 1) {
        func_80423080_de();
    }
    return 0;
}
