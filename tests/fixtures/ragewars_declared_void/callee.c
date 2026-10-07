#include "span_1000/code_8027302C.h"
#include "types.h"

void func_80274098_de(void *arg0, void *arg1, void *arg2) {
    f32 *out = (f32 *)arg0;
    f32 *a = (f32 *)arg1;
    f32 *b = (f32 *)arg2;

    out[0] = ((b[3] * a[0]) - (b[2] * a[1]))
             + (b[1] * a[2]) + (b[0] * a[3]);
    out[1] = ((b[2] * a[0]) + (b[3] * a[1]))
             - (b[0] * a[2]) + (b[1] * a[3]);
    out[2] = (-b[1] * a[0]) + (b[0] * a[1])
             + (b[3] * a[2]) + (b[2] * a[3]);
    out[3] = ((-b[0] * a[0]) - (b[1] * a[1]))
             - (b[2] * a[2]) + (b[3] * a[3]);
}
