#include "common/types_06e4f7ef1f9e.h"
#include "common/types_1dc8418c21db.h"
#include "common/types_8a8189af7b05.h"
#include "common/types_8fd754e1e915.h"
#include "span_1000/code_8020AF9C.h"
#include "types.h"







extern f32 D_800C1D90_de[];
extern CollisionInfo8020CD74 D_800FFFF0;
extern CollisionInfo8020CD74 D_80100050;
extern CollisionInfo8020CD74 D_80100070;
extern CollisionInfo8020CD74 D_801000D0;

extern s32 func_8024DEE0_de(void *);
extern s32 func_8024DF5C_de(void *);
extern s32 func_8024DFA0_de(void *);
extern s32 func_80243A90_de(Instance8020CD74 *, Vec3, CollisionInfo8020CD74 *);




s32 func_8020CD74_de(s32 **arg0, Instance8020CD74 *arg1, s32 arg2) {
    Vec3 position;
    CollisionInfo8020CD74 collision;
    Instance8020CD74 saved;
    s32 first_test;
    s32 result;
    s32 *table;
    void *entry;

    table = *arg0;
    entry = (u8 *)table + ((arg2 * table[0]) + 8);
    position = *(Vec3 *)entry;
    position.y += D_800C1D90_de[1];

    if ((*(u8 *)arg1 == 1) && (((func_80203C40_S1 *)(arg1))->unk100 & 0x300000)) {
        collision = D_801000D0;
    } else {
        first_test = func_8024DEE0_de(arg1);
        if (func_8024DF5C_de(arg1) == 0) {
            if (first_test != 0) {
                collision = D_80100050;
            } else {
                collision = D_801000D0;
            }
        } else if (func_8024DFA0_de(arg1) == 0) {
            collision = D_80100070;
        } else {
            collision = D_800FFFF0;
        }
    }
    collision.w[0] = 0x4000;
    saved = *arg1;
    result = func_80243A90_de(arg1, position, &collision);
    *arg1 = saved;
    return result == 0;
}
