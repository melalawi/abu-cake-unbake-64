#ifdef NON_MATCHING
#include "common/types_06e4f7ef1f9e.h"
#include "common/types_1dc8418c21db.h"
#include "common/types_8a8189af7b05.h"
#include "common/types_8fd754e1e915.h"
#include "span_1000/code_8020AF9C.h"
#include "span_1000/code_802106E0.h"
#include "types.h"

extern char D_801372A4[];
extern f32 D_800C20C0_de[];
extern s32 func_8020CCE8_de(EntryList8020CCE8 *, s32, s32 *, s32 *);
extern void *func_8020C9B0_de(void *, s32);
extern void *func_8020C994_de(void *, s32);
extern unsigned char func_8020D1CC_de(void *, s32, s32);
extern f32 func_802726BC_de(f32 *, f32 *);

/* Choose the farthest reachable neighbor linked by kinds 1, 2 or 7
 * from the actor that this state is following. */
void func_80212670_de(void *object)
{
    s32 neighbors[64];
    s32 links[64];
    Vec3 position;
    func_80212948_S3 *state = object;
    func_80212828_S5 *actor;
    char *table = D_801372A4;
    f32 *point;
    f32 distance;
    f32 farthest;
    s32 actor_node;
    s32 best;
    s32 count;
    s32 i;
    Link *link;

    actor = ((func_8020A028_S3 *)state->unk64)->unk1D8;
    actor_node = ((func_80203E78_S1 *)actor->unk1454)->unk4;
    position = *(Vec3 *)&actor->unk8;
    for (i = 0; i < 64; i++) {
        neighbors[i] = -1;
        links[i] = -1;
    }
    best = -1;
    farthest = D_800C20C0_de[0];
    count = func_8020CCE8_de((EntryList8020CCE8 *)table, state->unk4, neighbors, links);
    for (i = 0; i < count; i++) {
        link = func_8020C9B0_de(table, links[i]);
        if ((link->type >= 1 && link->type <= 2) || link->type == 7) {
            point = func_8020C994_de(table, neighbors[i]);
            if (func_8020D1CC_de(table, actor_node, neighbors[i])) {
                distance = func_802726BC_de(&position.x, point);
                if (farthest < distance) {
                    farthest = distance;
                    best = i;
                }
            }
        }
    }
    state->unkC = best;
}
#endif /* NON_MATCHING */
