typedef signed short s16;

typedef unsigned short u16;

typedef signed int s32;

typedef unsigned int u32;

typedef float f32;

typedef struct Vec3 Vec3;

struct Vec3 {
    f32 x;
    f32 y;
    f32 z;
};

typedef struct Draw Draw;

typedef struct MenuRules MenuRules;

struct MenuRules {
    char pad0[0x1C];
    s32 locked;
};

struct Draw {
    char pad0[0xC];
    void *model;
};

typedef struct Handles Handles;

typedef struct Item57BD4 Item57BD4;

struct Handles {
    char pad0[0x60];
    s16 handles[17];
};

struct func_80257A34_S1 {
    char pad0[0x7C];
    Handles unk7C;
    char pad7C[0x110 - 0x7C - sizeof(Handles)];
    char unk110;
    char pad110[0x138 - 0x110 - sizeof(char)];
    char unk138;
    char pad138[0x1DB8 - 0x138 - sizeof(char)];
    char unk1DB8;
    char pad1DB8[0x1DBC - 0x1DB8 - sizeof(char)];
    char unk1DBC;
};

struct Item57BD4 {
    s16 field0;
    s16 field2;
    s16 field4;
    u16 flags;
};

typedef struct Voice Voice;

struct Voice {
    s32 unk0;
    s32 sound;
    s32 unk8;
    s32 unkC;
    char pad10[0x28];
    s16 priority;
    s16 channel;
    char pad3C[0x64];
    s32 pending;
    s32 flags;
    char padA8[0x24];
};

typedef struct func_80257BD4_S1 func_80257BD4_S1;

struct func_80257BD4_S1 {
    char pad0[0x110];
    char unk110;
    char pad110[0x138 - 0x110 - sizeof(char)];
    char unk138;
    char pad138[0x1DB8 - 0x138 - sizeof(char)];
    char unk1DB8;
    char pad1DB8[0x2B8C - 0x1DB8 - sizeof(char)];
    s16 unk2B8C;
    char pad2B8C[0x2B90 - 0x2B8C - sizeof(s16)];
    s32 unk2B90;
    char pad2B90[0x2B98 - 0x2B90 - sizeof(s32)];
    void * unk2B98;
};

typedef struct func_80257A34_S1 func_80257A34_S1;
