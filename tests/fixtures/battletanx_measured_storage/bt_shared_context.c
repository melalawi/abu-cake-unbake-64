typedef struct {
    unsigned int w0;
    unsigned int w1;
} Awords;
typedef union {
    Awords words;
    long long int force_union_align;
} Acmd;
typedef Acmd *(*ALCmdHandler)(void *, short *, int, int, Acmd *);
typedef int (*ALDMAproc)(int addr, int len, void *state);
typedef int (*ALVoiceHandler)(void *);
typedef int (*ALSetParam)(void *, int, void *);
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
struct Measured_func_800949D8_us_07091f3fea0d { int value; };
struct Measured_func_800949D8_us_5bc2a4c5e17f { unsigned char padding[4]; int value; };
struct Measured_func_800949D8_us_7cc74cb35a88 { unsigned char padding[12]; int value; };
