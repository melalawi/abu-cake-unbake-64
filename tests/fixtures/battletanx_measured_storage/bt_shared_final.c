                                                  /* size = 0x38 */

extern unsigned char D_802D5BC0; /* opaque address transport */

void func_800949D8_us(s32 arg0, s32 arg1, s32 arg2, s32 arg3, s32 arg4, s32 arg5, s32 arg6) {
    union { unsigned char bytes[0x38];
    } m2c_stack;

    s32 *var_t0;
    s32 *var_t0_2;
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
                if (arg1 < ((struct Measured_func_800949D8_us_07091f3fea0d *)(temp_v1))->value) {
                    ((struct Measured_func_800949D8_us_07091f3fea0d *)(temp_v1))->value = arg1;
                    ((struct Measured_func_800949D8_us_5bc2a4c5e17f *)(temp_v1))->value = arg5;
                }
                var_a2 += 0x320;
            } while (var_t1 < arg4);
        }
    } else {
        var_t1_2 = 0;
        if (arg4 > 0) {
            var_t2 = m2c_stack.bytes;
            var_t0 = &D_802D5BC0;
            do {
                temp_v1_2 = (arg6 * 0x960) + var_t0 + (arg0 * 8);
                if (arg1 < ((struct Measured_func_800949D8_us_07091f3fea0d *)(temp_v1_2))->value) {
                    var_t3 = 0;
                    ((struct Measured_func_800949D8_us_07091f3fea0d *)(temp_v1_2))->value = arg1;
                    ((struct Measured_func_800949D8_us_5bc2a4c5e17f *)(temp_v1_2))->value = arg5;
                } else {
                    *var_t2 = ((struct Measured_func_800949D8_us_5bc2a4c5e17f *)(temp_v1_2))->value;
                }
                var_t2 += 4;
                var_t1_2 += 1;
                var_t0 += 0x320;
            } while (var_t1_2 < arg4);
        }
        var_t1_3 = 0;
        if (arg4 > 0) {
            var_t2_2 = m2c_stack.bytes;
            var_t0_2 = &D_802D5BC0;
            do {
                temp_v1_3 = (arg6 * 0x960) + var_t0_2 + (arg2 * 8);
                if (arg3 < ((struct Measured_func_800949D8_us_07091f3fea0d *)(temp_v1_3))->value) {
                    var_t3 = 0;
                    ((struct Measured_func_800949D8_us_07091f3fea0d *)(temp_v1_3))->value = arg3;
                    ((struct Measured_func_800949D8_us_5bc2a4c5e17f *)(temp_v1_3))->value = arg5;
                } else {
                    ((struct Measured_func_800949D8_us_7cc74cb35a88 *)(var_t2_2))->value = (s32) ((struct Measured_func_800949D8_us_5bc2a4c5e17f *)(temp_v1_3))->value;
                }
                var_t2_2 += 4;
                var_t1_3 += 1;
                var_t0_2 += 0x320;
            } while (var_t1_3 < arg4);
        }
        if (var_t3 != 0) {
            var_t1_4 = 0;
            if ((arg4 > 0) & (var_t3 != 0)) {
                var_t0_3 = m2c_stack.bytes;
                do {
                    var_t1_4 += 1;
                    var_t3 &= -(((struct Measured_func_800949D8_us_07091f3fea0d *)(var_t0_3))->value == ((struct Measured_func_800949D8_us_7cc74cb35a88 *)(var_t0_3))->value);
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
                            if (temp_t2 < ((struct Measured_func_800949D8_us_07091f3fea0d *)(temp_v1_4))->value) {
                                ((struct Measured_func_800949D8_us_07091f3fea0d *)(temp_v1_4))->value = temp_t2;
                                ((struct Measured_func_800949D8_us_5bc2a4c5e17f *)(temp_v1_4))->value = arg5;
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
