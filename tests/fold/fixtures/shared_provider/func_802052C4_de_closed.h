#ifndef FUNC_802052C4_DE_CLOSED_H
#define FUNC_802052C4_DE_CLOSED_H
#include "types.h"
typedef void (*Shared_VoidCallback)(void);
typedef struct Shared_CallbackHook Shared_CallbackHook;
struct Shared_CallbackHook {
    u32 unknown00[2];
    Shared_VoidCallback callback;
};
typedef struct Shared_CallbackOwner Shared_CallbackOwner;
struct Shared_CallbackOwner {
    u32 unknown00[12];
    Shared_CallbackHook *hook;
};

#include "types.h"
typedef void (*FuncPtr)(void);


#endif
