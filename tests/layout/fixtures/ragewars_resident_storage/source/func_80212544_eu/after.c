#include "types.h"
#include "common/types_06e4f7ef1f9e.h"
#include "span_1000/code_802106E0.h"

extern s32 func_802744D4_de(void);
extern void func_80209988_de(void *object);

void func_80212544_eu(void *arg0) {
    void *level1 = ((Shared_Actor *)arg0)->entity;
    void *inner = ((func_80212828_S5 *)(level1))->unk1454;
    s32 r1, r2, r3;

    ((func_80212450_S3 *)(inner))->unk220 = 0;

    r1 = func_802744D4_de();
    ((func_80212450_S3 *)(inner))->unk2D8 = r1 % 4 + 2;

    r2 = func_802744D4_de();
    ((func_80212450_S3 *)(inner))->unk2DC = r2 % 2;

    r3 = func_802744D4_de();
    ((func_80212450_S3 *)(inner))->unk2E0 = r3 % 32400 + 0x2710;

    func_80209988_de(inner);

    ((func_80212450_S3 *)(inner))->unk318 = 0;
    ((func_80212450_S3 *)(inner))->unk31C = 0;
    ((func_80212450_S3 *)(inner))->unk320 = -1;
}
