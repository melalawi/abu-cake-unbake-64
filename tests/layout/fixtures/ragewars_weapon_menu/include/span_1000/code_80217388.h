#ifndef UNBAKE_SPAN_1000_CODE_80217388_H
#define UNBAKE_SPAN_1000_CODE_80217388_H
#include "../types.h"

struct Slot_func_80217928_de;
/* unbake published declaration: published_48daf0666b8dd98d158756e7 */
typedef struct Slot_func_80217928_de Slot_func_80217928_de;

struct Slot_func_80217928_de;
/* unbake published declaration: published_8d1afac22f8ab88dc02b7244 */
struct Slot_func_80217928_de {
    s32 weapon;
    void *info;
    s32 owned;
    f32 angle;
    s32 y;
    f32 scale;
};

struct Menu;
/* unbake published declaration: published_0a586c2fa339b710d0e3976f */
struct Menu {
    s32 active;
    s32 pad4;
    f32 open;
    char padC[0x1C - 0xC];
    Slot_func_80217928_de slots[8];
    char pad[0x37C - 0xDC];
    s32 cursor;
};

struct func_80217388_S1;
/* unbake published declaration: published_428510071e7214861bc70de0 */
struct func_80217388_S1 {
    char pad0[0xD0];
    void * unkD0;
};

struct func_80217388_S2;
/* unbake published declaration: published_5f642439016a0f640555c7d4 */
typedef struct func_80217388_S2 func_80217388_S2;

struct func_80217388_S2;
/* unbake published declaration: published_7487c6dc23a40b962d63bf71 */
struct func_80217388_S2 {
    char pad0[0xFC];
    s32 unkFC;
};

struct Menu;
/* unbake published declaration: published_ae2da6d6ec1ed6fadc8f747f */
typedef struct Menu Menu;

struct func_80217388_S1;
/* unbake published declaration: published_bd3d3363fcf51e7c8a26fafb */
typedef struct func_80217388_S1 func_80217388_S1;

#endif
