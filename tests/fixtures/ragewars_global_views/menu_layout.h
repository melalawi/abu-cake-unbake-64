#include "types.h"
typedef struct Shared_MenuObject Shared_MenuObject;
struct Shared_MenuObject {
    u8 unknown00[0x17C1];
    u8 language;
    u8 unknown17C2[0x2E];
};
typedef struct Shared_MenuData Shared_MenuData;
struct Shared_MenuData {
    u32 unknown00[18];
    Shared_MenuObject object;
    s32 stage;
    u32 unknown183C[6];
    s32 pause;
};
