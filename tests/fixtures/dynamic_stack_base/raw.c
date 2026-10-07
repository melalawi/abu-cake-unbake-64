struct _m2c_stack_func_80115084_us {
    /* 0x00 */ char pad0[0x18];
    /* 0x18 */ s32 sp18;                            /* inferred */
    /* 0x1C */ s32 sp1C;                            /* inferred */
    /* 0x20 */ s32 sp20;                            /* inferred */
    /* 0x24 */ M2C_UNK sp24;                        /* inferred */
    /* 0x24 */ char pad24[0x20];
    /* 0x44 */ s32 sp44;                            /* inferred */
    /* 0x48 */ s32 sp48;                            /* inferred */
    /* 0x4C */ s32 sp4C;                            /* inferred */
};                                                  /* size = 0x50 */

s32 func_80115084_us(void *arg0, s32 arg1, s32 arg2, void *arg3, u8 arg4, void *arg5, void *arg6) {
    s32 sp4C;
    s32 sp48;
    s32 sp44;
    M2C_UNK sp24;
    s32 sp20;
    s32 sp1C;
    s32 sp18;
    s32 temp_t4;
    s32 temp_t7;
    s32 temp_t8;
    s32 temp_t9;

    sp1C = 0;
    if ((s32) arg4 > 0) {
        sp18 = 1;
    } else {
        sp18 = M2C_FIELD(arg0, s32 *, 0x60);
    }
    sp4C = sp18;
    if (sp18 < 0x80) {
loop_4:
        if (*(arg1 + (sp4C * 2)) != 3) {
            temp_t7 = sp4C + 1;
            sp4C = temp_t7;
            if (temp_t7 < 0x80) {
                goto loop_4;
            }
        }
    }
    if (sp4C == 0x80) {
        *arg3 = -1;
        goto block_22;
    }
    sp20 = 0;
    do {
        M2C_FIELD((sp + sp20), s8 *, 0x24) = 0;
        temp_t4 = sp20 + 1;
        sp20 = temp_t4;
    } while (temp_t4 < 0x20);
    sp48 = sp4C;
    *arg5 = 1;
    temp_t8 = sp4C;
    temp_t9 = temp_t8 + 1;
    sp4C = temp_t9;
    sp44 = temp_t8;
    if ((*arg5 < arg2) && (temp_t9 < 0x80)) {
loop_12:
        if (*(arg1 + (sp4C * 2)) == 3) {
            *(arg1 + (sp44 * 2)) = arg4;
            M2C_FIELD((arg1 + (sp44 * 2)), s8 *, 1) = (s8) sp4C;
            sp1C = func_801152E8_us(arg0, sp44, (s32) &sp24, arg4);
            if (sp1C != 0) {
                return sp1C;
            }
            sp44 = sp4C;
            *arg5 = (s32) (*arg5 + 1);
            goto block_16;
        }
block_16:
        sp4C += 1;
        if ((*arg5 >= arg2) || (sp4C >= 0x80)) {
            goto block_18;
        }
        goto loop_12;
    }
block_18:
    *arg3 = sp48;
    if ((sp4C == 0x80) && (*arg5 < arg2)) {
        *arg6 = sp44;
    } else {
        *(arg1 + (sp44 * 2)) = 1;
        sp1C = func_801152E8_us(arg0, sp44, (s32) &sp24, arg4);
        *arg6 = 0;
    }
block_22:
    return sp1C;
}
