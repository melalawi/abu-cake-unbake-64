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
typedef struct {
    char pad[0x102];
    s16 local;
} Header_func_8025B5F0_de;
typedef struct {
    char pad[0x84];
    char sound[0x58];
    s16 samples[20];
    char pad2[0x104 - 0xDC - 40];
    s32 key;
} Owner_func_8025B5F0_de;
typedef struct {
    s32 index;
    s32 state;
    s32 used;
    char pad0[4];
    s32 key;
    char pad1[0x26];
    s16 id;
    char pad1b[0x14];
    s32 flag;
    char pad2[0x50];
    s32 mode;
    s32 value;
    s32 active;
    Owner_func_8025B5F0_de *owner;
    char pad3[0x18];
} SlotCC;
typedef struct {
    Header_func_8025B5F0_de *header;
    SlotCC slots[17];
} RecordD90;
extern void func_802B2F00_de(void *sound, s16 sample);
extern s32 func_802B2620_de(void *sound);
extern void func_802B2F60_de(void *sound);
static __inline__ void release(SlotCC *slot) {
    Owner_func_8025B5F0_de *owner = slot->owner;
    void *sound;
    slot->active = 1;
    slot->flag = 0;
    if (slot->key != owner->key) {
        sound = owner->sound;
        func_802B2F00_de(sound, owner->samples[slot->index]);
        if (func_802B2620_de(sound) != 0) {
            func_802B2F60_de(sound);
        }
        slot->state = -1;
    }
}
