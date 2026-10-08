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
struct MenuStrings_D4290;
extern struct MenuStrings_D4290 D_800D3690;
const char *D_800D7070[103] = {
    (const char *)&D_800D3690 + 0,
    (const char *)&D_800D3690 + 12,
    (const char *)&D_800D3690 + 24,
    (const char *)&D_800D3690 + 36,
    (const char *)&D_800D3690 + 48,
    (const char *)&D_800D3690 + 60,
    (const char *)&D_800D3690 + 72,
    (const char *)&D_800D3690 + 88,
    (const char *)&D_800D3690 + 104,
    (const char *)&D_800D3690 + 120,
    (const char *)&D_800D3690 + 136,
    (const char *)&D_800D3690 + 152,
    (const char *)&D_800D3690 + 164,
    (const char *)&D_800D3690 + 184,
    (const char *)&D_800D3690 + 204,
    (const char *)&D_800D3690 + 220,
    (const char *)&D_800D3690 + 236,
    (const char *)&D_800D3690 + 252,
    (const char *)&D_800D3690 + 264,
    (const char *)&D_800D3690 + 280,
    (const char *)&D_800D3690 + 300,
    (const char *)&D_800D3690 + 320,
    (const char *)&D_800D3690 + 340,
    (const char *)&D_800D3690 + 364,
    (const char *)&D_800D3690 + 384,
    (const char *)&D_800D3690 + 404,
    (const char *)&D_800D3690 + 420,
    (const char *)&D_800D3690 + 440,
    (const char *)&D_800D3690 + 460,
    (const char *)&D_800D3690 + 476,
    (const char *)&D_800D3690 + 496,
    (const char *)&D_800D3690 + 520,
    (const char *)&D_800D3690 + 532,
    (const char *)&D_800D3690 + 544,
    (const char *)&D_800D3690 + 556,
    (const char *)&D_800D3690 + 568,
    (const char *)&D_800D3690 + 584,
    (const char *)&D_800D3690 + 592,
    (const char *)&D_800D3690 + 600,
    (const char *)&D_800D3690 + 616,
    (const char *)&D_800D3690 + 624,
    (const char *)&D_800D3690 + 640,
    (const char *)&D_800D3690 + 652,
    (const char *)&D_800D3690 + 664,
    (const char *)&D_800D3690 + 676,
    (const char *)&D_800D3690 + 688,
    (const char *)&D_800D3690 + 700,
    (const char *)&D_800D3690 + 712,
    (const char *)&D_800D3690 + 724,
    (const char *)&D_800D3690 + 740,
    (const char *)&D_800D3690 + 756,
    (const char *)&D_800D3690 + 768,
    (const char *)&D_800D3690 + 780,
    (const char *)&D_800D3690 + 796,
    (const char *)&D_800D3690 + 812,
    (const char *)&D_800D3690 + 828,
    (const char *)&D_800D3690 + 844,
    (const char *)&D_800D3690 + 1484,
    (const char *)&D_800D3690 + 1500,
    (const char *)&D_800D3690 + 1516,
    (const char *)&D_800D3690 + 1532,
    (const char *)&D_800D3690 + 1544,
    (const char *)&D_800D3690 + 1564,
    (const char *)&D_800D3690 + 1580,
    (const char *)&D_800D3690 + 1604,
    (const char *)&D_800D3690 + 1620,
    (const char *)&D_800D3690 + 1640,
    (const char *)&D_800D3690 + 1668,
    (const char *)&D_800D3690 + 1688,
    (const char *)&D_800D3690 + 1708,
    (const char *)&D_800D3690 + 1728,
    (const char *)&D_800D3690 + 1752,
    (const char *)&D_800D3690 + 1772,
    (const char *)&D_800D3690 + 860,
    (const char *)&D_800D3690 + 880,
    (const char *)&D_800D3690 + 900,
    (const char *)&D_800D3690 + 920,
    (const char *)&D_800D3690 + 940,
    (const char *)&D_800D3690 + 964,
    (const char *)&D_800D3690 + 980,
    (const char *)&D_800D3690 + 996,
    (const char *)&D_800D3690 + 1012,
    (const char *)&D_800D3690 + 1032,
    (const char *)&D_800D3690 + 1052,
    (const char *)&D_800D3690 + 1068,
    (const char *)&D_800D3690 + 1084,
    (const char *)&D_800D3690 + 1104,
    (const char *)&D_800D3690 + 1112,
    (const char *)&D_800D3690 + 1124,
    (const char *)&D_800D3690 + 1176,
    (const char *)&D_800D3690 + 1148,
    (const char *)&D_800D3690 + 1156,
    (const char *)&D_800D3690 + 1184,
    (const char *)&D_800D3690 + 1204,
    (const char *)&D_800D3690 + 1224,
    (const char *)&D_800D3690 + 1236,
    (const char *)&D_800D3690 + 1256,
    (const char *)&D_800D3690 + 1272,
    (const char *)&D_800D3690 + 1288,
    (const char *)&D_800D3690 + 1304,
    (const char *)&D_800D3690 + 1320,
    (const char *)&D_800D3690 + 1340,
    (const char *)&D_800D3690 + 1360,
};
