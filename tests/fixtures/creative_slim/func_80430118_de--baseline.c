#include "common/types_8fd754e1e915.h"
#include "common/unused.h"
#include "span_16E000/code_8042F988.h"

typedef struct ConfirmationNode { char pad[0x30]; struct ConfirmationNode *next; } ConfirmationNode;
typedef struct { char pad[0x2C]; s32 selected[4]; } ConfirmationRoot;
typedef struct { char pad[0x58]; s32 state, mode; char tail[0xBA0 - 0x60]; s32 status, counter; } ConfirmationChannel;
typedef struct { char bytes[0xB68]; } ConfirmationSlot;



void func_8025DF34_de(s32);                                 /* extern */
void func_8029973C_de();                                  /* extern */
s32 func_80299B4C_de();                                /* extern */
s32 func_8029DB58_de(s32);                             /* extern */
s32 func_8040EBD0_de(void *);                          /* extern */
ConfirmationNode *func_8041B7FC_de(s32, s32);                      /* extern */
void func_8041B8DC_de(s32, s32, void *);                  /* extern */
void func_80433BCC_de(s32);                               /* extern */
void func_80433D38_de(s32);                               /* extern */
void func_80434250_de(s32, s32);                            /* extern */
void func_80434EF4_de(s32);                               /* extern */
extern char *D_800E1454_de;                    

s32 func_80430118_de(s32 arg0, s32 arg1, s32 arg2, s32 arg3, s32 arg4) {
    ConfirmationChannel *channel;
    s32 temp_s0;
    s32 temp_s1;
    s32 temp_v0;
    void *temp_v1;
    ConfirmationNode *var_s0;
    ConfirmationNode *var_s0_2;
    ConfirmationNode *var_s0_3;

    temp_s1 = arg2 & 0xFFFF;
    channel = (ConfirmationChannel *)&((ConfirmationSlot *)D_800E1454_de)[temp_s1];
    temp_v0 = channel->state;
    switch (temp_v0) {
    case 12:
        temp_s0 = func_8029DB58_de(arg4);
        if (temp_s0 < func_80299B4C_de()) {
            temp_v1 = channel;
            ((ConfirmationChannel *)temp_v1)->counter = 0;
            ((ConfirmationChannel *)temp_v1)->status = 2;
        }
        if (arg3 == 1) {
            func_8029973C_de();
            func_80434EF4_de(temp_s1);
            channel->status = 0;
            func_8025DF34_de(0xE81);
            return 0;
        }
        return 0;
    case 4:
        if (arg3 == 1) {
            func_8029973C_de();
            func_80434250_de(temp_s1, 1);
            ((ConfirmationRoot *)D_800E1454_de)->selected[temp_s1] = 0;
            func_8025DF34_de(0xE74);
            return 0;
        }
        return 0;
    case 13:
        if ((channel->mode == 6) && (arg3 == 1)) {
            func_8029973C_de();
            var_s0 = func_8041B7FC_de(((func_80203E78_S1 *)(D_800E1454_de))->unk4, temp_s1)->next;
loop_11:
            if (func_8040EBD0_de(var_s0) != 0) {
                var_s0 = var_s0->next;
                goto loop_11;
            }
            func_8041B8DC_de((((func_80203E78_S1 *)(D_800E1454_de))->unk4), temp_s1, var_s0);
            func_8025DF34_de(0xE74);
            return 0;
        }
        return 0;
    case 22:
        if (arg3 == 1) {
            var_s0_2 = func_8041B7FC_de(((func_80203E78_S1 *)(D_800E1454_de))->unk4, temp_s1)->next;
loop_16:
            if (func_8040EBD0_de(var_s0_2) != 0) {
                var_s0_2 = var_s0_2->next;
                goto loop_16;
            }
            func_8041B8DC_de((((func_80203E78_S1 *)(D_800E1454_de))->unk4), temp_s1, var_s0_2);
            func_80433BCC_de(temp_s1);
        }
        goto block_24;
    case 27:
        if (arg3 == 1) {
            var_s0_3 = func_8041B7FC_de(((func_80203E78_S1 *)(D_800E1454_de))->unk4, temp_s1)->next;
loop_21:
            if (func_8040EBD0_de(var_s0_3) != 0) {
                var_s0_3 = var_s0_3->next;
                goto loop_21;
            }
            func_8041B8DC_de((((func_80203E78_S1 *)(D_800E1454_de))->unk4), temp_s1, var_s0_3);
            func_80433D38_de(temp_s1);
        }
block_24:
        func_8029973C_de();
        break;
    }
    return 0;
}
