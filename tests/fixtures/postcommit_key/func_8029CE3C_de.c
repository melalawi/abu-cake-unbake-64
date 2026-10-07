#include "types.h"
#include "common/types_8a8189af7b05.h"
#include "span_1000/code_8029BBA0.h"

void func_8029CE3C_de(s32 arg0, s32 arg1, s32 arg2) {
    Matrix temporary;
    Matrix *out = (Matrix *)arg0;
    Matrix *a = (Matrix *)arg1;
    Matrix *b = (Matrix *)arg2;
    s32 row;

    if (out == a) {
        temporary = *a;
        if (a == b) {
            b = &temporary;
        }
        a = &temporary;
    } else if (out == b) {
        temporary = *b;
        b = &temporary;
    }

    {
        f32 *destination = out->m;
        f32 *left = a->m;
        f32 *right = b->m;
        row = 0;
        do {
            destination[0] = left[0] * right[0] +
                left[4] * right[1] +
                left[8] * right[2] +
                left[12] * right[3];
            destination[1] = left[1] * right[0] +
                left[5] * right[1] +
                left[9] * right[2] +
                left[13] * right[3];
            destination[2] = left[2] * right[0] +
                left[6] * right[1] +
                left[10] * right[2] +
                left[14] * right[3];
            destination[3] = left[3] * right[0] +
                left[7] * right[1] +
                left[11] * right[2] +
                left[15] * right[3];
            destination += 4;
            right += 4;
        } while (++row < 4);
    }
}
