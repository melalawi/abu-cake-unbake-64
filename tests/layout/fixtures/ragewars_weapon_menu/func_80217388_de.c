#include "span_1000/code_80217388.h"
#include "types.h"

extern void func_8025E1C4_de(s32);
extern void *func_8025CC6C_de(void);
extern s32 func_8025CA24_de(void *, void *);






void func_80217388_de(void *arg0, void *arg1) {
    void *owner = 0;
    u8 type = *(u8 *)arg0;

    switch (type) {
    case 1:
    case 2:
        owner = arg0;
        break;
    case 0:
        owner = ((func_80217388_S1 *)(arg0))->unkD0;
        break;
    }

    if (((func_80217388_S2 *)(arg1))->unkFC != 0) {
        func_8025E1C4_de(owner);
        if (((func_80217388_S2 *)(arg1))->unkFC != 0) {
            func_8025CA24_de(func_8025CC6C_de(), ((func_80217388_S2 *)(arg1))->unkFC);
            ((func_80217388_S2 *)(arg1))->unkFC = 0;
        }
    }
}

