typedef signed char s8;
typedef unsigned char u8;
typedef signed short s16;
typedef unsigned short u16;
typedef signed int s32;
typedef unsigned int u32;
typedef signed long long s64;
typedef unsigned long long u64;
typedef float f32;
typedef double f64;
typedef struct ResidentPickupPrefix {
    const char **labels;
    s16 itemId;
    s16 soundId;
    s16 effectId;
    u16 flags;
} ResidentPickupPrefix;
typedef s32 (*ResidentPickupHandler)(void *actor, void *entry);
typedef s32 (*ResidentTargetedPickupHandler)(void *actor, void *entry, s32 target);
typedef struct ResidentPowerupDescriptor {
    ResidentPickupPrefix common;
    s16 slotIndex;
    s16 powerupType;
    s32 amount;
    ResidentPickupHandler handler;
} ResidentPowerupDescriptor;
typedef struct ResidentWeaponDescriptor {
    ResidentPickupPrefix common;
    s16 weaponSlot;
    s16 ammoAmount;
    ResidentPickupHandler handler;
} ResidentWeaponDescriptor;
typedef struct ResidentHealthDescriptor {
    ResidentPickupPrefix common;
    s32 amount;
    s32 value;
    ResidentPickupHandler handler;
} ResidentHealthDescriptor;
typedef struct ResidentLifeDescriptor {
    ResidentPickupPrefix common;
    u16 amount;
    u16 flags;
    ResidentPickupHandler handler;
} ResidentLifeDescriptor;
typedef struct ResidentSpecialDescriptor {
    ResidentPickupPrefix common;
    ResidentPickupHandler handler;
} ResidentSpecialDescriptor;
typedef struct ResidentTargetedDescriptor {
    ResidentPickupPrefix common;
    ResidentTargetedPickupHandler handler;
} ResidentTargetedDescriptor;
typedef struct ResidentAmmoDescriptor {
    ResidentPickupPrefix common;
    s16 ammoAmount[3];
    u16 flags;
    ResidentPickupHandler handler;
} ResidentAmmoDescriptor;
extern const char *D_800D7070[103];
extern s32 func_802AC66C_de(void *, void *);
ResidentPowerupDescriptor D_800CDEF0[4] = {
    {{&D_800D7070[49], 2123, 3596, -1, 0}, 18, 3, 65536, (ResidentPickupHandler)((char *)func_802AC66C_de - 0x80000000U)},
    {{&D_800D7070[50], 2124, 3596, -1, 0}, 19, 4, 196608, (ResidentPickupHandler)((char *)func_802AC66C_de - 0x80000000U)},
    {{&D_800D7070[51], 2125, 3596, -1, 0}, 20, 6, 65536, (ResidentPickupHandler)((char *)func_802AC66C_de - 0x80000000U)},
    {{&D_800D7070[53], 2127, 3596, -1, 0}, 21, 5, 65536, (ResidentPickupHandler)((char *)func_802AC66C_de - 0x80000000U)},
};
