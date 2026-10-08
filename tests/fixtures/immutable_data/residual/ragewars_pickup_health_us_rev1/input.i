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
extern s32 func_802ACC04_de(void *, void *);
ResidentHealthDescriptor D_800CDFC8_de[8] = {
    {{&D_800D7070[0], 1701, 1, 0, 0}, 2, 200, (ResidentPickupHandler)((char *)func_802ACC04_de - 0x80000000U)},
    {{&D_800D7070[1], 1702, 1, 0, 0}, 10, 100, (ResidentPickupHandler)((char *)func_802ACC04_de - 0x80000000U)},
    {{&D_800D7070[5], 1703, 1, 299, 0}, 100, 100, (ResidentPickupHandler)((char *)func_802ACC04_de - 0x80000000U)},
    {{&D_800D7070[6], 1704, 1, 299, 0}, 100, 200, (ResidentPickupHandler)((char *)func_802ACC04_de - 0x80000000U)},
    {{&D_800D7070[1], 1720, 1, 0, 0}, 10, 100, (ResidentPickupHandler)((char *)func_802ACC04_de - 0x80000000U)},
    {{&D_800D7070[2], 1721, 3560, 0, 0}, 15, 100, (ResidentPickupHandler)((char *)func_802ACC04_de - 0x80000000U)},
    {{&D_800D7070[3], 1722, 3560, 0, 0}, 25, 100, (ResidentPickupHandler)((char *)func_802ACC04_de - 0x80000000U)},
    {{&D_800D7070[4], 1723, 3570, 0, 0}, 50, 100, (ResidentPickupHandler)((char *)func_802ACC04_de - 0x80000000U)},
};
