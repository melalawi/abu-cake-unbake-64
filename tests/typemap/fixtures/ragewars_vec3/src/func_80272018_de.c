#include "span_1000/code_80271B18.h"
struct Vec3;
#include "types.h"
#include "common/types_8a8189af7b05.h"

/** Cross product of two vectors, written directly to the output (no aliasing guard). */
void func_80272018_de(Vec3 *out, Vec3 *a, Vec3 *b) {
    out->x = (a->y * b->z) - (a->z * b->y);
    out->y = (a->z * b->x) - (a->x * b->z);
    out->z = (a->x * b->y) - (a->y * b->x);
}
