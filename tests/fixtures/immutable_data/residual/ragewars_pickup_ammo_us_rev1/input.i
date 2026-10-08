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
extern s32 func_802ACFB0_de(void *, void *);
ResidentAmmoDescriptor D_800CE1E0_de[16] = {
    {{&D_800D7070[102], 3033, 3580, 0, 0}, {0, 0, 0}, 0, (ResidentPickupHandler)((char *)func_802ACFB0_de - 0x80000000U)},
    {{&D_800D7070[102], 3034, 3580, 0, 0}, {0, 0, 0}, 0, (ResidentPickupHandler)((char *)func_802ACFB0_de - 0x80000000U)},
    {{&D_800D7070[102], 3035, 3580, 0, 0}, {0, 0, 0}, 0, (ResidentPickupHandler)((char *)func_802ACFB0_de - 0x80000000U)},
    {{&D_800D7070[102], 3036, 3580, 0, 0}, {0, 0, 0}, 0, (ResidentPickupHandler)((char *)func_802ACFB0_de - 0x80000000U)},
    {{&D_800D7070[102], 3037, 3580, 0, 0}, {0, 0, 0}, 0, (ResidentPickupHandler)((char *)func_802ACFB0_de - 0x80000000U)},
    {{&D_800D7070[102], 3038, 3580, 0, 0}, {0, 0, 0}, 0, (ResidentPickupHandler)((char *)func_802ACFB0_de - 0x80000000U)},
    {{&D_800D7070[102], 3039, 3580, 0, 0}, {0, 0, 0}, 0, (ResidentPickupHandler)((char *)func_802ACFB0_de - 0x80000000U)},
    {{&D_800D7070[102], 3040, 3580, 0, 0}, {0, 0, 0}, 0, (ResidentPickupHandler)((char *)func_802ACFB0_de - 0x80000000U)},
    {{&D_800D7070[102], 3041, 3580, 0, 0}, {0, 0, 0}, 0, (ResidentPickupHandler)((char *)func_802ACFB0_de - 0x80000000U)},
    {{&D_800D7070[102], 3042, 3580, 0, 0}, {0, 0, 0}, 0, (ResidentPickupHandler)((char *)func_802ACFB0_de - 0x80000000U)},
    {{&D_800D7070[102], 3043, 3580, 0, 0}, {0, 0, 0}, 0, (ResidentPickupHandler)((char *)func_802ACFB0_de - 0x80000000U)},
    {{&D_800D7070[102], 3044, 3580, 0, 0}, {0, 0, 0}, 0, (ResidentPickupHandler)((char *)func_802ACFB0_de - 0x80000000U)},
    {{&D_800D7070[102], 3045, 3580, 0, 0}, {0, 0, 0}, 0, (ResidentPickupHandler)((char *)func_802ACFB0_de - 0x80000000U)},
    {{&D_800D7070[102], 3046, 3580, 0, 0}, {0, 0, 0}, 0, (ResidentPickupHandler)((char *)func_802ACFB0_de - 0x80000000U)},
    {{&D_800D7070[102], 3047, 3580, 0, 0}, {0, 0, 0}, 0, (ResidentPickupHandler)((char *)func_802ACFB0_de - 0x80000000U)},
    {{&D_800D7070[102], 3048, 3580, 0, 0}, {0, 0, 0}, 0, (ResidentPickupHandler)((char *)func_802ACFB0_de - 0x80000000U)},
};
