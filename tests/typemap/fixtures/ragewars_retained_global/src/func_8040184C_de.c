#include "common/types_1dc8418c21db.h"
#include "common/types_8fd754e1e915.h"
#include "common/unused.h"
#include "span_16E000/code_80400000.h"
#include "types.h"
/* Evaluates keyframe track 3 of the current record's resource at time t: before the first key or
   after the last it holds that key's value, otherwise it finds the surrounding keys and blends
   their values with the smoothstep weight 3u^2 - 2u^3; an empty track yields D_800E0B60[1]. */





extern func_80203E78_S1 *D_800DE7E0;


#define THREE D_800DCB38
#define ONE (*(&D_800DCB38 + 1))

extern s32 *func_8028FDB4_de(s32 resource, s32 index);

f32 func_8040184C_de(f32 t) {
    s32 *track;
    D_800C7470_Pair *key;
    s32 count;
    s32 i;
    f32 u;

    track = func_8028FDB4_de(D_800DE7E0->unk4, 3);
    key = (D_800C7470_Pair *)(track + 2);
    count = track[1];
    for (i = 0; i < count; i++) {
    }
    if (count == 0) {
        return D_800DCB30[1];
    }
    if (t <= key[0].second) {
        return key[0].first;
    }
    if (key[count - 1].second <= t) {
        return key[count - 1].first;
    }
    while (key->second < t) {
        key++;
    }
    u = (t - key[-1].second) / (key->second - key[-1].second);
    u = u * (u * THREE) - 2.0f * u * u * u;
    return key[-1].first * (ONE - u) + key->first * u;
}
