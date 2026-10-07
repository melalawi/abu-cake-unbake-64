struct _m2c_stack_func_800949D8_us {
    /* 0x00 */ char pad0[0x38];
};                                                  /* size = 0x38 */

extern M2C_UNK D_802D5BC0;

void func_800949D8_us(s32 arg0, s32 arg1, s32 arg2, s32 arg3, s32 arg4, s32 arg5, s32 arg6) {
    M2C_UNK *var_t0;
    M2C_UNK *var_t0_2;
    s32 *var_t0_3;
    s32 *var_t2;
    s32 *var_t2_2;
    s32 temp_lo;
    s32 temp_t2;
    s32 temp_t9;
    s32 var_a2;
    s32 var_t0_4;
    s32 var_t1;
    s32 var_t1_2;
    s32 var_t1_3;
    s32 var_t1_4;
    s32 var_t1_5;
    s32 var_t3;
    s32 var_t3_2;
    s32 var_v0;
    void *temp_v1;
    void *temp_v1_2;
    void *temp_v1_3;
    void *temp_v1_4;

    temp_t9 = arg2 - arg0;
    var_t3 = 1;
    if (temp_t9 == 0) {
        var_t1 = 0;
        if (arg4 > 0) {
            var_a2 = arg6 * 0x960;
            do {
                temp_v1 = (arg0 * 8) + var_a2 + &D_802D5BC0;
                var_t1 += 1;
                if (arg1 < M2C_FIELD(temp_v1, s32 *, 0)) {
                    M2C_FIELD(temp_v1, s32 *, 0) = arg1;
                    M2C_FIELD(temp_v1, s32 *, 4) = arg5;
                }
                var_a2 += 0x320;
            } while (var_t1 < arg4);
        }
    } else {
        var_t1_2 = 0;
        if (arg4 > 0) {
            var_t2 = sp;
            var_t0 = &D_802D5BC0;
            do {
                temp_v1_2 = (arg6 * 0x960) + var_t0 + (arg0 * 8);
                if (arg1 < M2C_FIELD(temp_v1_2, s32 *, 0)) {
                    var_t3 = 0;
                    M2C_FIELD(temp_v1_2, s32 *, 0) = arg1;
                    M2C_FIELD(temp_v1_2, s32 *, 4) = arg5;
                } else {
                    *var_t2 = M2C_FIELD(temp_v1_2, s32 *, 4);
                }
                var_t2 += 4;
                var_t1_2 += 1;
                var_t0 += 0x320;
            } while (var_t1_2 < arg4);
        }
        var_t1_3 = 0;
        if (arg4 > 0) {
            var_t2_2 = sp;
            var_t0_2 = &D_802D5BC0;
            do {
                temp_v1_3 = (arg6 * 0x960) + var_t0_2 + (arg2 * 8);
                if (arg3 < M2C_FIELD(temp_v1_3, s32 *, 0)) {
                    var_t3 = 0;
                    M2C_FIELD(temp_v1_3, s32 *, 0) = arg3;
                    M2C_FIELD(temp_v1_3, s32 *, 4) = arg5;
                } else {
                    M2C_FIELD(var_t2_2, s32 *, 0xC) = (s32) M2C_FIELD(temp_v1_3, s32 *, 4);
                }
                var_t2_2 += 4;
                var_t1_3 += 1;
                var_t0_2 += 0x320;
            } while (var_t1_3 < arg4);
        }
        if (var_t3 != 0) {
            var_t1_4 = 0;
            if ((arg4 > 0) & (var_t3 != 0)) {
                var_t0_3 = sp;
                do {
                    var_t1_4 += 1;
                    var_t3 &= -(M2C_FIELD(var_t0_3, s32 *, 0) == M2C_FIELD(var_t0_3, s32 *, 0xC));
                    var_t0_3 += 4;
                } while ((var_t1_4 < arg4) & (var_t3 != 0));
            }
            if (var_t3 == 0) {
                goto block_24;
            }
        } else {
block_24:
            var_t3_2 = arg0 + 1;
            if ((arg2 - 1) >= var_t3_2) {
                var_v0 = var_t3_2 - arg0;
                do {
                    temp_lo = var_v0 * (arg3 - arg1);
                    if (temp_t9 == 0) {

                    }
                    if ((temp_t9 == -1) && ((temp_lo / temp_t9) == 0x80000000)) {

                    }
                    var_t1_5 = 0;
                    temp_t2 = arg1 + (temp_lo / temp_t9);
                    if (arg4 > 0) {
                        var_t0_4 = arg6 * 0x960;
                        do {
                            temp_v1_4 = (var_t3_2 * 8) + var_t0_4 + &D_802D5BC0;
                            var_t1_5 += 1;
                            if (temp_t2 < M2C_FIELD(temp_v1_4, s32 *, 0)) {
                                M2C_FIELD(temp_v1_4, s32 *, 0) = temp_t2;
                                M2C_FIELD(temp_v1_4, s32 *, 4) = arg5;
                            }
                            var_t0_4 += 0x320;
                        } while (var_t1_5 < arg4);
                    }
                    var_t3_2 += 1;
                    var_v0 = var_t3_2 - arg0;
                } while ((arg2 - 1) >= var_t3_2);
            }
        }
    }
}
